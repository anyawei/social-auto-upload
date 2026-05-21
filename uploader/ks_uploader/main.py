# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import inspect
import os
from datetime import datetime
from pathlib import Path

from patchright.async_api import Page
from patchright.async_api import Playwright
from patchright.async_api import TimeoutError as PlaywrightTimeoutError
from patchright.async_api import async_playwright

from conf import DEBUG_MODE, LOCAL_CHROME_HEADLESS, LOCAL_CHROME_PATH
from uploader.base_video import BaseVideoUploader
from utils.base_social_media import set_init_script
from utils.files_times import get_absolute_path
from utils.login_qrcode import build_login_qrcode_path
from utils.login_qrcode import decode_qrcode_from_path
from utils.login_qrcode import print_terminal_qrcode
from utils.login_qrcode import remove_qrcode_file
from utils.login_qrcode import save_data_url_image
from utils.log import kuaishou_logger

KUAISHOU_UPLOAD_URL = "https://cp.kuaishou.com/article/publish/video"
KUAISHOU_MANAGE_URL = "https://cp.kuaishou.com/article/manage/video?status=2&from=publish"
KUAISHOU_LOGIN_URL = "https://passport.kuaishou.com/pc/account/login/?sid=kuaishou.web.cp.api&callback=https%3A%2F%2Fcp.kuaishou.com%2Frest%2Finfra%2Fsts%3FfollowUrl%3Dhttps%253A%252F%252Fcp.kuaishou.com%252Farticle%252Fpublish%252Fvideo%26setRootDomain%3Dtrue"
KUAISHOU_UPLOAD_URL_PATTERN = "**/article/publish/video**"
KUAISHOU_MANAGE_URL_PATTERN = "**/article/manage/video?status=2&from=publish**"
KUAISHOU_COOKIE_INVALID_SELECTOR = "div.names div.container div.name:text('机构服务')"
KUAISHOU_PUBLISH_STRATEGY_IMMEDIATE = "immediate"
KUAISHOU_PUBLISH_STRATEGY_SCHEDULED = "scheduled"


def _msg(emoji: str, text: str) -> str:
    return f"{emoji} {text}"


def _print_ks_qrcode(qrcode_content: str, qrcode_path: Path) -> None:
    try:
        print_terminal_qrcode(qrcode_content, qrcode_path, "快手APP", compact=False, border=2)
    except TypeError as exc:
        if "unexpected keyword argument 'compact'" not in str(exc):
            raise
        kuaishou_logger.warning(_msg("😵", "检测到旧版二维码打印函数，小人切回兼容模式继续登录"))
        print_terminal_qrcode(qrcode_content, qrcode_path, "快手APP")


async def _emit_qrcode_callback(qrcode_callback, payload: dict):
    if not qrcode_callback:
        return

    callback_result = qrcode_callback(payload)
    if inspect.isawaitable(callback_result):
        await callback_result


def _build_login_result(
    success: bool,
    status: str,
    message: str,
    account_file: str,
    qrcode: dict | None = None,
    current_url: str = "",
) -> dict:
    return {
        "success": success,
        "status": status,
        "message": message,
        "account_file": str(account_file),
        "qrcode": qrcode,
        "current_url": current_url,
    }


async def _is_ks_cookie_invalid(page: Page, timeout: int = 5000) -> bool:
    try:
        await page.wait_for_selector(KUAISHOU_COOKIE_INVALID_SELECTOR, timeout=timeout)
        return True
    except Exception:
        return False


async def _extract_ks_qrcode_src(page: Page) -> str:
    login_form = page.locator("main#login-form").first
    await login_form.wait_for(state="visible", timeout=30000)

    qrcode_img = login_form.locator('div.qr-login img[alt="qrcode"]').first
    try:
        if not await qrcode_img.count() or not await qrcode_img.is_visible():
            platform_switch = login_form.locator("div.platform-switch").first
            await platform_switch.wait_for(state="visible", timeout=10000)
            await platform_switch.click()
            await asyncio.sleep(1)
    except Exception:
        platform_switch = login_form.locator("div.platform-switch").first
        await platform_switch.wait_for(state="visible", timeout=10000)
        await platform_switch.click()
        await asyncio.sleep(1)

    await qrcode_img.wait_for(state="visible", timeout=15000)

    qrcode_src = await qrcode_img.get_attribute("src")
    if not qrcode_src:
        raise RuntimeError("未获取到快手登录二维码地址")

    return qrcode_src


async def _save_ks_qrcode(page: Page, account_file: str, previous_qrcode_path: Path | None = None, qrcode_callback=None) -> dict:
    qrcode_src = await _extract_ks_qrcode_src(page)
    qrcode_path = save_data_url_image(qrcode_src, build_login_qrcode_path(account_file, suffix="ks_login_qrcode"))

    if previous_qrcode_path and previous_qrcode_path != qrcode_path:
        if remove_qrcode_file(previous_qrcode_path):
            kuaishou_logger.info(_msg("🧹", f"临时二维码文件已清理: {previous_qrcode_path}"))

    kuaishou_logger.info(_msg("🖼️", f"二维码已经准备好啦，已保存到: {qrcode_path}"))
    qrcode_content = decode_qrcode_from_path(qrcode_path)
    if qrcode_content:
        _print_ks_qrcode(qrcode_content, qrcode_path)
    else:
        kuaishou_logger.warning(_msg("😵", f"终端没法完整显示二维码，请打开 {qrcode_path} 扫码"))

    qrcode_info = {
        "image_path": str(qrcode_path),
        "image_data_url": qrcode_src,
    }
    await _emit_qrcode_callback(qrcode_callback, qrcode_info)
    return qrcode_info


