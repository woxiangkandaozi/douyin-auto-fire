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


SEND_BUTTONS = (
    '[class*="messageMsgInputpublishBtn"]',
    ".e2e-send-msg-bt",
    'button[aria-label*="发送"]',
    '[role="button"][aria-label*="发送"]',
)

LATEST_OUTGOING_MESSAGE = (
    '.messageMessageListlist [data-index="0"] '
    ".messageMessageBoxmessageBox:has("
    ".messageMessageBoxcontentBox.messageMessageBoxisFromMe"
    ")"
)

MESSAGE_CONFIRM_ANCHOR = "data-douyin-sender-anchor"

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


async def _get_editor_text(editor: Locator) -> str:
    """
    尝试获取聊天输入框当前内容。

    Douyin 的输入框可能是：
    - textarea
    - input
    - contenteditable div
    - 带 contenteditable 子节点的 wrapper
    """

    # 1. inner_text
    try:
        value = await editor.inner_text(timeout=1000)
        if value:
            return value
    except Exception:
        pass

    # 2. text_content
    try:
        value = await editor.text_content(timeout=1000)
        if value:
            return value
    except Exception:
        pass

    # 3. input_value
    try:
        value = await editor.input_value(timeout=1000)
        if value:
            return value
    except Exception:
        pass

    # 4. contenteditable 属性
    try:
        value = await editor.get_attribute("contenteditable")
        if value == "true":
            text = await editor.inner_text(timeout=1000)
            return text or ""
    except Exception:
        pass

    return ""


async def _find_real_editable(editor: Locator) -> Locator:
    """
    message_input() 有可能返回 wrapper，而不是实际可以输入文字的元素。

    因此这里进一步寻找：
    textarea / input / contenteditable。
    """

    candidates = [
        editor,
        editor.locator("textarea"),
        editor.locator("input"),
        editor.locator('[contenteditable="true"]'),
        editor.locator('[contenteditable="plaintext-only"]'),
        editor.locator('[role="textbox"]'),
    ]

    for candidate in candidates:
        try:
            count = await candidate.count()
        except Exception:
            continue

        if count <= 0:
            continue

        for index in range(min(count, 5)):
            item = candidate.nth(index)

            try:
                if not await item.is_visible():
                    continue
            except Exception:
                continue

            try:
                editable = await item.is_editable()
                if editable:
                    return item
            except Exception:
                pass

            # contenteditable 元素有时候 is_editable() 判断不稳定
            try:
                ce = await item.get_attribute("contenteditable")
                if ce in ("true", "plaintext-only"):
                    return item
            except Exception:
                pass

    return editor


async def _wait_editor_text(
    editor: Locator,
    expected: str,
    timeout_ms: int = 5000,
) -> bool:
    """
    使用 Python + Playwright 轮询输入框内容。

    不使用 page.wait_for_function()，避免 GitHub Actions
    上出现 JavaScript 解析问题。
    """

    deadline = asyncio.get_running_loop().time() + timeout_ms / 1000

    while asyncio.get_running_loop().time() < deadline:
        try:
            current = await _get_editor_text(editor)

            if current == expected:
                return True

            # 某些 contenteditable 会把换行、空格处理掉，
            # 因此这里允许包含完整目标文本。
            if expected and expected in current:
                return True

        except Exception:
            pass

        await asyncio.sleep(0.15)

    return False


