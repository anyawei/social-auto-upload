# -*- coding: utf-8 -*-
from datetime import datetime

import asyncio
import inspect
import os
from pathlib import Path

from patchright.async_api import Page
from patchright.async_api import Playwright
from patchright.async_api import TimeoutError as PlaywrightTimeoutError
from patchright.async_api import async_playwright

from conf import DEBUG_MODE, LOCAL_CHROME_HEADLESS, LOCAL_CHROME_PATH
from uploader.base_video import BaseVideoUploader
from utils.base_social_media import set_init_script
from utils.login_qrcode import build_login_qrcode_path
from utils.login_qrcode import decode_qrcode_from_path
from utils.login_qrcode import print_terminal_qrcode
from utils.login_qrcode import remove_qrcode_file
from utils.login_qrcode import save_data_url_image
from utils.log import douyin_logger

DOUYIN_PUBLISH_STRATEGY_IMMEDIATE = "immediate"
DOUYIN_PUBLISH_STRATEGY_SCHEDULED = "scheduled"


def _msg(emoji: str, text: str) -> str:
    return f"{emoji} {text}"


async def _emit_qrcode_callback(qrcode_callback, payload: dict):
    if not qrcode_callback:
        return

    callback_result = qrcode_callback(payload)
    if inspect.isawaitable(callback_result):
        await callback_result


def _build_login_result(success: bool, status: str, message: str, account_file: str, qrcode: dict | None = None, current_url: str = "") -> dict:
    return {
        "success": success,
        "status": status,
        "message": message,
        "account_file": str(account_file),
        "qrcode": qrcode,
        "current_url": current_url,
    }


async def cookie_auth(account_file):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True, channel="chrome")
        try:
            context = await browser.new_context(storage_state=account_file)
            context = await set_init_script(context)
            page = await context.new_page()
            await page.goto("https://creator.douyin.com/creator-micro/content/upload")
            try:
                await page.wait_for_url("https://creator.douyin.com/creator-micro/content/upload", timeout=5000)
            except Exception:
                return False

            if await page.get_by_text("手机号登录").count() or await page.get_by_text("扫码登录").count():
                return False

            return True
        finally:
            await browser.close()


async def douyin_setup(account_file, handle=False, return_detail=False, qrcode_callback=None, headless: bool = LOCAL_CHROME_HEADLESS):
    if not os.path.exists(account_file) or not await cookie_auth(account_file):
        if not handle:
            result = _build_login_result(False, "cookie_invalid", "cookie文件不存在或已失效", account_file)
            return result if return_detail else False
        douyin_logger.info(_msg("🥹", "cookie 失效了，准备打开浏览器重新登录"))
        result = await douyin_cookie_gen(account_file, qrcode_callback=qrcode_callback, headless=headless)
        return result if return_detail else result["success"]

    result = _build_login_result(True, "cookie_valid", "cookie有效", account_file)
    return result if return_detail else True


async def _extract_douyin_qrcode_src(page: Page) -> str:
    scan_login_tab = page.get_by_text("扫码登录", exact=True).first
    await scan_login_tab.wait_for(timeout=30000)

    qrcode_img = (
        scan_login_tab
        .locator("..")
        .locator("xpath=following-sibling::div[1]")
        .locator('img[aria-label="二维码"]')
        .first
    )

    if not await qrcode_img.count():
        qrcode_img = page.get_by_role("img", name="二维码").first

    await qrcode_img.wait_for(state="visible", timeout=30000)
    src = await qrcode_img.get_attribute("src")
    if not src:
        raise RuntimeError("未获取到抖音登录二维码地址")

    return src


async def _save_douyin_qrcode(page: Page, account_file: str, previous_qrcode_path: Path | None = None, qrcode_callback=None) -> dict:
    qrcode_src = await _extract_douyin_qrcode_src(page)
    qrcode_path = save_data_url_image(qrcode_src, build_login_qrcode_path(account_file))
    if previous_qrcode_path and previous_qrcode_path != qrcode_path:
        if remove_qrcode_file(previous_qrcode_path):
            douyin_logger.info(_msg("🧹", f"临时二维码文件已清理: {previous_qrcode_path}"))
    douyin_logger.info(_msg("🖼️", f"二维码已经准备好啦，已保存到: {qrcode_path}"))
    qrcode_content = decode_qrcode_from_path(qrcode_path)
    if qrcode_content:
        print_terminal_qrcode(qrcode_content, qrcode_path, "抖音APP")
    else:
        douyin_logger.warning(_msg("😵", f"终端没法完整显示二维码，请打开 {qrcode_path} 扫码"))
    qrcode_info = {
        "image_path": str(qrcode_path),
        "image_data_url": qrcode_src,
    }
    await _emit_qrcode_callback(qrcode_callback, qrcode_info)
    return qrcode_info


async def _is_douyin_login_completed(page: Page) -> bool:
    if not page.url.startswith("https://creator.douyin.com/creator-micro/home"):
        return False

    login_markers = [
        page.get_by_text("扫码登录", exact=True).first,
        page.get_by_text("手机号登录", exact=True).first,
        page.get_by_text("二维码失效", exact=True).first,
        page.get_by_role("img", name="二维码").first,
    ]

    for marker in login_markers:
        if not await marker.count():
            continue
        try:
            if await marker.is_visible():
                return False
        except Exception:
            continue

    return True


async def _wait_for_douyin_login(page: Page, account_file: str, qrcode_info: dict, qrcode_callback=None, poll_interval: int = 3, max_checks: int = 100) -> dict:
    qrcode_path = Path(qrcode_info["image_path"])
    for _ in range(max_checks):
        if await _is_douyin_login_completed(page):
            douyin_logger.info(_msg("🥳", f"扫码成功，已经跳转到登录后页面: {page.url}"))
            return _build_login_result(True, "success", "抖音扫码登录成功", account_file, qrcode_info, page.url)

        expired_box = page.get_by_text("二维码失效", exact=True).locator("..").first
        if await expired_box.count() and await expired_box.is_visible():
            douyin_logger.warning(_msg("😵", "二维码失效了，小人马上去刷新"))
            await expired_box.click()
            await asyncio.sleep(1)
            qrcode_info = await _save_douyin_qrcode(page, account_file, qrcode_path, qrcode_callback=qrcode_callback)
            qrcode_path = Path(qrcode_info["image_path"])

        await asyncio.sleep(poll_interval)

    return _build_login_result(False, "timeout", "等待抖音扫码登录超时", account_file, qrcode_info, page.url)


async def douyin_cookie_gen(
    account_file,
    qrcode_callback=None,
    poll_interval: int = 3,
    max_checks: int = 100,
    headless: bool = LOCAL_CHROME_HEADLESS,
):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=headless, channel="chrome")
        context = await browser.new_context()
        context = await set_init_script(context)
        qrcode_path = None
        result = _build_login_result(False, "failed", "抖音登录失败", account_file)
        try:
            page = await context.new_page()
            await page.goto("https://creator.douyin.com/")
            qrcode_info = await _save_douyin_qrcode(page, account_file, qrcode_callback=qrcode_callback)
            qrcode_path = Path(qrcode_info["image_path"])
            douyin_logger.info(_msg("🧍", "请扫码，小人正在耐心等待登录完成"))
            result = await _wait_for_douyin_login(
                page,
                account_file,
                qrcode_info,
                qrcode_callback=qrcode_callback,
                poll_interval=poll_interval,
                max_checks=max_checks,
            )
            if result["success"]:
                await asyncio.sleep(2)
                await context.storage_state(path=account_file)
                if not await cookie_auth(account_file):
                    result = _build_login_result(
                        False,
                        "cookie_invalid",
                        "抖音扫码流程结束，但 cookie 校验失败",
                        account_file,
                        qrcode_info,
                        page.url,
                    )
        except Exception as exc:
            result = _build_login_result(False, "failed", str(exc), account_file, current_url=page.url if "page" in locals() else "")
        finally:
            if remove_qrcode_file(qrcode_path):
                douyin_logger.info(_msg("🧹", f"临时二维码文件已清理: {qrcode_path}"))
            if not result["success"]:
                douyin_logger.error(_msg("😢", f"登录失败: {result['message']}"))
            await context.close()
            await browser.close()
        return result