async def _is_ks_qrcode_expired(page: Page) -> bool:
    expired_box = page.locator("div.qrcode-status.qrcode-status-timeout").first
    try:
        if not await expired_box.count():
            return False
        return await expired_box.is_visible()
    except Exception:
        return False


async def _is_ks_login_page_gone(page: Page) -> bool:
    try:
        login_form = page.locator("main#login-form").first
        if not await login_form.count():
            return True
        return not await login_form.is_visible()
    except Exception:
        return True


async def cookie_auth(account_file):
    async with async_playwright() as playwright:
        if LOCAL_CHROME_PATH:
            browser = await playwright.chromium.launch(headless=True, executable_path=LOCAL_CHROME_PATH)
        else:
            browser = await playwright.chromium.launch(headless=True, channel="chrome")
        try:
            context = await browser.new_context(storage_state=account_file)
            context = await set_init_script(context)
            page = await context.new_page()
            await page.goto(KUAISHOU_UPLOAD_URL)
            if await _is_ks_cookie_invalid(page):
                kuaishou_logger.info(_msg("🥹", "cookie 已失效，得重新登录一下"))
                return False

            kuaishou_logger.success(_msg("🥳", "cookie 有效"))
            return True
        except Exception as exc:
            kuaishou_logger.warning(_msg("😵", f"cookie 校验时出错，按失效处理: {exc}"))
            return False
        finally:
            await browser.close()


async def ks_setup(account_file, handle=False, return_detail=False, qrcode_callback=None, headless: bool = LOCAL_CHROME_HEADLESS):
    account_file = get_absolute_path(account_file, "ks_uploader")
    if not os.path.exists(account_file) or not await cookie_auth(account_file):
        if not handle:
            result = _build_login_result(False, "cookie_invalid", "cookie文件不存在或已失效", account_file)
            return result if return_detail else False
        kuaishou_logger.info(_msg("🥹", "cookie 失效了，准备重新登录快手创作者平台"))
        result = await get_ks_cookie(account_file, qrcode_callback=qrcode_callback, headless=headless)
        return result if return_detail else result["success"]

    result = _build_login_result(True, "cookie_valid", "cookie有效", account_file)
    return result if return_detail else True


async def get_ks_cookie(
    account_file,
    qrcode_callback=None,
    headless: bool = LOCAL_CHROME_HEADLESS,
    poll_interval: int = 3,
    max_checks: int = 100,
):
    if headless:
        kuaishou_logger.info(_msg("🖼️", "快手登录将以无头模式运行，小人会输出终端二维码并保存本地二维码图片"))

    async with async_playwright() as playwright:
        if LOCAL_CHROME_PATH:
            browser = await playwright.chromium.launch(headless=headless, executable_path=LOCAL_CHROME_PATH)
        else:
            browser = await playwright.chromium.launch(headless=headless, channel="chrome")
        context = await browser.new_context()
        context = await set_init_script(context)
        qrcode_path = None
        qrcode_info = None
        result = _build_login_result(False, "failed", "快手登录失败", account_file)
        try:
            page = await context.new_page()
            await page.goto(KUAISHOU_LOGIN_URL)
            kuaishou_logger.info(_msg("🧍", "请在浏览器里扫码登录快手，小人正在耐心等待"))

            qrcode_info = await _save_ks_qrcode(page, account_file, qrcode_callback=qrcode_callback)
            qrcode_path = Path(qrcode_info["image_path"])

            for _ in range(max_checks):
                if page.url.startswith(KUAISHOU_UPLOAD_URL) or await _is_ks_login_page_gone(page):
                    await context.storage_state(path=account_file)
                    if await cookie_auth(account_file):
                        kuaishou_logger.success(_msg("🥳", "快手扫码登录成功，小人开心收工"))
                        result = _build_login_result(True, "success", "快手扫码登录成功", account_file, qrcode_info, page.url)
                    else:
                        kuaishou_logger.error(_msg("😢", "快手扫码完成了，但 cookie 校验失败"))
                        result = _build_login_result(
                            False,
                            "cookie_invalid",
                            "快手扫码流程结束，但 cookie 校验失败",
                            account_file,
                            qrcode_info,
                            page.url,
                        )
                    return result

                if qrcode_info and await _is_ks_qrcode_expired(page):
                    kuaishou_logger.warning(_msg("😵", "二维码失效了，小人马上去刷新"))
                    refresh_button = page.locator("p.qrcode-refresh").first
                    if await refresh_button.count():
                        await refresh_button.click()
                        await asyncio.sleep(1)
                    qrcode_info = await _save_ks_qrcode(
                        page,
                        account_file,
                        qrcode_path,
                        qrcode_callback=qrcode_callback,
                    )
                    qrcode_path = Path(qrcode_info["image_path"])

                await asyncio.sleep(poll_interval)

            result = _build_login_result(
                False,
                "timeout",
                "等待快手扫码登录超时",
                account_file,
                qrcode_info,
                page.url,
            )
        except Exception as exc:
            result = _build_login_result(False, "failed", str(exc), account_file, current_url=page.url if "page" in locals() else "")
        finally:
            if remove_qrcode_file(qrcode_path):
                kuaishou_logger.info(_msg("🧹", f"临时二维码文件已清理: {qrcode_path}"))
            if not result["success"]:
                kuaishou_logger.error(_msg("😢", f"登录失败: {result['message']}"))
            await context.close()
            await browser.close()

    return result


