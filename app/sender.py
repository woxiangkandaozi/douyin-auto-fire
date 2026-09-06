from __future__ import annotations

import asyncio
import random
import secrets
from urllib.parse import urlsplit

from playwright.async_api import Locator, Page

from app.douyin import DouyinChat, PageOperationError, first_visible
from app.models import Message, Sticker
from app.selectors import (
    IMAGE_INPUTS,
    MESSAGE_INPUTS,
    STICKER_BUTTONS,
    STICKER_PANELS,
)


def _monotonic() -> float:
    """Monotonic clock for the send-state deadline."""
    return asyncio.get_running_loop().time()


# ============================================================
# 发送按钮
# ============================================================

SEND_BUTTONS = (
    '[class*="messageMsgInputpublishBtn"]',
    ".e2e-send-msg-bt",
    'button[aria-label*="发送"]',
    '[role="button"][aria-label*="发送"]',
)


async def _trigger_send(page: Page) -> None:
    """
    尝试点击发送按钮。
    如果找不到发送按钮，则使用 Enter。
    """
    button = None

    for selector in SEND_BUTTONS:
        candidate = page.locator(selector).first

        try:
            if await candidate.count() and await candidate.is_visible():
                button = candidate
                break
        except Exception:
            continue

    if button is not None:
        try:
            await button.click()
            return
        except Exception:
            pass

    await page.keyboard.press("Enter")


async def _publish_ready(page: Page) -> bool:
    """判断当前页面是否存在可见的发送按钮。"""
    for selector in SEND_BUTTONS:
        candidate = page.locator(selector).first

        try:
            if await candidate.count() and await candidate.is_visible():
                return True
        except Exception:
            continue

    return False


# ============================================================
# 最新发送消息
# ============================================================

LATEST_OUTGOING_MESSAGE = (
    '.messageMessageListlist [data-index="0"] '
    ".messageMessageBoxmessageBox:has("
    ".messageMessageBoxcontentBox.messageMessageBoxisFromMe"
    ")"
)

MESSAGE_CONFIRM_ANCHOR = "data-douyin-sender-anchor"


# ============================================================
# 发送状态
# ============================================================

SEND_FAILURE_MARKERS = (
    "text=发送失败",
    '[aria-label*="重试"]',
    '[title*="重试"]',
    '[class*="sendFailed"]',
    '[class*="SendFailed"]',
    '[class*="ContentSideSendStatusretry"]',
    '[class*="SendStatusretry"]',
)


SEND_PENDING_MARKERS = (
    ".semi-spin",
    '[class*="im-saas-message-spin"]',
    '[data-icon="spin"]',
)


# ============================================================
# 时间参数
# ============================================================

SEND_CONFIRM_TIMEOUT_MS = 15_000
SEND_POLL_INTERVAL_MS = 300
SEND_STABLE_INTERVAL_MS = 500
SEND_INITIAL_CLEAN_GRACE_MS = 2_000


# ============================================================
# 发送消息总入口
# ============================================================

async def send_message(
    page: Page,
    chat: DouyinChat,
    message: Message,
    stickers: dict[str, Sticker],
) -> None:

    if message.type == "random":
        await send_message(
            page,
            chat,
            random.choice(message.choices),
            stickers,
        )
        return

    if message.type == "text":
        await send_text(
            chat,
            message.content or "",
        )
        return

    if message.type == "image":
        if message.path is None:
            raise PageOperationError("图片消息缺少文件路径")

        await send_image(
            page,
            message.path.as_posix(),
        )
        return

    if message.type == "douyin_sticker":
        sticker = stickers.get(message.sticker or "")

        if sticker is None:
            raise PageOperationError(
                f"没有原生表情映射: {message.sticker}"
            )

        await send_douyin_sticker(
            page,
            sticker,
        )
        return

    raise PageOperationError(
        f"不支持的消息类型: {message.type}"
    )