async def _print_editor_debug(editor: Locator) -> None:
    """
    打印输入框详细信息，方便 GitHub Actions 日志定位真正的 DOM。
    """

    print()
    print("=" * 70)
    print("输入框调试信息")
    print("=" * 70)

    try:
        count = await editor.count()
        print(f"元素数量: {count}")
    except Exception as exc:
        print(f"元素数量获取失败: {exc}")

    try:
        print(f"可见: {await editor.is_visible()}")
    except Exception as exc:
        print(f"可见状态获取失败: {exc}")

    try:
        print(f"可编辑: {await editor.is_editable()}")
    except Exception as exc:
        print(f"可编辑状态获取失败: {exc}")

    for attr in (
        "tagName",
        "contenteditable",
        "role",
        "class",
        "placeholder",
        "aria-label",
    ):
        try:
            if attr == "tagName":
                value = await editor.evaluate(
                    "(el) => el.tagName"
                )
            else:
                value = await editor.get_attribute(attr)

            print(f"{attr}: {value}")
        except Exception as exc:
            print(f"{attr}: 获取失败 ({exc})")

    try:
        print(f"当前文本: {await _get_editor_text(editor)!r}")
    except Exception as exc:
        print(f"当前文本获取失败: {exc}")

    try:
        html = await editor.evaluate(
            "(el) => el.outerHTML"
        )

        if html:
            # 防止日志太长
            if len(html) > 8000:
                html = html[:8000] + "...[truncated]"

            print()
            print("输入框 HTML:")
            print(html)

    except Exception as exc:
        print(f"outerHTML 获取失败: {exc}")

    print("=" * 70)
    print()


async def _clear_editor(editor: Locator) -> None:
    """
    尝试清空输入框。
    """

    # textarea / input
    try:
        await editor.fill("")
        return
    except Exception:
        pass

    # contenteditable
    try:
        await editor.click(force=True)
        await editor.press("Control+A")
        await editor.press("Backspace")
        return
    except Exception:
        pass

    # Mac runner 兜底
    try:
        await editor.click(force=True)
        await editor.press("Meta+A")
        await editor.press("Backspace")
    except Exception:
        pass


async def _input_text_with_fallback(
    editor: Locator,
    content: str,
) -> bool:
    """
    向聊天输入框写入文字。

    按以下顺序尝试：

    1. fill()
    2. click + press_sequentially()
    3. click + keyboard.insert_text()
    4. contenteditable 子节点
    5. role=textbox 子节点
    """

    # ============================================================
    # 方法 1：直接 fill
    # ============================================================

    try:
        await _clear_editor(editor)

        await editor.fill(content)

        if await _wait_editor_text(editor, content, 1500):
            print("文字输入方式: fill()")
            return True

    except Exception as exc:
        print(f"fill() 输入失败: {exc}")

    # ============================================================
    # 方法 2：逐字输入
    # ============================================================

    try:
        await _clear_editor(editor)

        await editor.click(force=True)
        await editor.press_sequentially(
            content,
            delay=20,
        )

        if await _wait_editor_text(editor, content, 3000):
            print("文字输入方式: press_sequentially()")
            return True

    except Exception as exc:
        print(f"press_sequentially() 输入失败: {exc}")

    # ============================================================
    # 方法 3：keyboard.insert_text
    # ============================================================

    try:
        await _clear_editor(editor)

        page = editor.page

        await editor.click(force=True)

        try:
            await editor.focus()
        except Exception:
            pass

        await page.keyboard.insert_text(content)

        if await _wait_editor_text(editor, content, 3000):
            print("文字输入方式: keyboard.insert_text()")
            return True

    except Exception as exc:
        print(f"keyboard.insert_text() 输入失败: {exc}")

    # ============================================================
    # 方法 4：寻找真正的 contenteditable
    # ============================================================

    nested_candidates = [
        editor.locator('[contenteditable="true"]'),
        editor.locator('[contenteditable="plaintext-only"]'),
        editor.locator('[role="textbox"]'),
        editor.locator("textarea"),
        editor.locator("input"),
    ]

    for candidate in nested_candidates:
        try:
            count = await candidate.count()
        except Exception:
            continue

        if count <= 0:
            continue

        for index in range(min(count, 10)):
            child = candidate.nth(index)

            try:
                if not await child.is_visible():
                    continue
            except Exception:
                continue

            # ----------------------------------------------------
            # 4.1 fill
            # ----------------------------------------------------

            try:
                await _clear_editor(child)
                await child.fill(content)

                if await _wait_editor_text(child, content, 1500):
                    print(
                        "文字输入方式: nested.fill()"
                    )
                    return True

            except Exception:
                pass

            # ----------------------------------------------------
            # 4.2 press_sequentially
            # ----------------------------------------------------

            try:
                await _clear_editor(child)
                await child.click(force=True)
                await child.press_sequentially(
                    content,
                    delay=20,
                )

                if await _wait_editor_text(child, content, 2500):
                    print(
                        "文字输入方式: "
                        "nested.press_sequentially()"
                    )
                    return True

            except Exception:
                pass

            # ----------------------------------------------------
            # 4.3 insert_text
            # ----------------------------------------------------

            try:
                await _clear_editor(child)
                await child.click(force=True)

                page = child.page

                try:
                    await child.focus()
                except Exception:
                    pass

                await page.keyboard.insert_text(content)

                if await _wait_editor_text(child, content, 2500):
                    print(
                        "文字输入方式: "
                        "nested.keyboard.insert_text()"
                    )
                    return True

            except Exception:
                pass

    return False