class KSBaseUploader(BaseVideoUploader):
    def __init__(
        self,
        publish_date: datetime | int,
        account_file,
        publish_strategy: str | None = None,
        debug: bool = DEBUG_MODE,
        headless: bool = LOCAL_CHROME_HEADLESS,
    ):
        self.publish_date = publish_date
        self.account_file = str(account_file)
        self.publish_strategy = publish_strategy
        self.debug = debug
        self.headless = headless
        self.local_executable_path = LOCAL_CHROME_PATH
        self.date_format = "%Y-%m-%d %H:%M"

    async def validate_base_args(self):
        if not os.path.exists(self.account_file):
            raise RuntimeError(f"cookie文件不存在，请先完成快手登录: {self.account_file}")
        if not await cookie_auth(self.account_file):
            raise RuntimeError(f"cookie文件已失效，请先完成快手登录: {self.account_file}")

        if self.publish_strategy is None:
            self.publish_strategy = (
                KUAISHOU_PUBLISH_STRATEGY_SCHEDULED
                if self.publish_date != 0
                else KUAISHOU_PUBLISH_STRATEGY_IMMEDIATE
            )

        if self.publish_strategy not in {
            KUAISHOU_PUBLISH_STRATEGY_IMMEDIATE,
            KUAISHOU_PUBLISH_STRATEGY_SCHEDULED,
        }:
            raise ValueError(f"不支持的发布策略: {self.publish_strategy}")

        if self.publish_strategy == KUAISHOU_PUBLISH_STRATEGY_SCHEDULED:
            self.publish_date = self.validate_publish_date(self.publish_date)
        else:
            self.publish_date = 0

    async def set_schedule_time(self, page: Page, publish_date: datetime):
        kuaishou_logger.info(_msg("🕒", "小人准备设置定时发布时间"))
        publish_date_hour = publish_date.strftime("%Y-%m-%d %H:%M:%S")
        await page.locator("label:text('发布时间')").locator("xpath=following-sibling::div").locator(".ant-radio-input").nth(1).click()
        await asyncio.sleep(1)
        await page.locator('div.ant-picker-input input[placeholder="选择日期时间"]').click()
        await asyncio.sleep(1)
        await page.keyboard.press("Control+KeyA")
        await page.keyboard.type(publish_date_hour)
        await page.keyboard.press("Enter")
        await asyncio.sleep(1)

    async def close_guide_overlay(self, page: Page) -> bool:
        joyride_tooltip = page.locator('div[id^="react-joyride-step"] div[role="alertdialog"]')

        # 判断是否显示
        if await joyride_tooltip.count() > 0 and await joyride_tooltip.first.is_visible():
            print("检测到 Joyride 引导遮罩，正在关闭...")

            # 点击关闭按钮（X），使用多个可靠特征
            close_button = page.locator('div[role="alertdialog"]').locator(
                '[aria-label="Skip"], [data-action="skip"], button[title="Skip"]'
            )

            await close_button.click(force=True)

            # 等待遮罩消失
            await joyride_tooltip.wait_for(state="hidden", timeout=5000)

            print("✅ 已关闭 Joyride 遮罩")
        else:
            print("未检测到 Joyride 遮罩，继续执行")


