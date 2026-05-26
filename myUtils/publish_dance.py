"""一键发片(舞蹈成片 → B站/抖音/快手):把封面生成 + 标题/简介/标签规则写进发布流程。

之所以做成一个脚本,是为了让"先生成封面再发布"这件事**固化在代码里、不依赖记忆**:
跑这个脚本就会自动按规则出封面、拼标题标签、逐平台发布。

规则(2026-05,anyawei 约定):
- 标题 = 简介 = "{角色名}，跳个{舞蹈名}"
- 标签 = 角色名, 舞蹈名, 风格, AI少女, 舞蹈挑战
- 封面 = 自动调 cover_frames.generate_covers(清晰度+正脸评分挑帧):
    B站(横版) → cover_landscape.jpg(3帧横拼)
    快手(竖版) → cover_portrait.jpg(最佳正脸帧)
    抖音        → 不传封面(自动用首帧;传自定义图会触发封面 modal 报错)
- 平台命令统一走 sau_cli(同 CLI 契约);抖音强制 --headed(headless 必超时失败)。
- 账号默认「沄」,B站分区默认宅舞(tid=20)。

用法(在 social-auto-upload 根目录):
    uv run python myUtils/publish_dance.py \
        --video <成片.mp4> --dance "叮叮当当舞" \
        [--role 沥] [--style 赛博朋克] [--account 沄] \
        [--platforms bilibili,douyin,kuaishou] [--tid 20] [--dry-run]

--dry-run:只生成封面 + 打印将执行的发布命令,不真正发布(先验封面/命令时用)。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SAU_CLI = REPO / "sau_cli.py"
sys.path.insert(0, str(REPO))

from myUtils.cover_frames import generate_covers  # noqa: E402

DEFAULT_ROLE = "沥"
DEFAULT_STYLE = "赛博朋克"
DEFAULT_ACCOUNT = "沄"
DEFAULT_PLATFORMS = ["bilibili", "douyin", "kuaishou"]
DEFAULT_TID = 20  # B站 宅舞


def _run_sau(extra: list[str], dry: bool) -> int:
    cmd = [sys.executable, str(SAU_CLI), *extra]
    printable = " ".join(c if c.startswith("-") or "/" not in c else f'"{c}"' for c in extra)
    print(f"  $ sau {printable}")
    if dry:
        return 0
    return subprocess.run(cmd, cwd=str(REPO)).returncode


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="一键发片:自动封面+标题+标签,发 B站/抖音/快手")
    ap.add_argument("--video", required=True, type=Path)
    ap.add_argument("--dance", required=True, help="舞蹈名,如 叮叮当当舞(标题里跟在'跳个'后)")
    ap.add_argument("--role", default=DEFAULT_ROLE, help=f"角色名,默认 {DEFAULT_ROLE}")
    ap.add_argument("--style", default=DEFAULT_STYLE, help=f"风格(用于标签),默认 {DEFAULT_STYLE}")
    ap.add_argument("--account", default=DEFAULT_ACCOUNT)
    ap.add_argument("--platforms", default=",".join(DEFAULT_PLATFORMS))
    ap.add_argument("--tid", type=int, default=DEFAULT_TID, help="B站分区,默认 20(宅舞)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    if not args.video.exists():
        print(f"❌ 视频不存在: {args.video}", file=sys.stderr)
        return 1

    title = f"{args.role}，跳个{args.dance}"
    desc = title
    tags = ",".join([args.role, args.dance, args.style, "AI少女", "舞蹈挑战"])
    platforms = [p.strip() for p in args.platforms.split(",") if p.strip()]

    print(f"标题/简介: {title}")
    print(f"标签:      {tags}")
    print(f"平台:      {', '.join(platforms)}  账号: {args.account}" + ("  [DRY-RUN]" if args.dry_run else ""))

    print("\n① 生成封面(清晰度+正脸挑帧)...")
    cov = generate_covers(args.video)
    print(cov["report"])
    portrait, landscape = str(cov["portrait"]), str(cov["landscape"])
    print(f"  竖版(快手): {portrait}")
    print(f"  横版(B站):  {landscape}")
    print("  抖音:       不传封面,自动用首帧")

    base = ["--account", args.account, "--file", str(args.video), "--title", title, "--desc", desc, "--tags", tags]
    results: dict[str, str] = {}
    for p in platforms:
        print(f"\n② 发布 {p} ...")
        if p == "bilibili":
            rc = _run_sau(
                ["bilibili", "upload-video", *base, "--tid", str(args.tid), "--thumbnail", landscape], args.dry_run
            )
        elif p == "kuaishou":
            rc = _run_sau(["kuaishou", "upload-video", *base, "--thumbnail", portrait], args.dry_run)
        elif p == "douyin":
            rc = _run_sau(["douyin", "upload-video", *base, "--headed"], args.dry_run)  # 不传封面
        else:
            print(f"  ⚠️ 跳过未知平台: {p}")
            continue
        results[p] = "OK" if rc == 0 else f"FAIL(rc={rc})"

    print("\n=== 汇总 ===")
    for p, r in results.items():
        print(f"  {p}: {r}")
    if not args.dry_run and any(r != "OK" for r in results.values()):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