async def _mark_latest_outgoing_message(page: Page) -> str | None:
    """
    给当前最新的自己发送的消息打一个临时标记。

    用于后续判断发送是否产生了新的消息。
    """

    try:
        locator = page.locator(LATEST_OUTGOING_MESSAGE).first

        if await locator.count() == 0:
            return None

        try:
            return await locator.get_attribute(
                MESSAGE_CONFIRM_ANCHOR
            )
        except Exception:
            pass

        marker = secrets.token_hex(8)

        try:
            await locator.evaluate(
                """
                (el, data) => {
                    el.setAttribute(data.name, data.value);
                }
                """,
                {
                    "name": MESSAGE_CONFIRM_ANCHOR,
                    "value": marker,
                },
            )
        except Exception:
            return None

        return marker

    except Exception:
        return None


async def _find_visible_marker(
    page: Page,
    markers: tuple[str, ...],
) -> Locator | None:
    """
    寻找页面中可见的 selector。
    """

    for selector in markers:
        try:
            locator = page.locator(selector)

            count = await locator.count()

            if count <= 0:
                continue

            for index in range(min(count, 10)):
                item = locator.nth(index)

                try:
                    if await item.is_visible():
                        return item
                except Exception:
                    continue

        except Exception:
            continue

    return None


async def _raise_send_failure(page: Page) -> None:
    """
    检查页面上是否出现发送失败标志。
    """

    marker = await _find_visible_marker(
        page,
        SEND_FAILURE_MARKERS,
    )

    if marker is None:
        return

    details = []

    try:
        text = await marker.inner_text()
        if text:
            details.append(text.strip())
    except Exception:
        pass

    try:
        title = await marker.get_attribute("title")
        if title:
            details.append(title)
    except Exception:
        pass

    try:
        aria = await marker.get_attribute("aria-label")
        if aria:
            details.append(aria)
    except Exception:
        pass

    message = "；".join(x for x in details if x)

    if not message:
        message = "页面检测到发送失败状态"

    raise PageOperationError(
        f"文字发送失败: {message}"
    )


async def _await_send_terminal_state(
    page: Page,
    timeout_ms: int = 15000,
) -> None:
    """
    等待发送进入最终状态。

    如果出现失败标记立即抛错。
    """

    deadline = (
        asyncio.get_running_loop().time()
        + timeout_ms / 1000
    )

    while asyncio.get_running_loop().time() < deadline:
        await _raise_send_failure(page)

        pending = await _find_visible_marker(
            page,
            SEND_PENDING_MARKERS,
        )

        if pending is None:
            return

        await asyncio.sleep(0.2)

    # 超时前再检查一次失败
    await _raise_send_failure(page)


async def _wait_for_new_outgoing_message(
    page: Page,
    before_marker: str | None,
    timeout_ms: int = 15000,
) -> bool:
    """
    Python/Playwright 轮询是否出现新的自己发送的消息。
    """

    deadline = (
        asyncio.get_running_loop().time()
        + timeout_ms / 1000
    )

    while asyncio.get_running_loop().time() < deadline:
        await _raise_send_failure(page)

        try:
            locator = page.locator(
                LATEST_OUTGOING_MESSAGE
            ).first

            if await locator.count() > 0:
                marker = await locator.get_attribute(
                    MESSAGE_CONFIRM_ANCHOR
                )

                if before_marker is None:
                    return True

                if marker != before_marker:
                    return True

        except Exception:
            pass

        await asyncio.sleep(0.2)

    return False