class KSVideo(KSBaseUploader):
    def __init__(
        self,
        title,
        file_path,
        tags,
        publish_date: datetime | int,
        account_file,
        publish_strategy: str | None = None,
        debug: bool = DEBUG_MODE,
        headless: bool = LOCAL_CHROME_HEADLESS,
        thumbnail_path=None,
        desc: str | None = None,
    ):
        super().__init__(
            publish_date=publish_date,
            account_file=account_file,
            publish_strategy=publish_strategy,
            debug=debug,
            headless=headless,
        )
        self.title = title
        self.file_path = file_path
        self.tags = tags or []
        self.thumbnail_path = thumbnail_path
        self.desc = desc or ""

    async def validate_upload_args(self):
        await self.validate_base_args()
        if not self.title or not str(self.title).strip():
            raise ValueError("快手视频上传时，title 是必须的")
        self.file_path = str(self.validate_video_file(self.file_path))
        if self.thumbnail_path:
            self.thumbnail_path = str(self.validate_image_file(self.thumbnail_path))

    async def handle_upload_error(self, page: Page):
        kuaishou_logger.warning(_msg("😵", "视频上传摔了一跤，小人马上重新上传"))
        await page.locator('div.progress-div [class^="upload-btn-input"]').set_input_files(self.file_path)

    async def set_thumbnail(self, page: Page):
        if not self.thumbnail_path:
            kuaishou_logger.info(
                _msg("🤷", "没传 thumbnail_path,跳过封面设置;快手会用视频首帧")
            )
            return

        kuaishou_logger.info(
            _msg("🖼️", f"小人准备设置封面 (路径: {self.thumbnail_path})")
        )

        # 点封面前先确保任何遮罩 / "我知道了" 引导已关掉,否则点击会被遮挡
        try:
            await self.close_guide_overlay(page)
        except Exception:
            pass

        cover_label = page.locator("span").filter(has_text="封面设置")
        await cover_label.wait_for(state="visible", timeout=30000)
        # 点封面区前先 scroll 到可见,有的版本封面块在折叠区里。
        try:
            await cover_label.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass

        if self.debug:
            try:
                shot_path = (
                    f"/tmp/sau_ks_before_cover_click_{int(asyncio.get_event_loop().time())}.png"
                )
                await page.screenshot(path=shot_path, full_page=True)
                kuaishou_logger.info(_msg("📸", f"点封面前页面截图: {shot_path}"))
            except Exception:
                pass

        await cover_label.locator("xpath=../following-sibling::div[1]").locator('div').nth(0).click()

        # 找封面 modal。ant-design 新老版本 DOM 结构差很多,这里多 selector 兜底,
        # 谁先 visible 取谁。原版 `div[role="document"].ant-modal` 在新版 antd 里
        # 失效(role="document" 已经从 ant-modal 上移除),会导致 30s timeout。
        await asyncio.sleep(1)
        # `:visible` 伪类 already 表示 Playwright 认为可见。但后续 `wait_for(visible)`
        # 在快手当前 UI 下会超时(动画/opacity/z-index 等),会把好端端的命中重置成 None。
        # 改成:只要伪类 match 命中数 > 0,就锁定它,不再额外 wait_for(visible)。
        #
        # 关键:优先用 `.ant-modal-wrap`(包含整个 modal 含 footer 按钮),
        # `.ant-modal-content` 只包含 header+body,可能不含 footer 的"确认"按钮 —
        # 这就是为什么后面 modal scope 找"确认"全 0 命中的根因。
        modal_selectors = [
            '.ant-modal-wrap:visible',                          # 新版:整个 modal wrapper(含 footer)
            '.ant-modal:visible',                               # 新版:next-level (一般也含 footer)
            '.ant-modal-content:visible',                       # 老版:可能不含 footer
            'div[role="dialog"].ant-modal:visible',
            'div[role="document"].ant-modal:visible',
        ]
        modal = None
        for sel in modal_selectors:
            loc = page.locator(sel)
            try:
                cnt = await loc.count()
            except Exception:
                cnt = 0
            kuaishou_logger.info(
                _msg("🪟", f"selector {sel!r} 命中数: {cnt}")
            )
            if cnt:
                modal = loc.last
                kuaishou_logger.info(_msg("✅", f"封面 modal 锁定: {sel}"))
                break

        if modal is None:
            try:
                shot_path = (
                    f"/tmp/sau_ks_modal_never_opened_{int(asyncio.get_event_loop().time())}.png"
                )
                await page.screenshot(path=shot_path, full_page=True)
                kuaishou_logger.error(
                    _msg("📸", f"4 种 selector 都没抓到封面 modal,现场: {shot_path}")
                )
            except Exception:
                pass
            raise RuntimeError("找不到封面 modal — 全部 selector 失效")

        if self.debug:
            try:
                shot_path = (
                    f"/tmp/sau_ks_cover_modal_open_{int(asyncio.get_event_loop().time())}.png"
                )
                await page.screenshot(path=shot_path, full_page=True)
                kuaishou_logger.info(_msg("📸", f"封面 modal 打开后截图: {shot_path}"))
            except Exception:
                pass

        # 切到"上传封面"tab。
        # 现状(2026-05 快手 UI):modal 默认在"封面截取"tab,需要主动切到"上传封面"
        # 才能露出本地文件 input;"封面截取"和"上传封面"两个 tab 各自有自己的 input。
        #
        # tab 元素可能在 `.ant-modal-content` 外面(antd 的 modal 内部结构会变),
        # 所以这里不绑 modal scope,直接 page scope 搜;也不用 exact 减少误判。
        tab_switched = False
        tab_strategies = [
            ("page.text(上传封面)",    page.get_by_text("上传封面")),
            ("page.text(本地上传)",    page.get_by_text("本地上传")),
            ("page.text(上传图片)",    page.get_by_text("上传图片")),
            ("page.role=tab(上传封面)", page.get_by_role("tab", name="上传封面")),
            ("modal.text(上传封面)",   modal.get_by_text("上传封面")),
        ]
        for name, loc in tab_strategies:
            try:
                cnt = await loc.count()
            except Exception:
                cnt = 0
            if not cnt:
                continue
            # 命中可能 > 1(同一段文字出现在不同祖先里),挨个尝试 click 直到一个成功
            for i in range(min(cnt, 5)):
                try:
                    await loc.nth(i).click(timeout=3000)
                    kuaishou_logger.info(
                        _msg("🗂️", f"切到 tab: {name} (nth={i}, 总命中={cnt})")
                    )
                    tab_switched = True
                    break
                except Exception as e:
                    kuaishou_logger.debug(
                        f"tab click {name}[{i}] 失败: {e}"
                    )
                    continue
            if tab_switched:
                break

        if not tab_switched:
            try:
                shot_path = (
                    f"/tmp/sau_ks_tab_not_found_{int(asyncio.get_event_loop().time())}.png"
                )
                await page.screenshot(path=shot_path, full_page=True)
                kuaishou_logger.error(
                    _msg("📸", f"找不到上传封面 tab,现场: {shot_path}")
                )
            except Exception:
                pass
            raise RuntimeError(
                "找不到「上传封面」tab — 不切 tab 会用错封面,这里直接 abort 别让脏数据发出去"
            )
        # 等"上传封面"面板渲染完
        await asyncio.sleep(2.0)

        # "上传封面" tab 不挂直接的 <input type=file>,而是一个"上传图片"按钮,
        # 点击触发系统 file chooser。Playwright 必须用 expect_file_chooser()
        # 接住,跟主流程上传视频用的模式一致(KSVideo.upload 顶部就是这么干的)。
        upload_btn_strategies = [
            ("page.text(上传图片)",     page.get_by_text("上传图片")),
            ("page.role=button(上传图片)", page.get_by_role("button", name="上传图片")),
            ("page.text(拖拽图片到此或点击上传)", page.get_by_text("拖拽图片到此或点击上传")),
        ]
        upload_btn = None
        chosen_strategy = None
        for name, loc in upload_btn_strategies:
            try:
                cnt = await loc.count()
            except Exception:
                cnt = 0
            if cnt:
                upload_btn = loc.last
                chosen_strategy = name
                kuaishou_logger.info(
                    _msg("📦", f"上传按钮锁定: {name} (命中={cnt})")
                )
                break
        if upload_btn is None:
            try:
                shot_path = (
                    f"/tmp/sau_ks_cover_no_input_{int(asyncio.get_event_loop().time())}.png"
                )
                await page.screenshot(path=shot_path, full_page=True)
                kuaishou_logger.error(_msg("📸", f"找不到上传按钮现场: {shot_path}"))
            except Exception:
                pass
            raise RuntimeError(
                "找不到「上传图片」按钮 — UI 可能又改了"
            )

        try:
            async with page.expect_file_chooser(timeout=15000) as fc_info:
                await upload_btn.click(timeout=5000)
            file_chooser = await fc_info.value
            await file_chooser.set_files(self.thumbnail_path)
            kuaishou_logger.info(
                _msg("🖼️", f"通过 file chooser 上传封面 (strategy: {chosen_strategy})")
            )
        except PlaywrightTimeoutError:
            # 兜底:有的版本是 hidden input,点击按钮不触发系统选择器
            # 此时还是有 input 挂在 DOM 上,只是 modal scope 没找到。换 page scope 找。
            kuaishou_logger.warning(
                _msg("⚠️", "file chooser 没弹出,fallback 到 page-scope input 查找")
            )
            file_input = page.locator(
                'input[type="file"][accept*="image"]'
            ).last
            try:
                await file_input.wait_for(state="attached", timeout=5000)
            except PlaywrightTimeoutError:
                file_input = page.locator('input[type="file"]').last
                await file_input.wait_for(state="attached", timeout=5000)
            await file_input.set_input_files(self.thumbnail_path)
            kuaishou_logger.info(_msg("🖼️", "通过 hidden input set_files 上传封面"))

        # 关键:等封面真的上传到 CDN 并渲染出预览,再点确认。
        # 否则 modal 关掉了但 server 那边什么都没收到 = 发布出去没封面。
        # 优先看 img preview 出现;退而求其次看"上传中"/loading 消失。
        preview_ok = False
        try:
            preview = modal.locator(
                'img[src^="http"], img[src^="data:"], img[src^="blob:"]'
            ).first
            await preview.wait_for(state="visible", timeout=20000)
            preview_ok = True
            kuaishou_logger.info(_msg("🧷", "封面预览图已渲染"))
        except PlaywrightTimeoutError:
            kuaishou_logger.warning(
                _msg("⚠️", "封面预览图没等到,等 loading 消失再点确认")
            )
        if not preview_ok:
            try:
                loading = modal.locator(
                    'text=/上传中|loading|加载中/'
                ).first
                if await loading.count():
                    await loading.wait_for(state="hidden", timeout=20000)
            except PlaywrightTimeoutError:
                pass

        # 给快手前端一点时间把"确认"按钮从 disabled 切到 enabled
        await asyncio.sleep(1.5)

        if self.debug:
            try:
                shot_path = (
                    f"/tmp/sau_ks_cover_modal_{int(asyncio.get_event_loop().time())}.png"
                )
                await page.screenshot(path=shot_path, full_page=True)
                kuaishou_logger.info(
                    _msg("📸", f"封面 modal 截图: {shot_path} (确认前)")
                )
            except Exception:
                pass

        # 找封面 modal 的"确认"按钮。优先从 modal scope 找,排除主页其他"确认"
        # 残留(比如发布栏的"确认发布")导致 .last 抓错隐藏元素。
        confirm_strategies = [
            ("modal.button:has-text(确认)",   modal.locator('button:has-text("确认")')),
            ("modal.role=button(确认)",       modal.get_by_role("button", name="确认", exact=True)),
            ("modal.text(确认)",              modal.get_by_text("确认", exact=True)),
            ("page button:visible:has-text(确认)", page.locator('button:visible:has-text("确认")')),
        ]
        confirm_button = None
        for name, loc in confirm_strategies:
            try:
                cnt = await loc.count()
            except Exception:
                cnt = 0
            if cnt:
                # modal scope 内通常只有 1 个"确认"(封面 modal 那个);
                # 兜底 page-scope 也用 .first 选第一个可见的,避免误选隐藏残留。
                confirm_button = loc.first
                kuaishou_logger.info(
                    _msg("🟢", f"确认按钮锁定: {name} (命中={cnt}, 取 first)")
                )
                break
        if confirm_button is None:
            try:
                shot_path = (
                    f"/tmp/sau_ks_cover_no_confirm_{int(asyncio.get_event_loop().time())}.png"
                )
                await page.screenshot(path=shot_path, full_page=True)
                kuaishou_logger.error(_msg("📸", f"找不到确认按钮: {shot_path}"))
            except Exception:
                pass
            raise RuntimeError("找不到封面 modal 的确认按钮")
        # 等 disabled 切走
        for _ in range(20):
            try:
                disabled = await confirm_button.get_attribute("disabled")
                aria_disabled = await confirm_button.get_attribute("aria-disabled")
            except Exception:
                break
            if disabled is None and aria_disabled not in ("true",):
                break
            await asyncio.sleep(0.5)

        # 用 force=True 强 click,绕过 Playwright 可点性判断;只要元素存在就点。
        # 之前 .last 选到隐藏元素的情况下会 timeout,这里强点能立刻给反馈。
        await confirm_button.click(force=True)
        kuaishou_logger.info(_msg("👆", "已 click 确认按钮 (force=True)"))

        # 给前端一点时间响应 click(modal 关 + 主页刷新 thumbnail)
        await asyncio.sleep(3)

        if self.debug:
            try:
                shot_path = (
                    f"/tmp/sau_ks_after_confirm_{int(asyncio.get_event_loop().time())}.png"
                )
                await page.screenshot(path=shot_path, full_page=True)
                kuaishou_logger.info(_msg("📸", f"点确认后截图: {shot_path}"))
            except Exception:
                pass

        # modal 是否真的关掉了 —— 是判断 click 有没有生效的最直接信号
        try:
            await modal.wait_for(state="hidden", timeout=15000)
            kuaishou_logger.info(_msg("✅", "封面 modal 已关闭"))
        except PlaywrightTimeoutError:
            kuaishou_logger.error(
                _msg(
                    "😵",
                    "click 后 15s modal 仍未关闭 — 上面那个'确认'按钮根本不是封面"
                    "modal 的(可能选到了其它元素)。这次发布大概率没封面!"
                )
            )

        # 主页缩略图 src 抓出来打印,人工判断是不是我们传的图。
        # 注意:即使我们没设封面成功,快手也会用视频首帧自动生成 thumb,所以
        # "有 img 元素"≠"封面是我们的"。看 src 才能确认。
        try:
            preview_on_page = page.locator(
                'xpath=//*[contains(text(),"封面设置")]/ancestor::*[self::div][2]//img'
            ).first
            src = await preview_on_page.get_attribute("src", timeout=8000)
            kuaishou_logger.info(_msg("🔍", f"主页缩略图 src: {src}"))
            # 我们传的是 PNG,快手保存后 URL 应该包含 'png' 或来自用户上传 CDN
            # (vs 视频首帧通常是 video tos 抽帧);这里只打印,不强判断
            if src and ("png" in src.lower() or "kos-" in src or "ks-photo" in src):
                kuaishou_logger.success(_msg("🥳", "封面看起来来自上传(包含 png/kos 标记)"))
            else:
                kuaishou_logger.warning(_msg(
                    "⚠️",
                    "封面 src 看起来不像我们传的图(可能是视频首帧抽出来的)。"
                    "去快手账号肉眼确认"
                ))
        except PlaywrightTimeoutError:
            kuaishou_logger.warning(_msg("⚠️", "主页缩略图 selector 没匹到"))

    async def upload(self, playwright: Playwright) -> None:
        kuaishou_logger.info(_msg("🧍", "小人先检查 cookie、视频文件、封面和发布时间"))
        await self.validate_upload_args()
        kuaishou_logger.info(_msg("🥳", "上传前检查通过"))

        # 关键参数全 dump,出问题时排查不用猜
        # (尤其是 thumbnail_path:None 还是路径,文件存不存在,体积多大)
        thumb_info = "无(快手将自动用视频首帧)"
        if self.thumbnail_path:
            tp = Path(self.thumbnail_path)
            if tp.exists():
                thumb_info = f"{tp} ({tp.stat().st_size} bytes)"
            else:
                thumb_info = f"{tp} (⚠️ 文件不存在)"
        kuaishou_logger.info(
            _msg(
                "📋",
                "本次上传参数: "
                f"title={self.title!r} | "
                f"file={self.file_path} | "
                f"thumbnail={thumb_info} | "
                f"tags={self.tags} | "
                f"desc={(self.desc or '')[:60]!r} | "
                f"publish_strategy={self.publish_strategy!r} | "
                f"publish_date={self.publish_date} | "
                f"account_file={self.account_file} | "
                f"headless={self.headless} | "
                f"debug={self.debug}",
            )
        )

        if self.local_executable_path:
            browser = await playwright.chromium.launch(
                headless=self.headless,
                executable_path=self.local_executable_path,
            )
        else:
            browser = await playwright.chromium.launch(
                headless=self.headless,
                channel="chrome",
            )
        context = await browser.new_context(storage_state=self.account_file)
        context = await set_init_script(context)

        upload_success = False
        try:
            page = await context.new_page()
            await page.goto(KUAISHOU_UPLOAD_URL)
            kuaishou_logger.info(_msg("🏃", f"小人开始搬运视频: {self.title}.mp4"))
            kuaishou_logger.info(_msg("🧭", "小人正在赶往快手上传主页"))
            await page.wait_for_url(KUAISHOU_UPLOAD_URL_PATTERN)

            upload_button = page.locator("button[class^='_upload-btn']")
            await upload_button.wait_for(state="visible", timeout=10000)

            async with page.expect_file_chooser() as fc_info:
                await upload_button.click()
            file_chooser = await fc_info.value
            await file_chooser.set_files(self.file_path)

            await asyncio.sleep(2)

            know_button = page.locator('button[type="button"] span:text("我知道了")').first
            try:
                if await know_button.count() and await know_button.is_visible():
                    await know_button.click()
            except Exception:
                pass

            await self.close_guide_overlay(page)

            kuaishou_logger.info(_msg("✍️", "小人开始填描述和话题"))
            await page.get_by_text("描述").locator("xpath=following-sibling::div").click()
            await page.keyboard.press("Backspace")
            await page.keyboard.press("Control+KeyA")
            await page.keyboard.press("Delete")
            await page.keyboard.type(self.desc or self.title)
            await page.keyboard.press("Enter")

            for index, tag in enumerate(self.tags[:3], start=1):
                kuaishou_logger.info(_msg("🏷️", f"小人正在添加第 {index} 个话题: #{tag}"))
                await page.keyboard.type(f"#{tag} ")
                await asyncio.sleep(2)

            max_retries = 60
            retry_count = 0
            while retry_count < max_retries:
                try:
                    number = await page.locator("text=上传中").count()
                    if number == 0:
                        kuaishou_logger.success(_msg("🥳", "视频已经传完啦"))
                        break

                    if retry_count % 5 == 0:
                        kuaishou_logger.info(_msg("🏃", "小人正在努力上传视频"))

                    if await page.locator("text=上传失败").count():
                        await self.handle_upload_error(page)

                    await asyncio.sleep(2)
                except Exception as exc:
                    kuaishou_logger.warning(_msg("😵", f"检查上传状态时出错，小人继续重试: {exc}"))
                    await asyncio.sleep(2)
                retry_count += 1

            if retry_count == max_retries:
                kuaishou_logger.warning(_msg("😵", "超过最大重试次数，视频上传可能未完成"))

            await self.set_thumbnail(page)

            if self.publish_strategy == KUAISHOU_PUBLISH_STRATEGY_SCHEDULED and self.publish_date != 0:
                await self.set_schedule_time(page, self.publish_date)

            while True:
                try:
                    publish_button = page.get_by_text("发布", exact=True)
                    if await publish_button.count() > 0:
                        await publish_button.click()

                    await asyncio.sleep(1)
                    confirm_button = page.get_by_text("确认发布")
                    if await confirm_button.count() > 0:
                        await confirm_button.click()

                    await page.wait_for_url(KUAISHOU_MANAGE_URL_PATTERN, timeout=5000)
                    kuaishou_logger.success(_msg("🥳", "视频发布成功，小人开心收工"))
                    break
                except Exception as exc:
                    kuaishou_logger.info(_msg("🏃", f"小人正在冲刺发布视频: {exc}"))
                    if self.debug:
                        await page.screenshot(full_page=True)
                    await asyncio.sleep(1)

            upload_success = True
        finally:
            if upload_success:
                await context.storage_state(path=self.account_file)
                kuaishou_logger.success(_msg("🥳", "cookie 更新完毕"))
                await asyncio.sleep(2)
            await context.close()
            await browser.close()

    async def main(self):
        async with async_playwright() as playwright:
            await self.upload(playwright)


