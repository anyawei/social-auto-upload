import asyncio
import inspect
import os
from datetime import datetime
from pathlib import Path

from conf import DEBUG_MODE, LOCAL_CHROME_HEADLESS, LOCAL_CHROME_PATH
from patchright.async_api import Page, Playwright, async_playwright
from patchright.async_api import TimeoutError as PlaywrightTimeoutError
from uploader.base_video import BaseVideoUploader
from utils.base_social_media import set_init_script
from utils.log import douyin_logger
from utils.login_qrcode import (
    build_login_qrcode_path,
    decode_qrcode_from_path,
    print_terminal_qrcode,
    remove_qrcode_file,
    save_data_url_image,
)

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


def _build_login_result(
    success: bool, status: str, message: str, account_file: str, qrcode: dict | None = None, current_url: str = ""
) -> dict:
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
            await page.goto(
                "https://creator.douyin.com/creator-micro/content/upload", wait_until="domcontentloaded", timeout=60000
            )
            try:
                await page.wait_for_url("https://creator.douyin.com/creator-micro/content/upload", timeout=10000)
            except Exception:
                # 抖音设备指纹绑定:headless 预检指纹跟 headed 登录不一致会被误判"未登录"。
                # 这里不直接判失效,放行让真正的上传流程(可走 headed,指纹一致)自己判断。
                douyin_logger.warning(_msg("⚠️", "cookie 预检未确认登录态(可能 headless 指纹差异),放行让上传流程自判"))
                return True

            if await page.get_by_text("手机号登录").count() or await page.get_by_text("扫码登录").count():
                douyin_logger.warning(_msg("⚠️", "cookie 预检看到登录入口,但 headless 指纹可能误判,放行让上传流程自判"))
                return True

            return True
        finally:
            await browser.close()


async def douyin_setup(
    account_file, handle=False, return_detail=False, qrcode_callback=None, headless: bool = LOCAL_CHROME_HEADLESS
):
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
        scan_login_tab.locator("..")
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


async def _save_douyin_qrcode(
    page: Page, account_file: str, previous_qrcode_path: Path | None = None, qrcode_callback=None
) -> dict:
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