async def _trigger_send(page: Page) -> None:
    """
    点击发送按钮。

    多种 selector 依次尝试。
    """

    button = await first_visible(
        page,
        SEND_BUTTONS,
    )

    if button is not None:
        try:
            await button.click(
                timeout=5000,
                force=True,
            )
            return
        except Exception as exc:
            print(
                f"发送按钮 click 失败，尝试 Enter: {exc}"
            )

    # 找不到按钮或者点击失败时，尝试 Enter
    try:
        await page.keyboard.press("Enter")
        return
    except Exception as exc:
        raise PageOperationError(
            f"无法点击发送按钮，也无法按 Enter 发送: {exc}"
        )


async def _confirm_outgoing_message(
    page: Page,
    before_marker: str | None,
) -> None:
    """
    确认消息已经真正进入聊天记录。
    """

    # 先等待发送状态结束
    await _await_send_terminal_state(
        page,
        timeout_ms=10000,
    )

    # 等待新的自己发送的消息出现
    success = await _wait_for_new_outgoing_message(
        page,
        before_marker,
        timeout_ms=10000,
    )

    if success:
        return

    # 最后一轮检查失败状态
    await _raise_send_failure(page)

    raise PageOperationError(
        "文字发送后未检测到新的自己发送消息"
    )


async def send_text(
    chat: DouyinChat,
    content: str,
) -> None:
    """
    发送文字消息。

    这是本次重点修复的函数。
    """

    if not content:
        raise PageOperationError(
            "发送文字为空"
        )

    print()
    print("=" * 70)
    print(f"准备发送文字，长度: {len(content)}")
    print(f"文字内容: {content!r}")
    print("=" * 70)

    # ------------------------------------------------------------
    # 获取聊天输入框
    # ------------------------------------------------------------

    editor = await chat.message_input()

    if editor is None:
        raise PageOperationError(
            "没有找到聊天输入框"
        )

    # ------------------------------------------------------------
    # 找真正可编辑的元素
    # ------------------------------------------------------------

    editor = await _find_real_editable(editor)

    # ------------------------------------------------------------
    # 输入文字
    # ------------------------------------------------------------

    success = await _input_text_with_fallback(
        editor,
        content,
    )

    if not success:
        print()
        print("文字输入失败，输出输入框详细信息。")

        await _print_editor_debug(editor)

        raise PageOperationError(
            "文字未能写入聊天输入框"
        )

    # ------------------------------------------------------------
    # 最终确认输入框里确实存在文字
    # ------------------------------------------------------------

    ready = await _wait_editor_text(
        editor,
        content,
        timeout_ms=3000,
    )

    if not ready:
        print()
        print("输入框最终确认失败。")
        await _print_editor_debug(editor)

        raise PageOperationError(
            "文字输入框内容确认失败"
        )

    print(
        f"文字已写入输入框: "
        f"{await _get_editor_text(editor)!r}"
    )

    # ------------------------------------------------------------
    # 在发送之前记录最新消息
    # ------------------------------------------------------------

    page = editor.page

    before = await _mark_latest_outgoing_message(
        page
    )

    await page.wait_for_timeout(300)

    # ------------------------------------------------------------
    # 点击发送
    # ------------------------------------------------------------

    await _trigger_send(page)

    print("已触发发送动作。")

    # ------------------------------------------------------------
    # 确认发送结果
    # ------------------------------------------------------------

    await _confirm_outgoing_message(
        page,
        before,
    )

    print("文字发送确认成功。")


async def send_image(
    chat: DouyinChat,
    image_path: str,
) -> None:
    """
    发送图片。
    """

    page = chat.page

    input_locator = await first_visible(
        page,
        IMAGE_INPUTS,
    )

    if input_locator is None:
        raise PageOperationError(
            "没有找到图片上传输入框"
        )

    try:
        await input_locator.set_input_files(
            image_path
        )
    except Exception as exc:
        raise PageOperationError(
            f"图片上传失败: {exc}"
        )

    await page.wait_for_timeout(
        random.randint(800, 1600)
    )

    await _await_send_terminal_state(
        page,
        timeout_ms=15000,
    )