class DouYinBaseUploader(BaseVideoUploader):
    def __init__(
        self,
        publish_date: datetime | int,
        account_file,
        publish_strategy: str = DOUYIN_PUBLISH_STRATEGY_IMMEDIATE,
        debug: bool = DEBUG_MODE,
        headless: bool = LOCAL_CHROME_HEADLESS,
    ):
        self.publish_date = publish_date
        self.account_file = account_file
        self.publish_strategy = publish_strategy
        self.debug = debug
        self.date_format = "%Y年%m月%d日 %H:%M"
        self.local_executable_path = LOCAL_CHROME_PATH
        self.headless = headless

    async def validate_base_args(self):
        if not os.path.exists(self.account_file):
            raise RuntimeError(f"cookie文件不存在，请先完成抖音登录: {self.account_file}")
        if not await cookie_auth(self.account_file):
            raise RuntimeError(f"cookie文件已失效，请先完成抖音登录: {self.account_file}")
        if self.publish_strategy not in {DOUYIN_PUBLISH_STRATEGY_IMMEDIATE, DOUYIN_PUBLISH_STRATEGY_SCHEDULED}:
            raise ValueError(f"不支持的发布策略: {self.publish_strategy}")

        if self.publish_strategy == DOUYIN_PUBLISH_STRATEGY_SCHEDULED:
            self.publish_date = self.validate_publish_date(self.publish_date)
        else:
            self.publish_date = 0

    async def set_schedule_time_douyin(self, page, publish_date):
        label_element = page.locator("[class^='radio']:has-text('定时发布')")
        await label_element.click()
        await asyncio.sleep(1)
        publish_date_hour = publish_date.strftime("%Y-%m-%d %H:%M")

        await asyncio.sleep(1)
        await page.locator('.semi-input[placeholder="日期和时间"]').click()
        await page.keyboard.press("Control+KeyA")
        await page.keyboard.type(str(publish_date_hour))
        await page.keyboard.press("Enter")
        await asyncio.sleep(1)

    async def fill_title_and_description(self, page: Page, title: str, description: str, tags: list[str] | None = None):
        description_section = (
            page.get_by_text("作品描述", exact=True)
            .locator("xpath=ancestor::div[2]")
            .locator("xpath=following-sibling::div[1]")
        )

        title_input = description_section.locator('input[type="text"]').first
        await title_input.wait_for(state="visible", timeout=10000)
        await title_input.fill(title[:30])

        description_editor = description_section.locator('.zone-container[contenteditable="true"]').first
        await description_editor.wait_for(state="visible", timeout=10000)
        await description_editor.click()
        await page.keyboard.press("Control+KeyA")
        await page.keyboard.press("Delete")
        await page.keyboard.type(description)

        for tag in tags or []:
            await page.keyboard.type(" #" + tag)
            await page.keyboard.press("Space")

        # 关掉话题候选 dropdown(publish-mention-wrapper) — 不关会拦截后续 "选择封面" 等点击
        try:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(200)
        except Exception:
            pass
        # 失焦 — 点击页面顶部安全区域(标题输入框上方空白)
        try:
            await title_input.blur()
        except Exception:
            pass
        # 兜底:JS 直接删 mention wrapper
        try:
            await page.evaluate(
                """
                () => {
                    document.querySelectorAll(
                        '[class*=publish-mention-wrapper], [class*=mention-wrapper], [class*=tag-hash]'
                    ).forEach(el => {
                        // 只删浮层(position fixed/absolute),保留内联 inline 用的 hash 标签
                        const pos = window.getComputedStyle(el).position;
                        if (pos === 'fixed' || pos === 'absolute') el.remove();
                    });
                }
                """
            )
        except Exception:
            pass
        await page.wait_for_timeout(300)

    async def set_location(self, page: Page, location: str = ""):
        if not location:
            return
        await page.locator('div.semi-select span:has-text("输入地理位置")').click()
        await page.keyboard.press("Backspace")
        await page.wait_for_timeout(2000)
        await page.keyboard.type(location)
        await page.wait_for_selector('div[role="listbox"] [role="option"]', timeout=5000)
        await page.locator('div[role="listbox"] [role="option"]').first.click()

    async def handle_product_dialog(self, page: Page, product_title: str):
        await page.wait_for_timeout(2000)
        await page.wait_for_selector('input[placeholder="请输入商品短标题"]', timeout=10000)
        short_title_input = page.locator('input[placeholder="请输入商品短标题"]')
        if not await short_title_input.count():
            douyin_logger.error(_msg("😵", "没找到商品短标题输入框"))
            return False

        product_title = product_title[:10]
        await short_title_input.fill(product_title)
        await page.wait_for_timeout(1000)

        finish_button = page.locator('button:has-text("完成编辑")')
        if "disabled" not in await finish_button.get_attribute("class"):
            await finish_button.click()
            douyin_logger.debug(_msg("🥳", "已点击“完成编辑”按钮"))
            await page.wait_for_selector(".semi-modal-content", state="hidden", timeout=5000)
            return True

        douyin_logger.error(_msg("😵", "“完成编辑”按钮是灰的，小人先把弹窗关掉"))
        cancel_button = page.locator('button:has-text("取消")')
        if await cancel_button.count():
            await cancel_button.click()
        else:
            close_button = page.locator(".semi-modal-close")
            await close_button.click()
        await page.wait_for_selector(".semi-modal-content", state="hidden", timeout=5000)
        return False

    async def set_product_link(self, page: Page, product_link: str, product_title: str):
        await page.wait_for_timeout(2000)
        try:
            await page.wait_for_selector("text=添加标签", timeout=10000)
            dropdown = page.get_by_text("添加标签").locator("..").locator("..").locator("..").locator(".semi-select").first
            if not await dropdown.count():
                douyin_logger.error(_msg("😵", "没找到标签下拉框"))
                return False
            douyin_logger.debug(_msg("🧍", "找到标签下拉框，小人准备选择“购物车”"))
            await dropdown.click()
            await page.wait_for_selector('[role="listbox"]', timeout=5000)
            await page.locator('[role="option"]:has-text("购物车")').click()
            douyin_logger.debug(_msg("🥳", "已经选中“购物车”"))

            await page.wait_for_selector('input[placeholder="粘贴商品链接"]', timeout=5000)
            input_field = page.locator('input[placeholder="粘贴商品链接"]')
            await input_field.fill(product_link)
            douyin_logger.debug(_msg("🔗", f"商品链接已经填好了: {product_link}"))

            add_button = page.locator('span:has-text("添加链接")')
            button_class = await add_button.get_attribute("class")
            if "disable" in button_class:
                douyin_logger.error(_msg("😵", "“添加链接”按钮现在点不了"))
                return False
            await add_button.click()
            douyin_logger.debug(_msg("🥳", "已点击“添加链接”按钮"))

            await page.wait_for_timeout(2000)
            error_modal = page.locator("text=未搜索到对应商品")
            if await error_modal.count():
                confirm_button = page.locator('button:has-text("确定")')
                await confirm_button.click()
                douyin_logger.error(_msg("😢", "这个商品链接无效"))
                return False

            if not await self.handle_product_dialog(page, product_title):
                return False

            douyin_logger.debug(_msg("🥳", "商品链接设置好了"))
            return True
        except Exception as e:
            douyin_logger.error(_msg("😢", f"设置商品链接时出错: {str(e)}"))
            return False