async def _wait_for_douyin_login(
    page: Page,
    account_file: str,
    qrcode_info: dict,
    qrcode_callback=None,
    poll_interval: int = 3,
    max_checks: int = 100,
) -> dict:
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
            # 等所有资源(load)在抖音创作中心常 timeout(海外分析/CDN 资源走代理卡死),改 domcontentloaded + 60s
            await page.goto("https://creator.douyin.com/", wait_until="domcontentloaded", timeout=60000)
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
            result = _build_login_result(
                False, "failed", str(exc), account_file, current_url=page.url if "page" in locals() else ""
            )
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
        publish_date_hour = publish_date.strftime("%Y-%m-%d %H:%M")
        # 选「定时发布」单选
        await page.locator("[class^='radio']:has-text('定时发布')").click()
        await asyncio.sleep(1)

        # 定位日期输入框(placeholder 优先 + 兜底)
        date_input = page.locator('.semi-input[placeholder="日期和时间"]').first
        if not await date_input.count():
            date_input = page.locator('input[placeholder*="日期"], .semi-datepicker input').first
        if not await date_input.count():
            raise RuntimeError("找不到定时发布日期输入框 — UI 可能变了")

        async def _fill_datetime(strategy: str) -> str:
            # 坑(2026-05-27):semi DatePicker 不吃"triple-click 全选 + type"(默认值清不掉,
            # 输入也不进 → 卡"距定时<2小时"报错)。旧代码更是用 Control+KeyA(Mac 上不是全选)。
            # 这里两招:① Playwright fill()(focus+清空+派发 input 事件,最稳);② Mac Cmd+A 全选重输。
            await date_input.click()
            await asyncio.sleep(0.4)
            if strategy == "fill":
                try:
                    await date_input.fill(publish_date_hour)
                except Exception as e:
                    douyin_logger.debug(f"fill() 失败: {e}")
            else:  # mac 全选(Cmd+A)再输
                await page.keyboard.press("Meta+A")
                await asyncio.sleep(0.2)
                await page.keyboard.press("Backspace")
                await asyncio.sleep(0.2)
                await page.keyboard.type(publish_date_hour, delay=80)
            await page.keyboard.press("Enter")
            await asyncio.sleep(1)
            try:
                return (await date_input.input_value()) or ""
            except Exception:
                return ""

        val = await _fill_datetime("fill")
        if publish_date_hour not in val:
            douyin_logger.warning(
                _msg("⚠️", f"定时时间 fill 没填对(期望含 '{publish_date_hour}',实际 '{val}'),换 Cmd+A 重输")
            )
            val = await _fill_datetime("mac_select")
        if publish_date_hour in val:
            douyin_logger.success(_msg("🕒", f"定时发布时间已设为 {val}"))
        else:
            raise RuntimeError(f"定时发布时间设置失败,实际值 '{val}' — 不发布以免按默认时间发错")

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
            dropdown = (
                page.get_by_text("添加标签").locator("..").locator("..").locator("..").locator(".semi-select").first
            )
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
        """抖音 PC 封面流程(2026-05-27 按 anyawei 实操重写)。

        正确流程(用户验证):
        1. 点 inline "选择封面" → 打开 "设置竖封面/设置横封面" modal(默认竖封面 tab)
        2. 点 modal 内黑色 "上传封面" 按钮 → 触发选文件(expect_file_chooser)→ set_files(竖版图)
           → 等 ReactCrop__image naturalWidth>0(图真加载到 crop)
        3. 传完竖封面会弹框问是否也设横封面 → 点同意 → tab 自动切到 "设置横封面"
        4. 再点 "上传封面" → file_chooser → set_files(横版图)
        5. 点 "完成" → modal 关闭 + 封面提交

        关键(踩过的坑):必须 **点 "上传封面" 按钮经 file_chooser 选图**;早期直接往 hidden
        input set_input_files 会让 ReactCrop 的 naturalWidth==0(图没真加载)→ 黑屏/回退首帧。
        竖封面用 thumbnail_portrait_path,横封面用 thumbnail_landscape_path。
        """
        portrait = self.thumbnail_portrait_path
        landscape = self.thumbnail_landscape_path
        if not portrait and not landscape:
            return

        douyin_logger.info(_msg("🏃", "小人正在设置视频封面"))

        async def _shot(tag: str) -> None:
            if not self.debug:
                return
            try:
                p = f"/tmp/sau_dy_cover_{tag}_{int(asyncio.get_event_loop().time())}.png"
                await page.screenshot(path=p, full_page=True)
                douyin_logger.info(_msg("📸", f"[{tag}] {p}"))
            except Exception:
                pass

        async def _dump_buttons(tag: str) -> None:
            """dump 当前所有可见按钮文字 — 用来发现'是否设横封面'弹框的按钮名。"""
            if not self.debug:
                return
            try:
                txts = await page.evaluate(
                    """() => Array.from(document.querySelectorAll(
                            'button, [role=button], div[class*=btn], div[class*=button]'))
                        .filter(b => b.offsetParent !== null)
                        .map(b => (b.innerText || '').replace(/\\s+/g, ' ').trim())
                        .filter(t => t && t.length <= 24)"""
                )
                douyin_logger.info(_msg("🔘", f"[{tag}] 可见按钮: {txts}"))
            except Exception:
                pass

        async def _dump_dom(tag: str) -> None:
            """无条件 dump 当前 DOM 的封面相关元素(imgs + 可见按钮 + 嵌套 portal),
            写到 /tmp 文件 + log 摘要。screenshot 在抖音页会抛异常,所以用 evaluate 拿数据。"""
            try:
                data = await page.evaluate(
                    """() => {
                        const imgs = Array.from(document.querySelectorAll('img'))
                            .map(im => ({
                                cls: (im.className || '').toString().slice(0, 50),
                                src: (im.currentSrc || im.src || '').slice(0, 70),
                                nw: im.naturalWidth, nh: im.naturalHeight,
                                vis: im.offsetParent !== null,
                            }))
                            .filter(o => o.nw > 0 || o.src.startsWith('blob:') || o.src.startsWith('data:'));
                        const btns = Array.from(document.querySelectorAll('button, [role=button]'))
                            .filter(b => b.offsetParent !== null)
                            .map(b => (b.innerText || '').replace(/\\s+/g, ' ').trim())
                            .filter(t => t && t.length <= 24);
                        const portals = Array.from(document.querySelectorAll(
                            '.semi-portal, div[role=dialog], div[class*=modal], div[class*=Modal]'))
                            .filter(p => p.offsetParent !== null)
                            .map(p => ({ cls: (p.className || '').toString().slice(0, 50),
                                         txt: (p.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 60) }));
                        const crops = Array.from(document.querySelectorAll(
                            '[class*=ReactCrop], [class*=crop], [class*=Crop], [class*=cropper]'))
                            .map(c => ({ cls: (c.className || '').toString().slice(0, 50),
                                         hasImg: !!c.querySelector('img') }));
                        return { imgs, btns, portals, crops };
                    }"""
                )
                import json as _json

                p = f"/tmp/sau_dy_dom_{tag}_{int(asyncio.get_event_loop().time())}.json"
                with open(p, "w", encoding="utf-8") as f:
                    _json.dump(data, f, ensure_ascii=False, indent=2)
                douyin_logger.info(
                    _msg(
                        "🔍",
                        f"[{tag}] DOM dump -> {p} | imgs={len(data['imgs'])} btns={data['btns']} crops={data['crops']}",
                    )
                )
            except Exception as e:
                douyin_logger.warning(_msg("⚠️", f"[{tag}] DOM dump 失败: {e}"))

        async def _verify_crop_loaded(label: str) -> bool:
            """判定封面图加载成功:.ReactCrop__image 或 任意 modal 内 blob/data img naturalWidth>0。"""
            for _ in range(40):  # 20s
                try:
                    res = await page.evaluate(
                        """() => {
                            const rc = document.querySelector('.ReactCrop__image');
                            if (rc && rc.naturalWidth > 0) return {ok: true, sel: 'ReactCrop__image', nw: rc.naturalWidth};
                            const cand = Array.from(document.querySelectorAll('img'))
                                .find(im => im.naturalWidth >= 100
                                    && ((im.currentSrc || im.src || '').startsWith('blob:')
                                        || (im.currentSrc || im.src || '').startsWith('data:'))
                                    && im.offsetParent !== null);
                            if (cand) return {ok: true, sel: 'blob/data img', nw: cand.naturalWidth,
                                              cls: (cand.className||'').toString().slice(0,40)};
                            return {ok: false};
                        }"""
                    )
                    if res and res.get("ok"):
                        douyin_logger.info(_msg("✅", f"[{label}] 封面图已加载 ({res})"))
                        return True
                except Exception:
                    pass
                await page.wait_for_timeout(500)
            await _dump_dom(f"{label}_未加载")
            return False

        async def _upload_via_button(image_path: str, label: str) -> None:
            """点 modal 内 '上传封面' 按钮 → file_chooser → set_files → 等 ReactCrop 加载。"""
            upload_btn = page.get_by_text("上传封面", exact=True).first
            if not await upload_btn.count():
                upload_btn = page.get_by_role("button", name="上传封面").first
            if not await upload_btn.count():
                raise RuntimeError(f"[{label}] 找不到 '上传封面' 按钮")
            try:
                async with page.expect_file_chooser(timeout=8000) as fc_info:
                    box = await upload_btn.bounding_box()
                    if box:
                        await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, delay=80)
                    else:
                        await upload_btn.click(timeout=3000)
                fc = await fc_info.value
                await fc.set_files(image_path)
                douyin_logger.info(_msg("🖼️", f"[{label}] file_chooser set_files: {image_path}"))
            except PlaywrightTimeoutError:
                # 兜底:file_chooser 没弹就直接往最后一个 image input set_files
                file_input = page.locator('input[type="file"][accept*="image"]').last
                await file_input.set_input_files(image_path)
                douyin_logger.info(_msg("🖼️", f"[{label}] fallback set_input_files: {image_path}"))
            # 选完文件留点时间让图渲染进 modal 的 crop 区(抖音当前 UI 是 inline crop,没有嵌套裁剪子框,
            # 别去点什么"确认/完成"—— 之前那样会误点主 modal 的"完成"提前关掉)。
            await page.wait_for_timeout(2500)
            if self.debug:
                await _dump_dom(f"{label}_set_files后")
            if not await _verify_crop_loaded(label):
                raise RuntimeError(f"[{label}] 封面图没加载(naturalWidth==0)")

        async def _go_landscape_tab() -> None:
            """传完竖封面后:过 '是否也设横封面' 弹框(点同意)/ 或直接点 '设置横封面' tab,切到横封面。"""
            await _dump_buttons("竖封面后-找横封面入口")
            # 1) 先试弹框的同意按钮(名字未知,挨个试)
            for t in ["去设置", "立即设置", "上传横封面", "去上传", "设置横封面", "继续", "好的", "确定", "确认"]:
                loc = page.locator(
                    f'div[role="dialog"] button:has-text("{t}"), .semi-modal button:has-text("{t}"), '
                    f'div[class*="modal"] button:has-text("{t}")'
                ).first
                try:
                    if await loc.count() and await loc.is_visible():
                        await loc.click(timeout=2000)
                        douyin_logger.info(_msg("🗂️", f"横封面弹框点了 '{t}'"))
                        await page.wait_for_timeout(800)
                        break
                except Exception:
                    continue
            # 2) 无论有没有弹框,确保切到 "设置横封面" tab
            tab = page.get_by_text("设置横封面", exact=True).first
            try:
                if await tab.count():
                    box = await tab.bounding_box()
                    if box:
                        await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, delay=60)
                    else:
                        await tab.click(timeout=2000, force=True)
                    douyin_logger.info(_msg("🗂️", "切到 '设置横封面' tab"))
                    await page.wait_for_timeout(1000)
            except Exception as e:
                douyin_logger.warning(_msg("⚠️", f"切横封面 tab 失败: {e}"))

        async def _click_done() -> None:
            await page.wait_for_timeout(800)
            done_btn = page.get_by_role("button", name="完成", exact=True).first
            if not await done_btn.count():
                done_btn = page.get_by_text("完成", exact=True).first
            if not await done_btn.count():
                raise RuntimeError("找不到 '完成' 按钮")
            box = await done_btn.bounding_box()
            if box:
                await page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, delay=80)
            else:
                await done_btn.click(timeout=3000, force=True)
            douyin_logger.info(_msg("🟢", "点了 '完成'"))
            for _ in range(20):  # 等 modal 关闭(设置竖封面 tab 消失)
                try:
                    if not await page.locator('text="设置竖封面"').count():
                        douyin_logger.success(_msg("🥳", "封面 modal 已关闭,封面已提交"))
                        return
                except Exception:
                    pass
                await page.wait_for_timeout(500)
            douyin_logger.warning(_msg("⚠️", "点完成后 modal 没在 10s 内关闭"))

        # === 流程 ===
        # 1) 打开封面 modal:点 inline "选择封面"(带重试 + 加长等待 —— 页面在跑"快速检测"或视频
        #    还在处理时,inline 封面区会迟迟不就绪/点了不弹,之前固定 10s 偶发超时回退首帧)
        async def _open_cover_modal() -> bool:
            for attempt in range(4):
                choose = page.get_by_text("选择封面", exact=True).first
                if not await choose.count():
                    choose = page.get_by_role("button", name="选择封面").first
                if not await choose.count():
                    await page.wait_for_timeout(2000)
                    continue
                try:
                    await choose.scroll_into_view_if_needed(timeout=3000)
                except Exception:
                    pass
                try:
                    await choose.click(timeout=5000, force=True)
                except Exception as e:
                    douyin_logger.warning(_msg("⚠️", f"点'选择封面'失败({attempt + 1}/4): {e}"))
                    await page.wait_for_timeout(1500)
                    continue
                for _ in range(36):  # 等"上传封面"出现,最多 ~18s
                    if await page.locator('text="上传封面"').count():
                        return True
                    await page.wait_for_timeout(500)
                douyin_logger.warning(_msg("⚠️", f"点'选择封面'后 modal ~18s 没开,重试({attempt + 1}/4)"))
            return False

        if not await _open_cover_modal():
            await _shot("modal没打开")
            raise RuntimeError("点 '选择封面' 后封面 modal 没打开(已重试4次)")
        douyin_logger.info(_msg("📦", "封面 modal 已打开"))
        await page.wait_for_timeout(1000)
        await _shot("modal已开")

        # 2) 竖封面(默认就在 "设置竖封面" tab,保险起见再点一下)
        if portrait:
            ptab = page.get_by_text("设置竖封面", exact=True).first
            try:
                if await ptab.count():
                    await ptab.click(timeout=2000, force=True)
                    await page.wait_for_timeout(500)
            except Exception:
                pass
            await _upload_via_button(portrait, "竖封面")
            await _shot("竖封面已传")

        # 3) 横封面
        if landscape:
            await _go_landscape_tab()
            await _upload_via_button(landscape, "横封面")
            await _shot("横封面已传")

        # 4) 完成
        await _click_done()
        await _shot("完成后")

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
        # 抖音创作中心首屏资源多,默认 wait_until="load" + 30s 经常不够;放宽到 domcontentloaded + 60s
        await page.goto(
            "https://creator.douyin.com/creator-micro/content/upload", wait_until="domcontentloaded", timeout=60000
        )
        douyin_logger.info(_msg("🏃", f"小人开始搬运视频: {self.title}.mp4"))
        douyin_logger.info(_msg("🧭", "小人正在赶往上传主页"))
        await page.wait_for_url("https://creator.douyin.com/creator-micro/content/upload", timeout=30000)
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

        # 调试开关:封面 + 定时都设好后停下、不真发帖(避免测试发重复)。set SAU_DY_COVER_TEST=1 启用。
        if os.environ.get("SAU_DY_COVER_TEST"):
            try:
                shot = f"/tmp/sau_dy_cover_test_final_{int(asyncio.get_event_loop().time())}.png"
                await page.screenshot(path=shot, full_page=True)
                douyin_logger.success(_msg("🧪", f"SAU_DY_COVER_TEST: 封面+定时已设,跳过发布。终态截图: {shot}"))
            except Exception:
                douyin_logger.success(_msg("🧪", "SAU_DY_COVER_TEST: 封面+定时已设,跳过发布"))
            await asyncio.sleep(2)
            await context.close()
            await browser.close()
            return

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
