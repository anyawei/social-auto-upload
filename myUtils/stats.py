"""作品数据拉取(播放/点赞/评论/分享/收藏)。

三个平台都从**官方接口的 JSON** 取数,不抠 DOM(DOM 文本对新作品常显示 "-",且布局易变):

| 平台   | 数据来源                                          | 浏览器       |
|--------|---------------------------------------------------|--------------|
| 抖音   | 拦截 ``janus/douyin/creator/pc/work_list`` 响应   | **必须 headed** |
| 快手   | 拦截 ``rest/cp/works/v2/video/pc/photo/list`` 响应 | headless 可  |
| B 站   | 直接调 ``member.bilibili.com/x/web/archives`` API | 无需浏览器   |

抖音为何必须 headed:抖音对 session 做设备指纹绑定,headless 指纹与扫码登录(headed)
不一致时,creator 站点只返回"登录壳子"、**work_list 接口返回空作品列表**(实测 headless
"共 0 个作品" vs headed "共 4 个作品")。所以抖音读写都得 headed,见
``feedback-dy-upload-pitfalls`` memory。

cookie 文件格式:
- 抖音 / 快手:Playwright ``storage_state``(``{"cookies": [...], "origins": [...]}``)
- B 站:biliup 格式(``{"cookie_info": {"cookies": [...]}, ...}``)
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from patchright.async_api import async_playwright

logger = logging.getLogger(__name__)

# --- 接口端点 / 标记 ---
DOUYIN_MANAGE_URL = "https://creator.douyin.com/creator-micro/content/manage"
DOUYIN_WORKLIST_MARKER = "creator/pc/work_list"
KUAISHOU_MANAGE_URL = "https://cp.kuaishou.com/article/manage/video"
KUAISHOU_PHOTOLIST_MARKER = "works/v2/video/pc/photo/list"
BILIBILI_ARCHIVES_API = (
    "https://member.bilibili.com/x/web/archives"
    "?status=is_pubing,pubed,not_pubed&pn=1&ps={limit}&interactive=1&coop=1"
)

# --- 轮询 / 超时常量 ---
PAGE_GOTO_TIMEOUT_MS = 60000
INTERCEPT_POLL_INTERVAL_MS = 1000
INTERCEPT_MAX_POLLS = 25
INTERCEPT_MIN_POLLS = 5
BILIBILI_HTTP_TIMEOUT_S = 20
BROWSER_CHANNEL = "chrome"
DEFAULT_LIMIT = 30

PLATFORM_DOUYIN = "douyin"
PLATFORM_KUAISHOU = "kuaishou"
PLATFORM_BILIBILI = "bilibili"
SUPPORTED_PLATFORMS = (PLATFORM_DOUYIN, PLATFORM_KUAISHOU, PLATFORM_BILIBILI)


@dataclass
class StatItem:
    """单个作品的统计数据。计数字段为 ``None`` 表示平台尚未生成数据(显示 "-")。"""

    platform: str
    title: str
    item_id: str = ""
    publish_time: str = ""
    play: int | None = None
    like: int | None = None
    comment: int | None = None
    share: int | None = None
    collect: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def matches(self, keyword: str | None) -> bool:
        """标题是否包含关键字(``None``/空 视为全部匹配)。"""
        return not keyword or keyword in self.title


def _require_existing_file(account_file: str | Path, platform: str) -> Path:
    path = Path(account_file)
    if not path.is_file():
        raise FileNotFoundError(
            f"{platform} cookie 文件不存在: {path}。先用 `sau {platform} login` 登录。"
        )
    return path


def _ms_to_datetime(ms: int | None) -> str:
    """毫秒时间戳 → ``YYYY-MM-DD HH:MM``;非法值返回空串。"""
    if not isinstance(ms, (int, float)) or ms <= 0:
        return ""
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")


async def _intercept_endpoint_json(
    account_file: Path,
    *,
    page_url: str,
    endpoint_marker: str,
    headless: bool,
) -> list[dict[str, Any]]:
    """打开 ``page_url``,拦截 URL 含 ``endpoint_marker`` 的 JSON 响应并返回其 body 列表。

    :param account_file: Playwright storage_state cookie 文件
    :param page_url: 要打开的创作中心页面
    :param endpoint_marker: 目标接口 URL 子串
    :param headless: 是否无头(抖音须 False)
    :returns: 匹配到的 JSON body 列表(按到达顺序)
    """
    captured: list[dict[str, Any]] = []

    async def on_response(resp) -> None:
        if endpoint_marker not in resp.url:
            return
        if "json" not in resp.headers.get("content-type", ""):
            return
        try:
            captured.append(await resp.json())
        except Exception as exc:  # noqa: BLE001 - 单个响应解析失败不应中断整体
            logger.warning("解析 %s 响应失败: %s", endpoint_marker, exc)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=headless, channel=BROWSER_CHANNEL)
        try:
            context = await browser.new_context(storage_state=str(account_file))
            page = await context.new_page()
            page.on("response", on_response)
            await page.goto(page_url, wait_until="domcontentloaded", timeout=PAGE_GOTO_TIMEOUT_MS)
            for poll in range(INTERCEPT_MAX_POLLS):
                await page.wait_for_timeout(INTERCEPT_POLL_INTERVAL_MS)
                if captured and poll >= INTERCEPT_MIN_POLLS:
                    break
        finally:
            await browser.close()

    if not captured:
        raise RuntimeError(
            f"未拦截到接口 {endpoint_marker} 的响应(可能 cookie 失效或被风控)。"
            + ("" if not headless else " 抖音须用 headed(headless 会拿到空列表)。")
        )
    return captured


def _walk_find_dicts(obj: Any, predicate) -> list[dict[str, Any]]:
    """深度遍历嵌套结构,收集所有满足 ``predicate(dict)`` 的字典。"""
    out: list[dict[str, Any]] = []
    if isinstance(obj, dict):
        if predicate(obj):
            out.append(obj)
        for value in obj.values():
            out.extend(_walk_find_dicts(value, predicate))
    elif isinstance(obj, list):
        for value in obj:
            out.extend(_walk_find_dicts(value, predicate))
    return out


async def fetch_douyin(account_file: str | Path, *, headless: bool = False, limit: int = DEFAULT_LIMIT) -> list[StatItem]:
    """拉取抖音作品统计。**默认 headed**(headless 会拿到空列表,见模块 docstring)。"""
    path = _require_existing_file(account_file, PLATFORM_DOUYIN)
    bodies = await _intercept_endpoint_json(
        path, page_url=DOUYIN_MANAGE_URL, endpoint_marker=DOUYIN_WORKLIST_MARKER, headless=headless
    )
    nodes = _walk_find_dicts(
        bodies, lambda d: "statistics" in d and isinstance(d.get("statistics"), dict) and "desc" in d
    )
    items: list[StatItem] = []
    seen: set[str] = set()
    for node in nodes:
        stat = node["statistics"]
        aweme_id = str(stat.get("aweme_id") or node.get("aweme_id") or "")
        if aweme_id and aweme_id in seen:
            continue
        seen.add(aweme_id)
        items.append(
            StatItem(
                platform=PLATFORM_DOUYIN,
                title=(node.get("desc") or "").strip(),
                item_id=aweme_id,
                publish_time=_ms_to_datetime((node.get("create_time") or 0) * 1000)
                if node.get("create_time")
                else "",
                play=stat.get("play_count"),
                like=stat.get("digg_count"),
                comment=stat.get("comment_count"),
                share=stat.get("share_count"),
                collect=stat.get("collect_count"),
            )
        )
    return items[:limit]


async def fetch_kuaishou(account_file: str | Path, *, headless: bool = True, limit: int = DEFAULT_LIMIT) -> list[StatItem]:
    """拉取快手作品统计(headless 可用)。"""
    path = _require_existing_file(account_file, PLATFORM_KUAISHOU)
    bodies = await _intercept_endpoint_json(
        path, page_url=KUAISHOU_MANAGE_URL, endpoint_marker=KUAISHOU_PHOTOLIST_MARKER, headless=headless
    )
    nodes = _walk_find_dicts(bodies, lambda d: "playCount" in d and ("title" in d or "caption" in d))
    items: list[StatItem] = []
    seen: set[str] = set()
    for node in nodes:
        work_id = str(node.get("workId") or "")
        if work_id and work_id in seen:
            continue
        seen.add(work_id)
        items.append(
            StatItem(
                platform=PLATFORM_KUAISHOU,
                title=(node.get("title") or node.get("caption") or "").replace("\n", " ").strip(),
                item_id=work_id,
                publish_time=_ms_to_datetime(node.get("uploadTime")),
                play=node.get("playCount"),
                like=node.get("likeCount"),
                comment=node.get("commentCount"),
            )
        )
    return items[:limit]


def _load_bilibili_cookie_header(account_file: Path) -> str:
    """从 biliup 格式 cookie 文件构造 HTTP ``Cookie`` 头。"""
    data = json.loads(account_file.read_text(encoding="utf-8"))
    cookies = data.get("cookie_info", {}).get("cookies") or data.get("cookies") or []
    if not cookies:
        raise RuntimeError(f"B 站 cookie 文件无 cookie_info.cookies: {account_file}")
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies)


def fetch_bilibili(account_file: str | Path, *, limit: int = DEFAULT_LIMIT) -> list[StatItem]:
    """拉取 B 站稿件统计(纯 HTTP,无需浏览器)。

    注意:B 站播放量批量延迟更新、点赞实时,新视频可能出现点赞数 > 播放数,属正常。
    """
    path = _require_existing_file(account_file, PLATFORM_BILIBILI)
    cookie_header = _load_bilibili_cookie_header(path)
    request = urllib.request.Request(
        BILIBILI_ARCHIVES_API.format(limit=limit),
        headers={
            "Cookie": cookie_header,
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "Referer": "https://member.bilibili.com/platform/upload-manager/article",
        },
    )
    with urllib.request.urlopen(request, timeout=BILIBILI_HTTP_TIMEOUT_S) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if payload.get("code") != 0:
        raise RuntimeError(f"B 站 archives 接口失败 code={payload.get('code')}: {payload.get('message')}")

    items: list[StatItem] = []
    for audit in payload.get("data", {}).get("arc_audits", []):
        archive = audit.get("Archive") or audit.get("archive") or {}
        stat = audit.get("stat") or audit.get("Stat") or {}
        ptime = archive.get("ptime") or archive.get("ctime") or 0
        items.append(
            StatItem(
                platform=PLATFORM_BILIBILI,
                title=(archive.get("title") or "").strip(),
                item_id=archive.get("bvid") or "",
                publish_time=_ms_to_datetime(ptime * 1000) if ptime else "",
                play=stat.get("view"),
                like=stat.get("like"),
                comment=stat.get("reply"),
                share=stat.get("share"),
                collect=stat.get("favorite") or stat.get("fav"),
                extra={"state_desc": archive.get("state_desc", "")},
            )
        )
    return items[:limit]


async def fetch_platform(platform: str, account_file: str | Path, *, limit: int = DEFAULT_LIMIT) -> list[StatItem]:
    """按平台名分发到对应拉取函数。"""
    if platform == PLATFORM_DOUYIN:
        return await fetch_douyin(account_file, limit=limit)
    if platform == PLATFORM_KUAISHOU:
        return await fetch_kuaishou(account_file, limit=limit)
    if platform == PLATFORM_BILIBILI:
        return fetch_bilibili(account_file, limit=limit)
    raise ValueError(f"不支持的平台: {platform}(支持 {', '.join(SUPPORTED_PLATFORMS)})")


def _cell(value: int | None) -> str:
    return "-" if value is None else str(value)


def _display_width(text: str) -> int:
    """CJK 字符按 2 列宽计算(终端等宽对齐用)。"""
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def render_table(items: list[StatItem], *, title_width: int = 24) -> str:
    """把 StatItem 列表渲染成等宽对齐的文本表格。"""
    headers = ["平台", "标题", "发布时间", "播放", "点赞", "评论", "分享"]
    rows = [headers]
    for it in items:
        title = it.title if _display_width(it.title) <= title_width else it.title[: title_width // 2] + "…"
        rows.append(
            [
                it.platform,
                title,
                it.publish_time or "-",
                _cell(it.play),
                _cell(it.like),
                _cell(it.comment),
                _cell(it.share),
            ]
        )
    widths = [max(_display_width(r[c]) for r in rows) for c in range(len(headers))]
    lines = [" | ".join(_pad(row[c], widths[c]) for c in range(len(headers))) for row in rows]
    sep = "-+-".join("-" * w for w in widths)
    lines.insert(1, sep)
    return "\n".join(lines)