async def _open_sticker_panel(
    page: Page,
) -> Locator:
    """
    打开表情/贴纸面板。
    """

    button = await first_visible(
        page,
        STICKER_BUTTONS,
    )

    if button is None:
        raise PageOperationError(
            "没有找到表情按钮"
        )

    try:
        await button.click(
            force=True
        )
    except Exception as exc:
        raise PageOperationError(
            f"无法打开表情面板: {exc}"
        )

    await page.wait_for_timeout(500)

    panel = await first_visible(
        page,
        STICKER_PANELS,
    )

    if panel is None:
        raise PageOperationError(
            "点击表情按钮后没有找到表情面板"
        )

    return panel


async def send_sticker(
    chat: DouyinChat,
    sticker: Sticker,
) -> None:
    """
    发送贴纸。
    """

    page = chat.page

    panel = await _open_sticker_panel(
        page
    )

    # sticker selector 由 Sticker 模型提供
    selector = getattr(
        sticker,
        "selector",
        None,
    )

    if not selector:
        raise PageOperationError(
            "Sticker 缺少 selector"
        )

    try:
        item = panel.locator(
            selector
        ).first

        if await item.count() == 0:
            raise PageOperationError(
                f"表情面板中没有找到: {selector}"
            )

        await item.click(
            force=True
        )

    except PageOperationError:
        raise

    except Exception as exc:
        raise PageOperationError(
            f"点击贴纸失败: {exc}"
        )

    await page.wait_for_timeout(
        random.randint(500, 1200)
    )

    await _await_send_terminal_state(
        page,
        timeout_ms=15000,
    )


async def send_message(
    chat: DouyinChat,
    message: Message,
) -> None:
    """
    根据 Message 类型发送消息。
    """

    message_type = getattr(
        message,
        "type",
        None,
    )

    # ------------------------------------------------------------
    # 文字
    # ------------------------------------------------------------

    if message_type in (
        "text",
        "TEXT",
        None,
    ):
        content = getattr(
            message,
            "content",
            None,
        )

        if content is None:
            content = getattr(
                message,
                "text",
                None,
            )

        if content is None:
            raise PageOperationError(
                "文字消息没有 content/text"
            )

        await send_text(
            chat,
            str(content),
        )
        return

    # ------------------------------------------------------------
    # 图片
    # ------------------------------------------------------------

    if message_type in (
        "image",
        "IMAGE",
    ):
        image_path = getattr(
            message,
            "path",
            None,
        )

        if image_path is None:
            image_path = getattr(
                message,
                "image_path",
                None,
            )

        if image_path is None:
            raise PageOperationError(
                "图片消息没有 path/image_path"
            )

        await send_image(
            chat,
            str(image_path),
        )
        return

    # ------------------------------------------------------------
    # 贴纸
    # ------------------------------------------------------------

    if message_type in (
        "sticker",
        "STICKER",
        "douyin_sticker",
        "DOUYIN_STICKER",
    ):
        sticker = getattr(
            message,
            "sticker",
            None,
        )

        if sticker is None:
            raise PageOperationError(
                "贴纸消息没有 sticker"
            )

        await send_sticker(
            chat,
            sticker,
        )
        return

    # ------------------------------------------------------------
    # 随机消息
    # ------------------------------------------------------------

    if message_type in (
        "random",
        "RANDOM",
    ):
        candidates = getattr(
            message,
            "messages",
            None,
        )

        if not candidates:
            candidates = getattr(
                message,
                "contents",
                None,
            )

        if not candidates:
            raise PageOperationError(
                "随机消息没有 messages/contents"
            )

        selected = random.choice(
            candidates
        )

        if isinstance(selected, str):
            await send_text(
                chat,
                selected,
            )
            return

        await send_message(
            chat,
            selected,
        )
        return

    raise PageOperationError(
        f"不支持的消息类型: {message_type!r}"
    )