# ============================================================
# 获取输入框文字
# ============================================================

async def _get_editor_text(editor: Locator) -> str:
    """
    尽可能读取聊天输入框中的文字。

    抖音的输入框可能是 contenteditable，
    也可能是其他可编辑 DOM，因此这里依次尝试：
    inner_text / text_content / input_value。
    """

    try:
        text = await editor.inner_text()
        if text:
            return text
    except Exception:
        pass

    try:
        text = await editor.text_content()
        if text:
            return text
    except Exception:
        pass

    try:
        value = await editor.input_value()
        if value:
            return value
    except Exception:
        pass

    return ""


# ============================================================
# 等待输入框出现指定文字
# ============================================================

async def _wait_editor_text(
    editor: Locator,
    expected: str,
    timeout_ms: int = 5_000,
) -> bool:
    """
    使用 Python + Playwright 轮询输入框。

    不使用 page.wait_for_function，
    避免 Chromium 对 JavaScript 表达式解析失败。
    """

    deadline = (
        _monotonic()
        + timeout_ms / 1000
    )

    while _monotonic() < deadline:

        try:
            if await editor.count() and await editor.is_visible():

                current = await _get_editor_text(editor)

                if expected in current:
                    return True

        except Exception:
            pass

        await editor.page.wait_for_timeout(
            SEND_POLL_INTERVAL_MS
        )

    return False


# ============================================================
# 发送文字
# ============================================================

async def send_text(
    chat: DouyinChat,
    content: str,
) -> None:

    editor = await chat.message_input()
    page = editor.page

    # 点击输入框
    await editor.click()

    # 输入文字
    await page.keyboard.insert_text(content)

    # --------------------------------------------------------
    # 确认文字进入输入框
    # --------------------------------------------------------

    ready = await _wait_editor_text(
        editor,
        content,
        timeout_ms=5_000,
    )

    if not ready:
        # 输出一点调试信息，但绝不输出 Cookie 等敏感信息
        try:
            current_text = await _get_editor_text(editor)
        except Exception:
            current_text = ""

        print("")
        print("=" * 70)
        print("输入框调试信息")
        print("=" * 70)
        print(f"期望文字长度: {len(content)}")
        print(f"当前文字长度: {len(current_text)}")
        print(f"当前文字: {current_text[:500]!r}")
        print("=" * 70)
        print("")

        raise PageOperationError(
            "文字未能写入聊天输入框"
        )

    # --------------------------------------------------------
    # 记录发送前的最新消息
    # --------------------------------------------------------

    before = await _mark_latest_outgoing_message(page)

    # 给 DOM 一点时间
    await page.wait_for_timeout(300)

    # --------------------------------------------------------
    # 点击发送
    # --------------------------------------------------------

    await _trigger_send(page)

    # --------------------------------------------------------
    # 等待发送结果
    # --------------------------------------------------------

    await _confirm_outgoing_message(
        page,
        before,
        label="文字",
        expected_text=content,
    )


# ============================================================
# 发送图片
# ============================================================

async def send_image(
    page: Page,
    image_path: str,
) -> None:

    before = await _mark_latest_outgoing_message(page)

    file_input = None

    for selector in IMAGE_INPUTS:
        candidate = page.locator(selector).first

        try:
            if await candidate.count():
                file_input = candidate
                break
        except Exception:
            continue

    if file_input is None:
        raise PageOperationError(
            "找不到图片上传控件"
        )

    await file_input.set_input_files(
        image_path
    )

    # 等待图片加载
    await page.wait_for_timeout(1_500)

    # 点击发送
    await _trigger_send(page)

    try:

        if not await _wait_for_new_outgoing_message(
            page,
            before,
            timeout_ms=15_000,
        ):
            raise PageOperationError(
                "图片消息已触发发送，但无法确认新消息"
            )

        latest = page.locator(
            LATEST_OUTGOING_MESSAGE
        ).first

        await _await_send_terminal_state(
            page,
            latest,
            "图片",
        )

    except PageOperationError:
        raise

    except Exception as exc:
        raise PageOperationError(
            "图片消息已触发发送，但无法确认是否发送成功；"
            "为避免重复不会自动重试"
        ) from exc


