"""从视频里挑 N 帧「清晰 + 正脸」的画面做封面。

- 竖版封面(portrait)= 评分最高的那一帧
- 横版封面(landscape)= 选出的 N 帧按时间顺序横向拼接

评分 = 清晰度(Laplacian 方差,过滤动态模糊) + 正脸(Haar 人脸:有脸 / 脸大 / 居中)。
只在「检测到正脸」的帧里挑;若整段都没检到脸,退化为挑最清晰的 N 帧。
选出的 N 帧之间强制时间间隔,避免 3 张几乎一样。

依赖:opencv-python(本项目已装)、numpy。

用法(项目根目录):
    uv run python myUtils/cover_frames.py <video.mp4> [--outdir DIR] [--count 3] [--samples 60]

输出(默认 outdir = 视频同级的 covers_<stem>/):
    cover_portrait.jpg    竖版(最佳单帧)
    cover_landscape.jpg   横版(N 帧横拼,左→右按时间)
    frame_1.jpg ...       选出的各帧(带评分信息打印到 stdout)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

_FACE_CASCADE = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")


def _sharpness(gray: np.ndarray) -> float:
    """Laplacian 方差:越大越清晰(动态模糊帧会很低)。"""
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def _best_face(gray: np.ndarray):
    """返回最大正脸 (x, y, w, h) 或 None。"""
    faces = _FACE_CASCADE.detectMultiScale(gray, scaleFactor=1.05, minNeighbors=5, minSize=(24, 24))
    if len(faces) == 0:
        return None
    return max(faces, key=lambda f: f[2] * f[3])


def _score_frame(frame: np.ndarray) -> dict:
    h, w = frame.shape[:2]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    sharp = _sharpness(gray)
    face = _best_face(gray)
    if face is None:
        return {"has_face": False, "sharp": sharp, "face_area": 0.0, "centered": 0.0}
    fx, fy, fw, fh = face
    face_area = (fw * fh) / (w * h)
    # 脸中心离画面水平中心越近越好(竖版封面人脸居中更好看)
    fcx = fx + fw / 2
    centered = 1.0 - min(1.0, abs(fcx - w / 2) / (w / 2))
    return {"has_face": True, "sharp": sharp, "face_area": face_area, "centered": centered}


def _zscore(values: list[float]) -> list[float]:
    arr = np.array(values, dtype=np.float64)
    std = arr.std()
    if std < 1e-9:
        return [0.0] * len(values)
    return list((arr - arr.mean()) / std)


def pick_frames(video: Path, count: int = 3, samples: int = 60) -> dict:
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise RuntimeError(f"打不开视频: {video}")
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    if total <= 0:
        cap.release()
        raise RuntimeError("读不到帧数")

    # 跳过头尾 3%(避开黑帧/转场),均匀采样
    lo, hi = int(total * 0.03), int(total * 0.97)
    hi = max(hi, lo + 1)
    idxs = sorted(set(int(x) for x in np.linspace(lo, hi, min(samples, hi - lo))))

    cands = []
    for fi in idxs:
        cap.set(cv2.CAP_PROP_POS_FRAMES, fi)
        ok, frame = cap.read()
        if not ok or frame is None:
            continue
        s = _score_frame(frame)
        s["index"] = fi
        s["t"] = fi / fps
        s["frame"] = frame
        cands.append(s)
    cap.release()
    if not cands:
        raise RuntimeError("没采到有效帧")

    faced = [c for c in cands if c["has_face"]]
    pool = faced if faced else cands  # 没脸就退化为全部(只比清晰度)

    # 综合分:清晰度 z 分 + 脸大 z 分 + 居中(有脸时)
    zs = _zscore([c["sharp"] for c in pool])
    za = _zscore([c["face_area"] for c in pool]) if faced else [0.0] * len(pool)
    for c, zsharp, zarea in zip(pool, zs, za):
        c["score"] = zsharp + (1.2 * zarea + 0.6 * c["centered"] if faced else 0.0)

    # 贪心挑分散的 count 帧:每挑一帧,排除其 ±min_gap 秒内的候选
    min_gap = max(1.0, (cands[-1]["t"] - cands[0]["t"]) / (count + 1))
    chosen: list[dict] = []
    for c in sorted(pool, key=lambda x: x["score"], reverse=True):
        if all(abs(c["t"] - p["t"]) >= min_gap for p in chosen):
            chosen.append(c)
        if len(chosen) == count:
            break
    # 不够(间隔太严)就放宽补齐
    if len(chosen) < count:
        for c in sorted(pool, key=lambda x: x["score"], reverse=True):
            if c not in chosen:
                chosen.append(c)
            if len(chosen) == count:
                break

    portrait = max(chosen, key=lambda x: x["score"])  # 竖版 = 最佳单帧
    chosen_by_time = sorted(chosen, key=lambda x: x["t"])  # 横版按时间左→右
    return {
        "portrait": portrait,
        "ordered": chosen_by_time,
        "faced_count": len(faced),
        "sampled": len(cands),
    }


def generate_covers(video: Path, outdir: Path | None = None, count: int = 3, samples: int = 60) -> dict:
    """挑帧并写出竖版/横版封面。返回 {portrait, landscape, frames, outdir, report}。

    - portrait  = 最佳正脸帧(竖版封面,给快手/竖版平台)
    - landscape = count 帧按时间横拼(横版封面,给 B站)
    供发布编排(publish_dance.py)直接调用,封面这一步因此被写进发布流程,不靠记忆。
    """
    outdir = outdir or video.parent / f"covers_{video.stem}"
    outdir.mkdir(parents=True, exist_ok=True)
    res = pick_frames(video, count=count, samples=samples)
    ordered = res["ordered"]

    frames = []
    for i, c in enumerate(ordered, 1):
        p = outdir / f"frame_{i}.jpg"
        cv2.imwrite(str(p), c["frame"], [cv2.IMWRITE_JPEG_QUALITY, 95])
        frames.append(p)

    portrait_path = outdir / "cover_portrait.jpg"
    cv2.imwrite(str(portrait_path), res["portrait"]["frame"], [cv2.IMWRITE_JPEG_QUALITY, 95])
    landscape_path = outdir / "cover_landscape.jpg"
    cv2.imwrite(
        str(landscape_path),
        cv2.hconcat([c["frame"] for c in ordered]),
        [cv2.IMWRITE_JPEG_QUALITY, 95],
    )

    lines = [
        f"采样 {res['sampled']} 帧,检到正脸 {res['faced_count']} 帧"
        + ("" if res["faced_count"] else " → 无脸,退化为挑最清晰帧")
    ]
    for i, c in enumerate(ordered, 1):
        tag = " ★竖版" if c is res["portrait"] else ""
        lines.append(
            f"  frame_{i}: t={c['t']:.1f}s 清晰度={c['sharp']:.0f} "
            f"脸={'有' if c['has_face'] else '无'} 分={c['score']:.2f}{tag}"
        )
    return {
        "portrait": portrait_path,
        "landscape": landscape_path,
        "frames": frames,
        "outdir": outdir,
        "report": "\n".join(lines),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="从视频挑清晰正脸帧做竖版/横版封面")
    ap.add_argument("video", type=Path)
    ap.add_argument("--outdir", type=Path, default=None)
    ap.add_argument("--count", type=int, default=3, help="挑几帧(横版用,默认 3)")
    ap.add_argument("--samples", type=int, default=60, help="候选采样帧数")
    args = ap.parse_args(argv)

    if not args.video.exists():
        print(f"❌ 视频不存在: {args.video}", file=sys.stderr)
        return 1
    r = generate_covers(args.video, args.outdir, args.count, args.samples)
    print(r["report"])
    print(f"竖版封面: {r['portrait']}")
    print(f"横版封面: {r['landscape']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