class KSNote(KSBaseUploader):
    def __init__(
        self,
        image_paths,
        note,
        tags,
        publish_date: datetime | int,
        account_file,
        title: str | None = None,
        publish_strategy: str | None = None,
        debug: bool = DEBUG_MODE,
        headless: bool = LOCAL_CHROME_HEADLESS,
    ):
        super().__init__(
            publish_date=publish_date,
            account_file=account_file,
            publish_strategy=publish_strategy,
            debug=debug,
            headless=headless,
        )
        self.image_paths = image_paths
        self.note = note or ""
        self.title = title or (self.note[:20] if self.note else "")
        self.tags = tags or []

    async def validate_upload_args(self):
        await self.validate_base_args()
        if not self.title or not str(self.title).strip():
            raise ValueError("快手图文上传时，title 是必须的")
        if not self.image_paths:
            raise ValueError("快手图文上传时，图片是必须的")

        if isinstance(self.image_paths, (str, Path)):
            self.image_paths = [self.image_paths]

        normalized_image_paths = []
        for image_path in self.image_paths:
            normalized_image_paths.append(str(self.validate_image_file(image_path)))
        self.image_paths = normalized_image_paths

    async def upload_note_content(self, page: Page) -> None:
        kuaishou_logger.info(_msg("🏃", f"小人开始搬运图文，共 {len(self.image_paths)} 张图片"))
        kuaishou_logger.info(_msg("🔀", "小人正在切换到图文发布"))
        await page.locator('div[role="tablist"] div[role="tab"]:has-text("图文")').click()
        await page.wait_for_timeout(1000)

        kuaishou_logger.info(_msg("📤", "小人正在上传图片"))
        upload_button = page.locator("button[class^='_upload-btn']").filter(has_text="上传图片")
        await upload_button.wait_for(state="visible", timeout=10000)

        async with page.expect_file_chooser() as fc_info:
            await upload_button.click()
        file_chooser = await fc_info.value
        await file_chooser.set_files(self.image_paths)

        know_button = page.locator('button[type="button"] span:text("我知道了")').first
        try:
            if await know_button.count() and await know_button.is_visible():
                await know_button.click()
        except Exception:
            pass

        await self.close_guide_overlay(page)

        kuaishou_logger.info(_msg("✍️", "小人开始填写图文内容和话题"))
        await page.get_by_text("描述").locator("xpath=following-sibling::div").click()
        await page.keyboard.press("Backspace")
        await page.keyboard.press("Control+KeyA")
        await page.keyboard.press("Delete")
        await page.keyboard.type(self.note)
        await page.keyboard.press("Enter")

        for index, tag in enumerate(self.tags[:3], start=1):
            kuaishou_logger.info(_msg("🏷️", f"小人正在添加第 {index} 个话题: #{tag}"))
            await page.keyboard.type(f"#{tag} ")
            await asyncio.sleep(2)

        max_retries = 60
        retry_count = 0
        while retry_count < max_retries:
            try:
                number = await page.locator("text=上传中").count()
                if number == 0:
                    kuaishou_logger.success(_msg("🥳", "图文素材已经传完啦"))
                    break

                if retry_count % 5 == 0:
                    kuaishou_logger.info(_msg("🏃", "小人正在努力上传图文素材"))

                if await page.locator("text=上传失败").count():
                    kuaishou_logger.warning(_msg("😵", "图文素材上传摔了一跤，小人马上重新上传"))
                    await page.locator('div.progress-div [class^="upload-btn-input"]').set_input_files(self.image_paths)

                await asyncio.sleep(2)
            except Exception as exc:
                kuaishou_logger.warning(_msg("😵", f"检查图文上传状态时出错，小人继续重试: {exc}"))
                await asyncio.sleep(2)
            retry_count += 1

        if retry_count == max_retries:
            kuaishou_logger.warning(_msg("😵", "超过最大重试次数，图文上传可能未完成"))

        if self.publish_strategy == KUAISHOU_PUBLISH_STRATEGY_SCHEDULED and self.publish_date != 0:
            await self.set_schedule_time(page, self.publish_date)

        while True:
            try:
                publish_button = page.get_by_text("发布", exact=True)
                if await publish_button.count() > 0:
                    await publish_button.click()

                await asyncio.sleep(1)
                confirm_button = page.get_by_text("确认发布")
                if await confirm_button.count() > 0:
                    await confirm_button.click()

                await page.wait_for_url(KUAISHOU_MANAGE_URL_PATTERN, timeout=5000)
                kuaishou_logger.success(_msg("🥳", "图文发布成功，小人开心收工"))
                break
            except Exception as exc:
                kuaishou_logger.info(_msg("🏃", f"小人正在冲刺发布图文: {exc}"))
                if self.debug:
                    await page.screenshot(full_page=True)
                await asyncio.sleep(1)

    async def upload(self, playwright: Playwright) -> None:
        kuaishou_logger.info(_msg("🧍", "小人先检查 cookie、图片和发布时间"))
        await self.validate_upload_args()
        kuaishou_logger.info(_msg("🥳", "图文上传前检查通过"))

        if self.local_executable_path:
            browser = await playwright.chromium.launch(
                headless=self.headless,
                executable_path=self.local_executable_path,
            )
        else:
            browser = await playwright.chromium.launch(
                headless=self.headless,
                channel="chrome",
            )
        context = await browser.new_context(storage_state=self.account_file)
        context = await set_init_script(context)

        upload_success = False
        try:
            page = await context.new_page()
            await page.goto(KUAISHOU_UPLOAD_URL)
            kuaishou_logger.info(_msg("🧭", "小人正在赶往快手图文发布页"))
            await page.wait_for_url(KUAISHOU_UPLOAD_URL_PATTERN)

            await self.upload_note_content(page)
            upload_success = True
        finally:
            if upload_success:
                await context.storage_state(path=self.account_file)
                kuaishou_logger.success(_msg("🥳", "cookie 更新完毕"))
                await asyncio.sleep(2)
            await context.close()
            await browser.close()

    async def main(self):
        async with async_playwright() as playwright:
            await self.upload(playwright)