# ============================================================
# 恢复输入框
# ============================================================

async def _restore_composer(
    page: Page,
    timeout_ms: int = 10_000,
) -> None:

    try:
        await page.keyboard.press("Escape")
    except Exception:
        pass

    try:
        editor = await first_visible(
            page,
            MESSAGE_INPUTS,
            timeout_ms,
        )
    except Exception:
        return

    try:
        await editor.click(
            timeout=timeout_ms
        )

        await editor.focus()

    except Exception:
        pass


# ============================================================
# 发送抖音原生表情
# ============================================================

async def send_douyin_sticker(
    page: Page,
    sticker: Sticker,
) -> None:

    before = await _mark_latest_outgoing_message(
        page
    )

    try:

        button = await first_visible(
            page,
            STICKER_BUTTONS,
        )

        await button.click(
            force=True
        )

        panel = await first_visible(
            page,
            STICKER_PANELS,
        )

        # 如果指定了分类
        if sticker.category:

            category = panel.get_by_text(
                sticker.category,
                exact=True,
            )

            if (
                await category.count()
                and await category.first.is_visible()
            ):
                await category.first.click()

        name = (
            sticker.accessible_name
            or sticker.name
        )

        # ----------------------------------------------------
        # 优先通过描述寻找
        # ----------------------------------------------------

        item = panel.locator(
            ".emojiEmojiItememojiItem"
        ).filter(
            has_text=name
        )

        for index in range(
            await item.count()
        ):

            candidate = item.nth(index)

            description = candidate.locator(
                ".emojiEmojiItememojiItemDesc"
            )

            if await description.count():

                text = (
                    await description.first.inner_text()
                ).strip()

                if text == name:

                    await _click_and_confirm_sticker(
                        page,
                        candidate,
                        before,
                        name,
                    )

                    return

        # ----------------------------------------------------
        # 备用 selector
        # ----------------------------------------------------

        candidates = (
            panel.get_by_role(
                "img",
                name=name,
                exact=True,
            ),
            panel.get_by_role(
                "button",
                name=name,
                exact=True,
            ),
            panel.locator(
                f'[aria-label="{_css_escape(name)}"]'
            ),
            panel.locator(
                f'[title="{_css_escape(name)}"]'
            ),
            panel.locator(
                f'[alt="{_css_escape(name)}"]'
            ),
        )

        for candidate in candidates:

            try:
                if (
                    await candidate.count()
                    and await candidate.first.is_visible()
                ):

                    await _click_and_confirm_sticker(
                        page,
                        candidate.first,
                        before,
                        name,
                    )

                    return

            except Exception:
                continue

        # ----------------------------------------------------
        # 最后使用 fallback index
        # ----------------------------------------------------

        if sticker.fallback_index is not None:

            items = panel.locator(
                '[role="button"], img, [aria-label], [title]'
            )

            if (
                await items.count()
                > sticker.fallback_index
            ):

                await _click_and_confirm_sticker(
                    page,
                    items.nth(
                        sticker.fallback_index
                    ),
                    before,
                    name,
                )

                return

        raise PageOperationError(
            f"在抖音表情面板中找不到原生表情: "
            f"{sticker.name}"
        )

    finally:

        await _restore_composer(
            page
        )


# ============================================================
# CSS 转义
# ============================================================

def _css_escape(
    value: str,
) -> str:

    return value.replace(
        "\\",
        "\\\\",
    ).replace(
        '"',
        '\\"',
    )


# ============================================================
# 标记当前最新消息
# ============================================================