class DouYinVideo(DouYinBaseUploader):
    def __init__(
        self,
        title,
        file_path,
        tags,
        publish_date: datetime | int,
        account_file,
        thumbnail_landscape_path=None,
        productLink="",
        productTitle="",
        thumbnail_portrait_path=None,
        desc: str | None = None,
        publish_strategy: str = DOUYIN_PUBLISH_STRATEGY_IMMEDIATE,
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
        self.title = title
        self.file_path = file_path
        self.tags = tags
        self.thumbnail_landscape_path = thumbnail_landscape_path
        self.thumbnail_portrait_path = thumbnail_portrait_path
        self.productLink = productLink
        self.productTitle = productTitle
        self.desc = desc or ""

    async def validate_upload_args(self):
        await self.validate_base_args()
        if not self.title or not str(self.title).strip():
            raise ValueError("视频模式下，title 是必须的")

        self.file_path = str(self.validate_video_file(self.file_path))
        if self.thumbnail_landscape_path:
            self.thumbnail_landscape_path = str(self.validate_image_file(self.thumbnail_landscape_path))
        if self.thumbnail_portrait_path:
            self.thumbnail_portrait_path = str(self.validate_image_file(self.thumbnail_portrait_path))

    async def handle_upload_error(self, page):
        douyin_logger.warning(_msg("😵", "视频上传摔了一跤，小人马上重新上传"))
        await page.locator('div.progress-div [class^="upload-btn-input"]').set_input_files(self.file_path)

    async def dismiss_shepherd_guides(self, page: Page, max_iterations: int = 8) -> int:
        """关闭抖音创作中心的 shepherd-js 引导浮窗。

        抖音创作平台经常引入新功能引导(如"共创中心"、"新创作功能"等),
        这种 shepherd 组件挂在 body 上,会拦截 pointer events 导致后续 click 卡死。
        本函数:
        1) 优先点 shepherd primary button(下一步 / 知道了 / 完成 / 跳过)逐步推进
        2) 退而求其次找 X 关闭按钮
        3) 兜底用 JS 直接删 shepherd 节点 + 遮罩

        Returns: 累计关闭的引导步数,debug 用。
        """
        dismissed_count = 0
        dismiss_selectors = [
            ".shepherd-element.shepherd-enabled .shepherd-button.shepherd-button-primary",
            ".shepherd-element.shepherd-enabled button:has-text('知道了')",
            ".shepherd-element.shepherd-enabled button:has-text('我知道了')",
            ".shepherd-element.shepherd-enabled button:has-text('完成')",
            ".shepherd-element.shepherd-enabled button:has-text('跳过')",
            ".shepherd-element.shepherd-enabled .shepherd-cancel-icon",
            ".shepherd-element.shepherd-enabled [class*='close']",
        ]
        for _ in range(max_iterations):
            still_present = 0
            try:
                still_present = await page.locator(".shepherd-element.shepherd-enabled").count()
            except Exception:
                pass
            if still_present == 0:
                break

            clicked = False
            for sel in dismiss_selectors:
                try:
                    loc = page.locator(sel).first
                    if await loc.count() and await loc.is_visible():
                        await loc.click(timeout=1000, force=True)
                        dismissed_count += 1
                        clicked = True
                        await page.wait_for_timeout(300)
                        break
                except Exception:
                    continue

            if clicked:
                continue

            # 兜底:DOM 移除(适用于无可点按钮的引导浮窗)
            try:
                removed = await page.evaluate(
                    """
                    () => {
                        const nodes = document.querySelectorAll(
                            '.shepherd-element, .shepherd-modal-overlay-container'
                        );
                        const n = nodes.length;
                        nodes.forEach(el => el.remove());
                        return n;
                    }
                    """
                )
                if removed:
                    dismissed_count += removed
            except Exception:
                pass
            await page.wait_for_timeout(200)

        if dismissed_count:
            douyin_logger.info(_msg("🧹", f"已关闭 {dismissed_count} 个引导浮窗 / 遮罩"))
        return dismissed_count

    async def handle_auto_video_cover(self, page):
        if await page.get_by_text("请设置封面后再发布").first.is_visible():
            douyin_logger.info(_msg("🧍", "发布前还得先把封面弄好"))
            recommend_cover = page.locator('[class^="recommendCover-"]').first
            if await recommend_cover.count():
                douyin_logger.info(_msg("🏃", "小人去选第一个推荐封面"))
                try:
                    await recommend_cover.click()
                    await asyncio.sleep(1)
                    confirm_text = "是否确认应用此封面？"
                    if await page.get_by_text(confirm_text).first.is_visible():
                        douyin_logger.info(_msg("🪟", f"弹出确认框了: {confirm_text}"))
                        await page.get_by_role("button", name="确定").click()
                        douyin_logger.info(_msg("🥳", "推荐封面已经应用"))
                        await asyncio.sleep(1)
                    douyin_logger.info(_msg("🥳", "封面选择流程完成"))
                    return True
                except Exception as e:
                    douyin_logger.warning(_msg("😵", f"推荐封面没选成功: {e}"))
        return False

    async def set_thumbnail(self, page: Page):
        """**推荐方法**:别传 --thumbnail,改用 ffmpeg 把封面图 prepend 0.5s 到视频前面
        (memory: feedback_dy_cover_via_first_frame)。 抖音 mobile 自动用视频首帧作竖封面。

        本函数(PC modal 流程)在 2026-05 抖音 UI 下还没完全跑通(三种 modal 状态并存 + 完整 modal AI 异步),
        包了 try/except 让它失败时 silent 跳过,不阻断发布。"""
        try:
            await self._set_thumbnail_inner(page)
        except Exception as e:
            douyin_logger.warning(_msg("⚠️", f"封面 modal 流程失败(silent skip): {e}"))
            douyin_logger.warning(_msg("💡", "建议:用 ffmpeg 把封面图 prepend 0.5s 到视频前,不传 --thumbnail"))

    async def _set_thumbnail_inner(self, page: Page):
        """新版抖音 PC 封面流程(2026-05-22 重写,验证过)。

        真实 UI 是 **两层**:
        - inline 区(publish 主表单内的"封面"块,包含 AI 推荐封面缩略图 + "选择封面"按钮)
        - modal(点 inline "选择封面" 触发):title "设置竖封面/设置横封面" + 大 ReactCrop crop area
          + "+ 上传封面"按钮 + 底部 "封面检测/完成/设置横封面" 按钮组

        之前的实现错误:把 inline 区当 modal,fallback set_input_files 灌到 inline 的 hidden input(它对应 AI 推荐或没绑定),
        modal 的 ReactCrop 区始终空着(黑色),最后保存的就是这个黑色 → **黑屏封面**。

        正确流程:
        1. 点 inline 区的 "选择封面" 按钮 → modal 打开
        2. modal 默认在 "设置竖封面" tab,点 "+ 上传封面" → expect_file_chooser → set_files
        3. 验证 ReactCrop__image naturalWidth > 0(真实图加载到 crop)
        4. 点底部 "设置横封面" 按钮 → tab 切到横封面
        5. 同样:"+ 上传封面" → file_chooser → set_files → 验证
        6. 点底部 "完成" 按钮 → modal 关 + 封面 commit
        """
        if not self.thumbnail_landscape_path and not self.thumbnail_portrait_path:
            return

        douyin_logger.info(_msg("🏃", "小人正在设置视频封面"))

        # 新版抖音封面流程:
        # 1. "选择封面" 点击触发了 modal,modal title 是 "设置封面"
        # 2. modal 有 tab 切换"竖封面3:4 / 横封面4:3"
        # 3. modal 内有 input[type="file"][accept*="image"],父级 cropUploadWrapper
        # 4. 还有 radio "使用原视频封面 / 上传新封面",我们要切到"上传新封面"才能让 file input 生效
        # 5. 设置好封面后,modal 有"完成"按钮关闭

        thumb_path = self.thumbnail_landscape_path or self.thumbnail_portrait_path
        if not thumb_path:
            return

        async def _trigger_card_input(tab_text: str) -> bool:
            """对指定 cover 卡(tab_text='竖封面3:4'/'横封面4:3'), Playwright 真实 click 卡内的 "选择封面" 按钮,
            **不接 file_chooser** — 因为我们要先让卡内 input 渲染出来,然后再去 set_files。

            为啥不直接 expect_file_chooser:抖音的 PC stealth 检测对 expect_file_chooser 不友好,
            click 后 dispatchEvent('click') 触发 React handler,直接渲染 input。
            """
            # Playwright locator scope 到对应卡片(coverControl div),click 卡内的 "选择封面"
            card_idx = await page.evaluate(
                """(tabText) => {
                    const tip = Array.from(document.querySelectorAll('div[class*="cover-tip"]'))
                        .find(e => (e.textContent || '').trim() === tabText);
                    if (!tip) return -1;
                    let card = tip;
                    while (card && !(card.className && card.className.toString().includes('coverControl'))) {
                        card = card.parentElement;
                    }
                    if (!card) return -1;
                    card.scrollIntoView({ block: 'center' });
                    return Array.from(document.querySelectorAll('div[class*="coverControl"]')).indexOf(card);
                }""",
                tab_text,
            )
            if card_idx < 0:
                return False
            card = page.locator('div[class*="coverControl"]').nth(card_idx)
            choose = card.get_by_text("选择封面", exact=True).first
            if not await choose.count():
                douyin_logger.warning(_msg("⚠️", f"'{tab_text}' 卡内无'选择封面'文字"))
                return False
            try:
                await choose.click(timeout=3000, force=True)
                douyin_logger.info(_msg("🗂️", f"click '{tab_text}' 卡内 '选择封面' (cardIdx={card_idx})"))
                await page.wait_for_timeout(1000)
                return True
            except Exception as e:
                douyin_logger.warning(_msg("⚠️", f"click '{tab_text}' '选择封面' 失败: {e}"))
                return False

        async def _activate_cover_card(tab_text: str) -> None:
            """通过 click 卡内 '选择封面' 让该卡的 input 渲染到 DOM,然后再 set_files 可以走到对应封面的 modal。"""
            await _trigger_card_input(tab_text)

        # 1) 触发竖封面 modal — 先 click 切换 active 卡到 "竖封面3:4",再 set_files。
        # 关键事实(从 b0wyctu7t run dump 出来):
        # - inline 区只有 1 个 image input(竖/横共用)
        # - modal 的 crop 比例由当前 active 卡决定(竖卡 -> 3:4, 横卡 -> 4:3)
        # - 要 set 竖封面就要先把竖封面卡 click 成 active,然后 set_files 触发 modal 弹出
        await _activate_cover_card("竖封面3:4")

        cover_inputs = page.locator('input[type="file"][accept*="image"]')
        if not await cover_inputs.count():
            raise RuntimeError("找不到 inline 封面 input — UI 可能变了")
        try:
            await cover_inputs.first.set_input_files(thumb_path)
            douyin_logger.info(_msg("🔘", f"set_input_files 触发竖封面 modal 打开"))
        except Exception as e:
            raise RuntimeError(f"set_input_files (竖) 失败: {e}")

        # 等 modal 真正打开 — 信号:页面有 "+ 上传封面" 按钮 / "设置竖封面" tab / 大 ReactCrop 区
        modal_open_signal = None
        for tries in range(20):
            for sig_sel in [
                ('text="上传封面"',     '上传封面 按钮'),
                ('text="设置竖封面"',   '设置竖封面 tab'),
                ('text="设置横封面"',   '设置横封面 tab'),
                ('.ReactCrop__image',   'ReactCrop 已加载'),
            ]:
                try:
                    if await page.locator(sig_sel[0]).count():
                        modal_open_signal = sig_sel[1]
                        break
                except Exception:
                    pass
            if modal_open_signal:
                break
            await page.wait_for_timeout(300)
        if not modal_open_signal:
            if self.debug:
                try:
                    shot = f"/tmp/sau_dy_modal_never_opened_{int(asyncio.get_event_loop().time())}.png"
                    await page.screenshot(path=shot, full_page=True)
                    douyin_logger.error(_msg("📸", f"6s modal 没打开现场: {shot}"))
                except Exception:
                    pass
            raise RuntimeError("set_input_files 后 modal 6s 内没打开 — UI 可能又变了")
        douyin_logger.info(_msg("📦", f"封面 modal 已打开 (信号: {modal_open_signal})"))
        await page.wait_for_timeout(1500)

        # 2) 抖音 "设置封面" 主 modal 内有 2 张并排卡片("竖封面3:4" + "横封面4:3"),
        # 每张卡片上传后会弹一个嵌套裁剪 modal (semi-portal + ReactCrop),
        # 必须在裁剪 modal 内点"确认"关掉,才能继续操作下一张。
        # 不关掉裁剪 modal 直接切下一卡片 → 裁剪未提交 → 封面是黑屏。

        async def _confirm_crop_modal() -> bool:
            """裁剪 modal (含 ReactCrop) 出现时点其内部的确认按钮,等它关闭。"""
            # 等 ReactCrop 出现(上传完成后 2-5s 内)
            try:
                await page.wait_for_selector('.ReactCrop__image', state='visible', timeout=6000)
            except Exception:
                return False  # 没出现裁剪 modal,跳过
            douyin_logger.debug(_msg("✂️", "裁剪 modal 出现,找确认按钮"))

            # 找裁剪 modal 内部的确认按钮(必须限定在含 ReactCrop 的 portal 内,避免误点主 modal 保存)
            confirm_texts = ["确认", "确定", "完成", "应用", "裁剪", "保存"]
            for confirm_text in confirm_texts:
                for sel in [
                    f'.semi-portal:has(.ReactCrop__image) button:visible:has-text("{confirm_text}")',
                    f'div[role="modal"]:has(.ReactCrop__image) button:visible:has-text("{confirm_text}")',
                    f'div:has(> .ReactCrop) button:visible:has-text("{confirm_text}")',
                ]:
                    try:
                        btn = page.locator(sel).first
                        if await btn.count() and await btn.is_visible():
                            await btn.click(timeout=2000)
                            douyin_logger.debug(_msg("✅", f"裁剪确认: '{confirm_text}' ({sel[:40]})"))
                            # 等 ReactCrop 消失
                            try:
                                await page.wait_for_selector('.ReactCrop__image', state='hidden', timeout=5000)
                            except Exception:
                                pass
                            return True
                    except Exception:
                        continue
            douyin_logger.warning(_msg("⚠️", "找不到裁剪 modal 的确认按钮"))
            return False

        async def _dump_cover_state(tag: str):
            """在 cover 操作流程中 dump 关键元素状态(file inputs + tab labels + active card),
            DEBUG 用,识别真实 tab 切换是否有效。"""
            if not self.debug:
                return
            try:
                state = await page.evaluate(
                    """
                    () => {
                        const r = (el) => {
                            const b = el.getBoundingClientRect();
                            return {x: Math.round(b.x), y: Math.round(b.y), w: Math.round(b.width), h: Math.round(b.height)};
                        };
                        const inputs = Array.from(document.querySelectorAll('input[type="file"][accept*="image"]'))
                            .map((inp, i) => ({
                                idx: i,
                                visible: inp.offsetParent !== null,
                                rect: r(inp),
                                parentText: (inp.closest('div[class*="cropUploadWrapper"]')?.textContent || inp.parentElement?.textContent || '').replace(/\\s+/g, ' ').trim().substring(0, 80),
                            }));
                        const tipLabels = Array.from(document.querySelectorAll('div[class*="cover-tip"]'))
                            .map(el => ({
                                text: (el.textContent || '').trim(),
                                rect: r(el),
                            }));
                        // 找 active card 候选:含 active / selected / checked 类的祖先
                        const cards = tipLabels.map(t => {
                            const el = Array.from(document.querySelectorAll('div[class*="cover-tip"]'))
                                .find(e => (e.textContent || '').trim() === t.text);
                            if (!el) return null;
                            let p = el.parentElement;
                            const ancestorClasses = [];
                            for (let i = 0; i < 5 && p; i++) {
                                ancestorClasses.push((p.className || '').toString().substring(0, 60));
                                p = p.parentElement;
                            }
                            return { tipText: t.text, ancestorClasses };
                        }).filter(Boolean);
                        return { inputs, tipLabels, cards };
                    }
                    """
                )
                import json as _json
                path = f"/tmp/sau_cover_state_{tag}_{int(asyncio.get_event_loop().time())}.json"
                with open(path, "w", encoding="utf-8") as f:
                    _json.dump(state, f, ensure_ascii=False, indent=2)
                douyin_logger.info(_msg("🔍", f"封面状态 dump [{tag}]: {path}"))
            except Exception as e:
                douyin_logger.warning(_msg("⚠️", f"dump 失败 [{tag}]: {e}"))

        async def _dump_modal_tree() -> None:
            """完整 dump 设置封面 modal 内部结构 — 给定位逻辑用。"""
            if not self.debug:
                return
            try:
                tree = await page.evaluate(
                    """
                    () => {
                        // 找两个 cover-tip("竖封面3:4" / "横封面4:3"),dump 它们的完整祖先 chain + sibling 文字摘要
                        const tips = Array.from(document.querySelectorAll('div[class*="cover-tip"]'));
                        if (tips.length === 0) return { error: 'no cover-tip found' };
                        const out = [];
                        for (const tip of tips) {
                            out.push(`==== tip: "${(tip.textContent || '').trim()}" ====`);
                            let el = tip;
                            let depth = 0;
                            while (el && el.tagName !== 'BODY' && depth < 25) {
                                const cls = (el.className || '').toString();
                                const tag = el.tagName;
                                const innerText = (el.innerText || '').replace(/\\s+/g, ' ').trim().substring(0, 140);
                                const hasUploadNew = innerText.includes('上传新封面');
                                const hasUseOrig = innerText.includes('使用原视频封面');
                                const hasFileInput = el.querySelector ? !!el.querySelector('input[type="file"]') : false;
                                const inputCount = el.querySelectorAll ? el.querySelectorAll('input[type="file"]').length : 0;
                                out.push(`  depth=${depth} <${tag} cls="${cls.substring(0, 60)}"> uploadNew=${hasUploadNew} useOrig=${hasUseOrig} fileInputs=${inputCount} text="${innerText}"`);
                                el = el.parentElement;
                                depth++;
                            }
                            out.push('');
                        }
                        // 再 dump 所有 file inputs (无论是否在 cover-tip 祖先链上)
                        const allInputs = Array.from(document.querySelectorAll('input[type="file"]'));
                        out.push(`==== all file inputs (page-wide) ${allInputs.length} 个 ====`);
                        allInputs.forEach((inp, i) => {
                            const accept = inp.accept || '';
                            // 找最近的有"封面"/"竖"/"横"字样的祖先
                            let el = inp.parentElement;
                            let nearestCoverAncestor = '';
                            for (let k = 0; k < 12 && el; k++) {
                                const t = (el.innerText || '').substring(0, 60);
                                if (t.includes('竖封面') || t.includes('横封面')) {
                                    nearestCoverAncestor = `depth=${k} cls="${(el.className||'').toString().substring(0,50)}" text="${t}"`;
                                    break;
                                }
                                el = el.parentElement;
                            }
                            out.push(`  input[${i}] accept="${accept}" visible=${inp.offsetParent !== null} coverAnc=${nearestCoverAncestor || 'none'}`);
                        });
                        return { tree: out.join('\\n'), lines: out.length };
                    }
                    """
                )
                path = f"/tmp/sau_modal_tree_{int(asyncio.get_event_loop().time())}.txt"
                with open(path, "w", encoding="utf-8") as f:
                    if "tree" in tree:
                        f.write(tree["tree"])
                    else:
                        f.write(str(tree))
                douyin_logger.info(_msg("🌳", f"modal 完整结构 dump: {path} ({tree.get('lines', '?')} 行)"))
            except Exception as e:
                douyin_logger.warning(_msg("⚠️", f"modal tree dump 失败: {e}"))

        async def _verify_crop_loaded(label: str) -> bool:
            """等 ReactCrop__image naturalWidth>0,说明真实图加载到了 crop 区。"""
            for _ in range(40):  # 20s max
                try:
                    nw = await page.evaluate("""() => {
                        const img = document.querySelector('.ReactCrop__image');
                        return img ? img.naturalWidth : -1;
                    }""")
                    if nw and nw > 0:
                        douyin_logger.info(_msg("✅", f"[{label}] ReactCrop 图已加载 naturalWidth={nw}"))
                        return True
                except Exception:
                    pass
                await page.wait_for_timeout(500)
            return False

        async def _click_save_in_crop_modal(label: str) -> bool:
            """这版 modal 极简: title '设置封面' + crop + 取消/保存 按钮。点保存关 modal 提交 crop。

            重要:force=True click 会跳过 React onClick handler,只触发原生 click 事件。
            用 Playwright 的 dispatch_event('click') 或 mouse.click(x,y) 才能真正触发 React。
            """
            # 找到粉色"保存"按钮 (cls 含 primary-cECiOJ 是粉色主按钮)
            save_btn = page.locator('button.primary-cECiOJ:has-text("保存"), button:has-text("保存")').filter(has_text="保存").first
            if not await save_btn.count():
                douyin_logger.error(_msg("❌", f"[{label}] 找不到 '保存' 按钮"))
                return False
            # 拿坐标
            try:
                box = await save_btn.bounding_box()
            except Exception:
                box = None
            if not box:
                douyin_logger.warning(_msg("⚠️", f"[{label}] 拿不到 '保存' 按钮坐标"))
                return False
            # 用 mouse 真实 click 中心坐标 — 触发 React onClick
            cx = box["x"] + box["width"] / 2
            cy = box["y"] + box["height"] / 2
            try:
                await page.mouse.click(cx, cy, delay=80)
                douyin_logger.info(_msg("🟢", f"[{label}] mouse.click '保存' at ({cx:.0f},{cy:.0f})"))
                return True
            except Exception as e:
                douyin_logger.warning(_msg("⚠️", f"[{label}] mouse click 失败: {e},fallback dispatch_event"))
            # 兜底 dispatchEvent
            try:
                await save_btn.dispatch_event('click')
                douyin_logger.info(_msg("🟢", f"[{label}] dispatch_event click '保存'"))
                return True
            except Exception as e:
                douyin_logger.error(_msg("❌", f"[{label}] dispatch_event 也失败: {e}"))
                return False

        async def _wait_modal_closed(label: str) -> bool:
            """modal close 信号:ReactCrop__image 消失。"""
            for _ in range(20):  # 10s
                try:
                    if not await page.locator('.ReactCrop__image').count():
                        douyin_logger.info(_msg("✅", f"[{label}] modal 已关闭"))
                        return True
                except Exception:
                    pass
                await page.wait_for_timeout(500)
            return False

        # 2) modal 已通过 set_files 打开,图已加载,验证后点保存
        if self.debug:
            try:
                shot = f"/tmp/sau_dy_modal_opened_{int(asyncio.get_event_loop().time())}.png"
                await page.screenshot(path=shot, full_page=True)
                douyin_logger.info(_msg("📸", f"modal 打开截图: {shot}"))
            except Exception:
                pass

        if not await _verify_crop_loaded("竖封面"):
            raise RuntimeError("竖封面 ReactCrop naturalWidth==0,图没加载")

        # DUMP modal 完整内容
        modal_inner = await page.evaluate(
            """() => {
                const portal = document.querySelector('.semi-portal:has(.ReactCrop__image), div[role="dialog"]:has(.ReactCrop__image)');
                if (!portal) return { error: 'no crop portal' };
                const r = (el) => {
                    if (!el) return null;
                    const b = el.getBoundingClientRect();
                    return {x: Math.round(b.x), y: Math.round(b.y), w: Math.round(b.width), h: Math.round(b.height), ratio: (b.width/b.height).toFixed(2)};
                };
                const buttons = Array.from(portal.querySelectorAll('button, [role="button"], a')).map(b => ({
                    text: (b.innerText || b.textContent || '').trim().substring(0, 30),
                    cls: (b.className || '').toString().substring(0, 80),
                    visible: b.offsetParent !== null,
                    rect: r(b),
                    title: b.getAttribute('title') || '',
                    ariaLabel: b.getAttribute('aria-label') || '',
                    iconHtml: (Array.from(b.querySelectorAll('svg, i, [class*="icon"]')).map(e => (e.outerHTML || '').substring(0, 100)).join(';')).substring(0, 200),
                    outerHtml: (b.outerHTML || '').substring(0, 250),
                }));
                const reactCrop = portal.querySelector('.ReactCrop');
                const cropImg = portal.querySelector('.ReactCrop__image');
                const cropSel = portal.querySelector('.ReactCrop__crop-selection');
                // 还有 image src / crop selection style
                const cropStyle = cropSel ? (cropSel.getAttribute('style') || '') : '';
                return {
                    title: (portal.innerText || '').split('\\n')[0].substring(0, 40),
                    buttons,
                    imgRect: r(cropImg),
                    imgSrc: cropImg ? (cropImg.src || '').substring(0, 60) : null,
                    cropRect: r(cropSel),
                    cropStyle,
                    reactCropRect: r(reactCrop),
                    portalText: (portal.innerText || '').substring(0, 300),
                };
            }"""
        )
        # 写到文件方便看
        import json as _json
        modal_dump_path = f"/tmp/sau_dy_modal_inner_{int(asyncio.get_event_loop().time())}.json"
        with open(modal_dump_path, "w", encoding="utf-8") as f:
            _json.dump(modal_inner, f, ensure_ascii=False, indent=2)
        douyin_logger.info(_msg("🔍", f"竖封面 modal 内部 dump: {modal_dump_path}"))

        await page.wait_for_timeout(1500)  # 让 crop 选区稳定

        if not await _click_save_in_crop_modal("竖封面"):
            raise RuntimeError("竖封面 modal '保存' 按钮找不到")

        if not await _wait_modal_closed("竖封面"):
            raise RuntimeError("竖封面 modal 没关闭 — 保存失败")

        douyin_logger.success(_msg("🥳", "mini modal 保存完成"))
        await page.wait_for_timeout(2500)

        # 完整 modal(AI 模式)如果弹了就先 X 关掉 — 它不适合我们用代码操作(全 AI 生成异步)。
        # mini modal 已经直接 commit 了 cover。
        try:
            close_btn = page.locator('button.semi-modal-close').first
            if await close_btn.count() and await close_btn.is_visible():
                box = await close_btn.bounding_box()
                if box:
                    await page.mouse.click(box["x"] + box["width"]/2, box["y"] + box["height"]/2, delay=80)
                    douyin_logger.info(_msg("🚪", "关闭完整 modal (X 按钮)"))
                    await page.wait_for_timeout(1500)
        except Exception:
            pass

        # 重新看完整 modal 是否还在 — 如果还在,试 _trigger_card_input 横封面后做横封面 mini modal
        full_modal_open = False
        for _ in range(3):
            try:
                if await page.locator('text="设置竖封面"').count() and await page.locator('text="设置横封面"').count():
                    full_modal_open = True
                    break
            except Exception:
                pass
            await page.wait_for_timeout(300)

        if not full_modal_open:
            # 完整 modal 没出现 — mini modal 可能已经直接 commit 了。验证 inline 区缩略图。
            douyin_logger.warning(_msg("⚠️", "完整 modal 没出现,验证 inline 区是否已 commit"))
            inline_state = await page.evaluate(
                """() => {
                    const cards = Array.from(document.querySelectorAll('div[class*="coverControl"]'));
                    return cards.map((card, idx) => {
                        // img/source/background-image 都看
                        const imgs = Array.from(card.querySelectorAll('img')).map(i => ({src: (i.src||'').substring(0,80), w: i.naturalWidth}));
                        // 所有有 background-image 的 element
                        const elsWithBg = [];
                        card.querySelectorAll('*').forEach(el => {
                            const bg = window.getComputedStyle(el).backgroundImage;
                            if (bg && bg !== 'none' && bg.includes('url(')) {
                                const m = bg.match(/url\\(['"]?([^'"\\)]+)/);
                                if (m) elsWithBg.push({tag: el.tagName, cls: (el.className||'').toString().substring(0,40), bg: m[1].substring(0, 80)});
                            }
                        });
                        return { idx, cardText: (card.innerText || '').replace(/\\s+/g,' ').substring(0,30), imgs, bgs: elsWithBg };
                    });
                }"""
            )
            douyin_logger.info(_msg("🔍", f"mini-only inline state: {inline_state}"))
            # 如果任一卡有 blob:/data:/真 url 的缩略图,认为 mini commit 成功
            committed_any = False
            for c in inline_state:
                for src_obj in c.get("imgs", []) + [{"src": b["bg"]} for b in c.get("bgs", [])]:
                    src = src_obj.get("src", "")
                    if src and not src.startswith("data:image/svg") and ("blob:" in src or "douyin" in src or "byteimg" in src or "static" not in src.lower()):
                        if any(kw in src for kw in ["blob:", "douyin.com", "byteimg.com", "douyincdn.com", "img.douyin"]):
                            committed_any = True
                            break
                if committed_any:
                    break
            if not committed_any:
                if self.debug:
                    try:
                        shot = f"/tmp/sau_dy_inline_not_committed_{int(asyncio.get_event_loop().time())}.png"
                        await page.screenshot(path=shot, full_page=True)
                        douyin_logger.error(_msg("📸", f"inline 没 commit: {shot}"))
                    except Exception:
                        pass
                raise RuntimeError("mini 保存后,inline 区没看到真实封面图(可能 commit 失败)")
            douyin_logger.success(_msg("🥳", "inline 区检测到真实封面图 — mini 已 commit"))
            # 跳过完整 modal 流程
            douyin_logger.info(_msg("🥳", "封面流程完成(mini-only path)"))
            return
        douyin_logger.info(_msg("📦", "完整封面 modal 已自动打开"))

        if self.debug:
            try:
                shot = f"/tmp/sau_dy_full_modal_{int(asyncio.get_event_loop().time())}.png"
                await page.screenshot(path=shot, full_page=True)
                douyin_logger.info(_msg("📸", f"完整 modal 截图: {shot}"))
            except Exception:
                pass

        async def _upload_in_full_modal(tab_text: str) -> bool:
            """在完整 modal 内:点 tab → 点 "+ 上传封面" → expect_file_chooser → set_files → 验证 ReactCrop。"""
            # 先切 tab(如果还不在该 tab)
            tab = page.get_by_text(tab_text, exact=True).first
            if await tab.count():
                try:
                    await tab.click(timeout=3000, force=True)
                    douyin_logger.info(_msg("🗂️", f"切到完整 modal tab '{tab_text}'"))
                    await page.wait_for_timeout(1200)
                except Exception as e:
                    douyin_logger.debug(f"tab '{tab_text}' click 失败: {e}")

            # 找 "+ 上传封面" 按钮 → file_chooser
            upload_btn = page.get_by_text("上传封面", exact=True).first
            if not await upload_btn.count():
                upload_btn = page.get_by_role("button", name="上传封面").first
            if not await upload_btn.count():
                douyin_logger.error(_msg("❌", f"[{tab_text}] 完整 modal 找不到 '+ 上传封面' 按钮"))
                return False

            uploaded = False
            try:
                async with page.expect_file_chooser(timeout=8000) as fc_info:
                    # 用 mouse click 真实触发,而不是 force=True
                    try:
                        box = await upload_btn.bounding_box()
                        if box:
                            await page.mouse.click(box["x"] + box["width"]/2, box["y"] + box["height"]/2, delay=80)
                        else:
                            await upload_btn.click(timeout=3000)
                    except Exception:
                        await upload_btn.click(timeout=3000, force=True)
                fc = await fc_info.value
                await fc.set_files(thumb_path)
                douyin_logger.info(_msg("🖼️", f"[{tab_text}] file_chooser set_files"))
                uploaded = True
            except PlaywrightTimeoutError:
                douyin_logger.warning(_msg("⚠️", f"[{tab_text}] file_chooser 没弹,fallback set_files to .last"))
                file_input = page.locator('input[type="file"][accept*="image"]').last
                try:
                    await file_input.wait_for(state="attached", timeout=3000)
                    await file_input.set_input_files(thumb_path)
                    douyin_logger.info(_msg("🖼️", f"[{tab_text}] set_input_files to .last input"))
                    uploaded = True
                except PlaywrightTimeoutError:
                    pass

            if not uploaded:
                return False

            # 等 ReactCrop 显示真实图
            for _ in range(40):
                try:
                    nw = await page.evaluate("""() => {
                        const img = document.querySelector('.ReactCrop__image');
                        return img ? img.naturalWidth : -1;
                    }""")
                    if nw and nw > 0:
                        douyin_logger.info(_msg("✅", f"[{tab_text}] ReactCrop naturalWidth={nw}"))
                        return True
                except Exception:
                    pass
                await page.wait_for_timeout(500)
            return False

        # 完整 modal 默认在 "设置竖封面" tab
        if not await _upload_in_full_modal("设置竖封面"):
            raise RuntimeError("完整 modal: 设置竖封面 失败")

        # 切到 "设置横封面" tab,上传
        if not await _upload_in_full_modal("设置横封面"):
            raise RuntimeError("完整 modal: 设置横封面 失败")

        # 点完整 modal "完成" 按钮 - 真 commit
        await page.wait_for_timeout(1000)
        done_btn = page.get_by_text("完成", exact=True).first
        if not await done_btn.count():
            done_btn = page.get_by_role("button", name="完成").first
        if not await done_btn.count():
            raise RuntimeError("完整 modal: 找不到 '完成' 按钮")
        try:
            box = await done_btn.bounding_box()
            if box:
                await page.mouse.click(box["x"] + box["width"]/2, box["y"] + box["height"]/2, delay=80)
                douyin_logger.info(_msg("🟢", f"mouse.click 完整 modal '完成' at ({box['x']+box['width']/2:.0f},{box['y']+box['height']/2:.0f})"))
            else:
                await done_btn.click(timeout=3000, force=True)
        except Exception:
            await done_btn.click(timeout=3000, force=True)

        # 等完整 modal 关闭
        for _ in range(20):
            try:
                if not await page.locator('text="设置竖封面"').count():
                    douyin_logger.success(_msg("🥳", "完整 modal 已关闭,封面真 commit"))
                    break
            except Exception:
                pass
            await page.wait_for_timeout(500)
        else:
            raise RuntimeError("完整 modal 没关闭")

        douyin_logger.success(_msg("🥳", "两张封面已通过完整 modal 全部 commit"))

    async def upload(self, playwright: Playwright) -> None:
        douyin_logger.info(_msg("🧍", "小人先检查 cookie、视频文件、封面和发布时间"))
        await self.validate_upload_args()
        douyin_logger.info(_msg("🥳", "上传前检查通过"))

        browser = await playwright.chromium.launch(headless=self.headless, channel="chrome")
        context = await browser.new_context(
            storage_state=f"{self.account_file}",
            permissions=["geolocation"],
        )
        context = await set_init_script(context)

        page = await context.new_page()
        await page.goto("https://creator.douyin.com/creator-micro/content/upload")
        douyin_logger.info(_msg("🏃", f"小人开始搬运视频: {self.title}.mp4"))
        douyin_logger.info(_msg("🧭", "小人正在赶往上传主页"))
        await page.wait_for_url("https://creator.douyin.com/creator-micro/content/upload")
        await page.locator("div[class^='container'] input").set_input_files(self.file_path)

        while True:
            try:
                await page.wait_for_url(
                    "https://creator.douyin.com/creator-micro/content/publish?enter_from=publish_page",
                    timeout=3000,
                )
                douyin_logger.info(_msg("🥳", "已经进入 version_1 发布页面"))
                break
            except Exception:
                try:
                    await page.wait_for_url(
                        "https://creator.douyin.com/creator-micro/content/post/video?enter_from=publish_page",
                        timeout=3000,
                    )
                    douyin_logger.info(_msg("🥳", "已经进入 version_2 发布页面"))
                    break
                except Exception:
                    douyin_logger.debug(_msg("🧍", "还没进到视频发布页面，小人继续等一会"))
                    await asyncio.sleep(0.5)

        await asyncio.sleep(1)
        # 进入发布页后先清理 shepherd 引导(共创中心等新功能引导会拦截 pointer events)
        await self.dismiss_shepherd_guides(page)
        douyin_logger.info(_msg("✍️", "小人开始填标题、描述和话题"))
        # 填字段前再次兜底,因为引导可能延迟弹出
        await self.dismiss_shepherd_guides(page)
        await self.fill_title_and_description(page, self.title, self.desc or self.title, self.tags)
        douyin_logger.info(_msg("🏷️", f"小人一共贴了 {len(self.tags)} 个话题"))

        while True:
            try:
                number = await page.locator('[class^="long-card"] div:has-text("重新上传")').count()
                if number > 0:
                    douyin_logger.success(_msg("🥳", "视频已经传完啦"))
                    break
                douyin_logger.info(_msg("🏃", "小人正在努力上传视频"))
                await asyncio.sleep(2)
                if await page.locator('div.progress-div > div:has-text("上传失败")').count():
                    douyin_logger.error(_msg("😵", "检测到上传失败，小人准备重试"))
                    await self.handle_upload_error(page)
            except Exception:
                douyin_logger.debug(_msg("🧍", "小人还在等视频上传完成"))
                await asyncio.sleep(2)

        if self.productLink and self.productTitle:
            douyin_logger.info(_msg("🛒", "小人正在设置商品链接"))
            await self.set_product_link(page, self.productLink, self.productTitle)
            douyin_logger.info(_msg("🥳", "商品链接设置完成"))

        await self.set_thumbnail(page)

        third_part_element = '[class^="info"] > [class^="first-part"] div div.semi-switch'
        if await page.locator(third_part_element).count():
            if "semi-switch-checked" not in await page.eval_on_selector(third_part_element, "div => div.className"):
                await page.locator(third_part_element).locator("input.semi-switch-native-control").click()

        if self.publish_strategy == DOUYIN_PUBLISH_STRATEGY_SCHEDULED and self.publish_date != 0:
            await self.set_schedule_time_douyin(page, self.publish_date)

        # 抖音点"发布"后可能弹"未添加自主声明" dialog(AI 生成 / 营销推广等检测),默认走"直接发布"
        # (用户已在 desc 里能识别 AI,日后可在创作中心补标声明);未来想强制标 AI 可加 --ai-declaration 参数。
        async def _dismiss_publish_confirm_dialog(_page) -> bool:
            """如果有"未添加自主声明"等发布前确认 dialog,点"直接发布"通过。返回是否处理过 dialog。"""
            for sel in [
                'button:visible:has-text("直接发布")',
                'div[role="dialog"] button:has-text("直接发布")',
                'div[role="dialog"] button:has-text("跳过")',
                'div[role="dialog"] button:has-text("忽略")',
            ]:
                try:
                    loc = _page.locator(sel).first
                    if await loc.count() and await loc.is_visible():
                        await loc.click(timeout=2000)
                        douyin_logger.debug(_msg("🚪", f"通过发布确认 dialog: {sel}"))
                        return True
                except Exception:
                    continue
            return False

        publish_button_names = ["发布", "立即发布", "发布作品", "发布视频", "确认发布"]
        max_attempts = 40
        for attempt in range(max_attempts):
            try:
                clicked = False
                for name in publish_button_names:
                    btn = page.get_by_role("button", name=name, exact=True)
                    if await btn.count():
                        await btn.click()
                        clicked = True
                        douyin_logger.debug(_msg("🎯", f"点击发布按钮: '{name}'"))
                        break

                if not clicked:
                    for sel in [
                        'button:has-text("立即发布"):visible',
                        'button:has-text("发布作品"):visible',
                        'button:has-text("发布视频"):visible',
                        'button[class*="primary"]:has-text("发布"):visible',
                        'button:visible:has-text("发布")',
                    ]:
                        try:
                            loc = page.locator(sel).first
                            if await loc.count() and await loc.is_visible():
                                await loc.click(timeout=2000)
                                clicked = True
                                douyin_logger.debug(_msg("🎯", f"点击发布按钮 fallback: {sel}"))
                                break
                        except Exception:
                            continue

                # 点完发布按钮后,等 0.5s 给 dialog 弹起来时间,然后尝试 dismiss
                if clicked:
                    await page.wait_for_timeout(800)
                    await _dismiss_publish_confirm_dialog(page)

                await page.wait_for_url(
                    "https://creator.douyin.com/creator-micro/content/manage**",
                    timeout=3000,
                )
                douyin_logger.success(_msg("🥳", "视频发布成功，小人开心收工"))
                break
            except Exception:
                # 跳转没成功 → 可能是 dialog 还在,主动 dismiss 一次
                await _dismiss_publish_confirm_dialog(page)
                await self.handle_auto_video_cover(page)
                if attempt % 10 == 0:
                    douyin_logger.info(_msg("🏃", f"小人正在冲刺发布视频 ({attempt + 1}/{max_attempts})"))
                await asyncio.sleep(0.5)
        else:
            raise RuntimeError(f"发布按钮 click 失败 {max_attempts} 次")

        await context.storage_state(path=self.account_file)
        douyin_logger.success(_msg("🥳", "cookie 更新完毕"))
        await asyncio.sleep(2)
        await context.close()
        await browser.close()

    async def douyin_upload_video(self):
        async with async_playwright() as playwright:
            await self.upload(playwright)

    async def main(self):
        await self.douyin_upload_video()


class DouYinNote(DouYinBaseUploader):
    def __init__(
        self,
        image_paths,
        note,
        tags,
        publish_date: datetime | int,
        account_file,
        title: str | None = None,
        publish_strategy: str = DOUYIN_PUBLISH_STRATEGY_IMMEDIATE,
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
        self.title = title or (self.note[:30] if self.note else "")
        self.tags = tags or []

    async def validate_upload_args(self):
        await self.validate_base_args()
        if not self.title or not str(self.title).strip():
            raise ValueError("图文模式下，title 是必须的")
        if not self.image_paths:
            raise ValueError("图文模式下，图片是必须的")

        if isinstance(self.image_paths, (str, Path)):
            self.image_paths = [self.image_paths]

        if len(self.image_paths) > 35:
            raise ValueError("图文模式下最多只支持上传 35 张图片")

        normalized_image_paths = []
        for image_path in self.image_paths:
            normalized_image_paths.append(str(self.validate_image_file(image_path)))
        self.image_paths = normalized_image_paths

    async def upload_note_content(self, page: Page) -> None:
        douyin_logger.info(_msg("🏃", f"小人开始搬运图文，共 {len(self.image_paths)} 张图片"))
        douyin_logger.info(_msg("🔀", "小人正在切换到图文发布"))
        await page.get_by_text("发布图文", exact=True).click()
        await page.wait_for_timeout(1000)

        douyin_logger.info(_msg("📤", "小人正在上传图片"))
        await page.locator("div[class^='container'] input[accept*='image']").set_input_files(self.image_paths)

        while True:
            try:
                await page.wait_for_url(
                    "**/creator-micro/content/post/image?**",
                    timeout=3000,
                )
                douyin_logger.info(_msg("🥳", "已经进入图文发布页面"))
                break
            except Exception:
                douyin_logger.debug(_msg("🧍", "小人还在等图片上传完成"))
                await asyncio.sleep(0.5)

        await asyncio.sleep(1)
        douyin_logger.info(_msg("✍️", "小人开始填标题、描述和话题"))
        await self.fill_title_and_description(page, self.title, self.note, self.tags)
        douyin_logger.info(_msg("🏷️", f"小人一共贴了 {len(self.tags)} 个话题"))

        if self.publish_strategy == DOUYIN_PUBLISH_STRATEGY_SCHEDULED and self.publish_date != 0:
            await self.set_schedule_time_douyin(page, self.publish_date)

        while True:
            try:
                publish_button = page.get_by_role("button", name="发布", exact=True)
                if await publish_button.count():
                    await publish_button.click()
                await page.wait_for_url(
                    "**/creator-micro/content/manage?enter_from=publish**",
                    timeout=3000,
                )
                douyin_logger.success(_msg("🥳", "图文发布成功，小人开心收工"))
                break
            except Exception:
                douyin_logger.info(_msg("🏃", "小人正在冲刺发布图文"))
                await asyncio.sleep(0.5)

    async def upload(self, playwright: Playwright) -> None:
        douyin_logger.info(_msg("🧍", "小人先检查 cookie、图片和发布时间"))
        await self.validate_upload_args()
        douyin_logger.info(_msg("🥳", "图文上传前检查通过"))

        browser = await playwright.chromium.launch(headless=self.headless, channel="chrome")
        context = await browser.new_context(
            storage_state=f"{self.account_file}",
            permissions=["geolocation"],
        )
        context = await set_init_script(context)

        upload_success = False
        try:
            page = await context.new_page()
            await page.goto("https://creator.douyin.com/creator-micro/content/upload")
            douyin_logger.info(_msg("🧭", "小人正在赶往图文发布页"))
            await page.wait_for_url("https://creator.douyin.com/creator-micro/content/upload")

            await self.upload_note_content(page)
            upload_success = True
        finally:
            if upload_success:
                await context.storage_state(path=self.account_file)
                douyin_logger.success(_msg("🥳", "cookie 更新完毕"))
                await asyncio.sleep(2)
            await context.close()
            await browser.close()

    async def douyin_upload_note(self):
        async with async_playwright() as playwright:
            await self.upload(playwright)