async def _mark_latest_outgoing_message(
    page: Page,
) -> tuple[str, str]:

    anchor = secrets.token_hex(8)

    latest = page.locator(
        LATEST_OUTGOING_MESSAGE
    ).first

    if not await latest.count():
        return anchor, ""

    content = latest.locator(
        '[data-e2e="msg-item-content"]'
    ).first

    if await content.count():

        before_content = (
            await content.inner_html()
        )

    else:

        before_content = (
            await latest.inner_html()
        )

    try:
        await latest.evaluate(
            """(element, value) => {
                element.setAttribute(
                    'data-douyin-sender-anchor',
                    value
                );
            }""",
            anchor,
        )
    except Exception:
        pass

    return anchor, before_content


# ============================================================
# 检查最新消息是否发生变化
# ============================================================

async def _latest_message_changed(
    page: Page,
    before: tuple[str, str],
) -> bool:

    anchor, before_content = before

    latest = page.locator(
        LATEST_OUTGOING_MESSAGE
    ).first

    try:

        if not await latest.count():
            return False

        current_anchor = await latest.get_attribute(
            MESSAGE_CONFIRM_ANCHOR
        )

        if current_anchor != anchor:
            return True

        content = latest.locator(
            '[data-e2e="msg-item-content"]'
        ).first

        if await content.count():
            current_content = await content.inner_html()
        else:
            current_content = await latest.inner_html()

        return current_content != before_content

    except Exception:
        return False


# ============================================================
# 等待新的发送消息
# ============================================================

async def _wait_for_new_outgoing_message(
    page: Page,
    before: tuple[str, str],
    timeout_ms: int = 15_000,
    expected_text: str = "",
    expected_resource: str = "",
) -> bool:

    deadline = (
        _monotonic()
        + timeout_ms / 1000
    )

    while _monotonic() < deadline:

        try:

            if await _latest_message_changed(
                page,
                before,
            ):

                latest = page.locator(
                    LATEST_OUTGOING_MESSAGE
                ).first

                content = latest.locator(
                    '[data-e2e="msg-item-content"]'
                ).first

                if not await content.count():
                    content = latest

                # ------------------------------------------------
                # 如果是文字消息
                # ------------------------------------------------

                if expected_text:

                    text = ""

                    try:
                        text = await content.inner_text()
                    except Exception:
                        pass

                    normalized_actual = (
                        " ".join(
                            (text or "").split()
                        )
                    )

                    normalized_expected = (
                        " ".join(
                            (expected_text or "").split()
                        )
                    )

                    if (
                        normalized_expected
                        and normalized_expected
                        not in normalized_actual
                    ):
                        await page.wait_for_timeout(
                            SEND_POLL_INTERVAL_MS
                        )
                        continue

                # ------------------------------------------------
                # 如果是图片/表情资源
                # ------------------------------------------------

                if expected_resource:

                    images = content.locator("img")

                    try:
                        image_count = await images.count()
                    except Exception:
                        image_count = 0

                    if image_count == 0:
                        await page.wait_for_timeout(
                            SEND_POLL_INTERVAL_MS
                        )
                        continue

                    if expected_resource:

                        resource_found = False

                        for index in range(image_count):

                            image = images.nth(index)

                            try:
                                src = (
                                    await image.get_attribute(
                                        "src"
                                    )
                                    or ""
                                )

                                if (
                                    expected_resource
                                    in src
                                ):
                                    resource_found = True
                                    break

                            except Exception:
                                continue

                        # 有图片就允许继续。
                        # 某些情况下 CDN URL 会发生变化。
                        if not resource_found:
                            pass

                return True

        except Exception:
            pass

        await page.wait_for_timeout(
            SEND_POLL_INTERVAL_MS
        )

    return False


# ============================================================
# 点击并确认表情
# ============================================================

async def _click_and_confirm_sticker(
    page: Page,
    item,
    before: tuple[str, str],
    name: str,
) -> None:

    resource_key = (
        await _sticker_resource_key(item)
    )

    await item.click(
        force=True
    )

    try:

        await _confirm_sticker_sent(
            page,
            before,
            name,
            resource_key,
        )

    except PageOperationError:

        if await _publish_ready(page):

            await _trigger_send(page)

            await _confirm_sticker_sent(
                page,
                before,
                name,
                resource_key,
            )

        else:
            raise


# ============================================================
# 获取表情资源 Key
# ============================================================

async def _sticker_resource_key(
    item,
) -> str:

    src = await item.get_attribute(
        "src"
    )

    if not src:

        image = item.locator(
            "img"
        ).first

        if await image.count():
            src = await image.get_attribute(
                "src"
            )

    if not src:
        return ""

    return urlsplit(
        src
    ).path.rsplit(
        "/",
        1
    )[-1]


# ============================================================
# 确认表情发送
# ============================================================

async def _confirm_sticker_sent(
    page: Page,
    before: tuple[str, str],
    name: str,
    resource_key: str = "",
) -> None:

    await _confirm_outgoing_message(
        page,
        before,
        f"原生表情“{name}”",
        resource_key=resource_key,
    )


# ============================================================
# 查找可见状态元素
# ============================================================

async def _marker_visible(
    scope: Locator,
    selectors: tuple[str, ...],
) -> bool:

    for selector in selectors:

        marker = scope.locator(
            selector
        ).first

        try:

            if (
                await marker.count()
                and await marker.is_visible()
            ):
                return True

        except Exception:
            continue

    return False


# ============================================================
# 调试：返回实际命中的失败 selector
# ============================================================

async def _find_visible_marker(
    scope: Locator,
    selectors: tuple[str, ...],
) -> tuple[str, str] | None:

    for selector in selectors:

        marker = scope.locator(
            selector
        ).first

        try:

            if (
                await marker.count()
                and await marker.is_visible()
            ):

                try:

                    html = await marker.evaluate(
                        "(element) => element.outerHTML"
                    )

                except Exception:

                    html = "<无法读取 outerHTML>"

                return (
                    selector,
                    html[:2000],
                )

        except Exception:
            continue

    return None


# ============================================================
# 输出详细失败信息
# ============================================================

async def _raise_send_failure(
    scope: Locator,
    label: str,
) -> None:

    failure = await _find_visible_marker(
        scope,
        SEND_FAILURE_MARKERS,
    )

    if failure is None:

        raise PageOperationError(
            f"{label}发送失败，页面提示可以重试"
        )

    selector, html = failure

    try:

        marker = scope.locator(
            selector
        ).first

        text = (
            await marker.inner_text()
        ).strip()

    except Exception:

        text = ""

    try:

        marker = scope.locator(
            selector
        ).first

        aria_label = (
            await marker.get_attribute(
                "aria-label"
            )
        )

    except Exception:

        aria_label = None

    try:

        marker = scope.locator(
            selector
        ).first

        title = (
            await marker.get_attribute(
                "title"
            )
        )

    except Exception:

        title = None

    print("")
    print("=" * 70)
    print("发送失败调试信息")
    print("=" * 70)
    print(f"消息类型: {label}")
    print(f"命中 selector: {selector}")
    print(f"元素文本: {text!r}")
    print(f"aria-label: {aria_label!r}")
    print(f"title: {title!r}")
    print(f"outerHTML: {html}")
    print("=" * 70)
    print("")

    raise PageOperationError(
        f"{label}发送失败，页面提示可以重试 "
        f"(命中 selector: {selector})"
    )


# ============================================================
# 等待发送状态
# ============================================================

async def _await_send_terminal_state(
    page: Page,
    scope: Locator,
    label: str,
    timeout_ms: int = SEND_CONFIRM_TIMEOUT_MS,
) -> None:

    deadline = (
        _monotonic()
        + timeout_ms / 1000
    )

    # ========================================================
    # 第一阶段：观察刚出现的消息
    # ========================================================

    grace_deadline = (
        _monotonic()
        + SEND_INITIAL_CLEAN_GRACE_MS / 1000
    )

    while _monotonic() < grace_deadline:

        if _monotonic() >= deadline:

            raise PageOperationError(
                f"{label}发送状态未能确认"
                "（发送超时或状态不确定），"
                "为避免重复不会自动重试"
            )

        # ----------------------------------------------------
        # 检查失败
        # ----------------------------------------------------

        failure = await _find_visible_marker(
            scope,
            SEND_FAILURE_MARKERS,
        )

        if failure:

            await _raise_send_failure(
                scope,
                label,
            )

        # ----------------------------------------------------
        # 检查 spinner
        # ----------------------------------------------------

        if await _marker_visible(
            scope,
            SEND_PENDING_MARKERS,
        ):

            break

        await page.wait_for_timeout(
            SEND_POLL_INTERVAL_MS
        )

    else:

        # 连续 2 秒没有失败，也没有 spinner，
        # 认为发送成功。
        return

    # ========================================================
    # 第二阶段：spinner 已出现
    # ========================================================

    while True:

        if _monotonic() >= deadline:

            raise PageOperationError(
                f"{label}发送状态未能确认"
                "（发送超时或状态不确定），"
                "为避免重复不会自动重试"
            )

        # ----------------------------------------------------
        # 检查失败
        # ----------------------------------------------------

        failure = await _find_visible_marker(
            scope,
            SEND_FAILURE_MARKERS,
        )

        if failure:

            await _raise_send_failure(
                scope,
                label,
            )

        # ----------------------------------------------------
        # spinner 消失
        # ----------------------------------------------------

        if not await _marker_visible(
            scope,
            SEND_PENDING_MARKERS,
        ):

            await page.wait_for_timeout(
                SEND_STABLE_INTERVAL_MS
            )

            # 再次检查失败
            failure = await _find_visible_marker(
                scope,
                SEND_FAILURE_MARKERS,
            )

            if failure:

                await _raise_send_failure(
                    scope,
                    label,
                )

            # spinner 仍然没有回来
            if not await _marker_visible(
                scope,
                SEND_PENDING_MARKERS,
            ):

                return

        await page.wait_for_timeout(
            SEND_POLL_INTERVAL_MS
        )


# ============================================================
# 确认最新消息
# ============================================================

async def _confirm_outgoing_message(
    page: Page,
    before: tuple[str, str],
    label: str,
    resource_key: str = "",
    expected_text: str = "",
) -> None:

    try:

        # ----------------------------------------------------
        # 等待新的消息气泡出现
        # ----------------------------------------------------

        found = await _wait_for_new_outgoing_message(
            page,
            before,
            timeout_ms=15_000,
            expected_text=expected_text,
            expected_resource=resource_key,
        )

        if not found:

            raise PageOperationError(
                f"{label}已触发发送，"
                "但没有检测到新的已发送消息"
            )

        # ----------------------------------------------------
        # 找到新的消息
        # ----------------------------------------------------

        latest = page.locator(
            LATEST_OUTGOING_MESSAGE
        ).first

        # ----------------------------------------------------
        # 等待真正的发送状态
        # ----------------------------------------------------

        await _await_send_terminal_state(
            page,
            latest,
            label,
        )

    except PageOperationError:
        raise

    except Exception as exc:

        raise PageOperationError(
            f"{label}已触发发送，"
            "但没有检测到新的已发送消息"
        ) from exc

    finally:

        # ----------------------------------------------------
        # 清除 anchor
        # ----------------------------------------------------

        anchors = page.locator(
            f"[{MESSAGE_CONFIRM_ANCHOR}]"
        )

        try:

            await anchors.evaluate_all(
                """elements =>
                    elements.forEach(
                        element =>
                            element.removeAttribute(
                                'data-douyin-sender-anchor'
                            )
                    )
                """
            )

        except Exception:
            pass
