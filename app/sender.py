from __future__ import annotations

import asyncio
import random
import secrets

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
    获取聊天输入框当前内容。

    Douyin 输入框可能是：
    - textarea
    - input
    - contenteditable
    - contenteditable 外层 wrapper
    """

    try:
        value = await editor.inner_text(timeout=1000)
        if value:
            return value
    except Exception:
        pass

    try:
        value = await editor.text_content(timeout=1000)
        if value:
            return value
    except Exception:
        pass

    try:
        value = await editor.input_value(timeout=1000)
        if value:
            return value
    except Exception:
        pass

    try:
        contenteditable = await editor.get_attribute(
            "contenteditable"
        )

        if contenteditable in (
            "true",
            "plaintext-only",
        ):
            value = await editor.inner_text(timeout=1000)
            return value or ""
    except Exception:
        pass

    return ""


async def _find_real_editable(editor: Locator) -> Locator:
    """
    message_input() 有可能返回输入框 wrapper。

    这里继续寻找真正可编辑的元素。
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

        for index in range(min(count, 10)):
            item = candidate.nth(index)

            try:
                if not await item.is_visible():
                    continue
            except Exception:
                continue

            try:
                if await item.is_editable():
                    return item
            except Exception:
                pass

            try:
                contenteditable = await item.get_attribute(
                    "contenteditable"
                )

                if contenteditable in (
                    "true",
                    "plaintext-only",
                ):
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

    不使用 page.wait_for_function()。
    """

    deadline = (
        asyncio.get_running_loop().time()
        + timeout_ms / 1000
    )

    while asyncio.get_running_loop().time() < deadline:
        try:
            current = await _get_editor_text(editor)

            if current == expected:
                return True

            if expected and expected in current:
                return True

        except Exception:
            pass

        await asyncio.sleep(0.15)

    return False


async def _print_editor_debug(editor: Locator) -> None:
    """
    输出输入框详细 DOM 信息。
    """

    print()
    print("=" * 70)
    print("输入框调试信息")
    print("=" * 70)

    try:
        print(f"元素数量: {await editor.count()}")
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

    try:
        tag_name = await editor.evaluate(
            "(el) => el.tagName"
        )
        print(f"tagName: {tag_name}")
    except Exception as exc:
        print(f"tagName 获取失败: {exc}")

    for attr in (
        "contenteditable",
        "role",
        "class",
        "placeholder",
        "aria-label",
    ):
        try:
            value = await editor.get_attribute(attr)
            print(f"{attr}: {value}")
        except Exception as exc:
            print(f"{attr}: 获取失败 ({exc})")

    try:
        value = await _get_editor_text(editor)
        print(f"当前文本: {value!r}")
    except Exception as exc:
        print(f"当前文本获取失败: {exc}")

    try:
        html = await editor.evaluate(
            "(el) => el.outerHTML"
        )

        if html:
            if len(html) > 8000:
                html = (
                    html[:8000]
                    + "...[truncated]"
                )

            print()
            print("输入框 HTML:")
            print(html)

    except Exception as exc:
        print(f"outerHTML 获取失败: {exc}")

    print("=" * 70)
    print()


async def _clear_editor(editor: Locator) -> None:
    """
    清空输入框。
    """

    try:
        await editor.fill("")
        return
    except Exception:
        pass

    try:
        await editor.click(force=True)
        await editor.press("Control+A")
        await editor.press("Backspace")
        return
    except Exception:
        pass

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
    多种方式尝试输入文字：

    1. fill
    2. press_sequentially
    3. keyboard.insert_text
    4. 寻找真正的 contenteditable
    5. 寻找 textarea/input/role=textbox
    """

    # ============================================================
    # 方法 1：fill()
    # ============================================================

    try:
        await _clear_editor(editor)

        await editor.fill(content)

        if await _wait_editor_text(
            editor,
            content,
            1500,
        ):
            print("文字输入方式: fill()")
            return True

    except Exception as exc:
        print(
            f"fill() 输入失败: {exc}"
        )

    # ============================================================
    # 方法 2：press_sequentially()
    # ============================================================

    try:
        await _clear_editor(editor)

        await editor.click(force=True)

        await editor.press_sequentially(
            content,
            delay=20,
        )

        if await _wait_editor_text(
            editor,
            content,
            3000,
        ):
            print(
                "文字输入方式: "
                "press_sequentially()"
            )
            return True

    except Exception as exc:
        print(
            "press_sequentially() "
            f"输入失败: {exc}"
        )

    # ============================================================
    # 方法 3：keyboard.insert_text()
    # ============================================================

    try:
        await _clear_editor(editor)

        page = editor.page

        await editor.click(force=True)

        try:
            await editor.focus()
        except Exception:
            pass

        await page.keyboard.insert_text(
            content
        )

        if await _wait_editor_text(
            editor,
            content,
            3000,
        ):
            print(
                "文字输入方式: "
                "keyboard.insert_text()"
            )
            return True

    except Exception as exc:
        print(
            "keyboard.insert_text() "
            f"输入失败: {exc}"
        )

    # ============================================================
    # 方法 4：寻找真正的子输入框
    # ============================================================

    nested_candidates = [
        editor.locator(
            '[contenteditable="true"]'
        ),
        editor.locator(
            '[contenteditable="plaintext-only"]'
        ),
        editor.locator(
            '[role="textbox"]'
        ),
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

        for index in range(
            min(count, 10)
        ):
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

                if await _wait_editor_text(
                    child,
                    content,
                    1500,
                ):
                    print(
                        "文字输入方式: "
                        "nested.fill()"
                    )
                    return True

            except Exception:
                pass

            # ----------------------------------------------------
            # 4.2 press_sequentially
            # ----------------------------------------------------

            try:
                await _clear_editor(child)

                await child.click(
                    force=True
                )

                await child.press_sequentially(
                    content,
                    delay=20,
                )

                if await _wait_editor_text(
                    child,
                    content,
                    2500,
                ):
                    print(
                        "文字输入方式: "
                        "nested.press_sequentially()"
                    )
                    return True

            except Exception:
                pass

            # ----------------------------------------------------
            # 4.3 keyboard.insert_text
            # ----------------------------------------------------

            try:
                await _clear_editor(child)

                await child.click(
                    force=True
                )

                page = child.page

                try:
                    await child.focus()
                except Exception:
                    pass

                await page.keyboard.insert_text(
                    content
                )

                if await _wait_editor_text(
                    child,
                    content,
                    2500,
                ):
                    print(
                        "文字输入方式: "
                        "nested.keyboard.insert_text()"
                    )
                    return True

            except Exception:
                pass

    return False


async def _mark_latest_outgoing_message(
    page: Page,
) -> str | None:
    """
    给当前最新的自己发送消息打临时标记。
    """

    try:
        locator = page.locator(
            LATEST_OUTGOING_MESSAGE
        ).first

        if await locator.count() == 0:
            return None

        try:
            existing = await locator.get_attribute(
                MESSAGE_CONFIRM_ANCHOR
            )

            if existing:
                return existing
        except Exception:
            pass

        marker = secrets.token_hex(8)

        try:
            await locator.evaluate(
                """
                (el, data) => {
                    el.setAttribute(
                        data.name,
                        data.value
                    );
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
    查找页面中可见的 selector。
    """

    for selector in markers:
        try:
            locator = page.locator(
                selector
            )

            count = await locator.count()

            if count <= 0:
                continue

            for index in range(
                min(count, 10)
            ):
                item = locator.nth(index)

                try:
                    if await item.is_visible():
                        return item
                except Exception:
                    continue

        except Exception:
            continue

    return None


async def _raise_send_failure(
    page: Page,
) -> None:
    """
    检查页面是否出现发送失败。
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
            details.append(
                text.strip()
            )
    except Exception:
        pass

    try:
        title = await marker.get_attribute(
            "title"
        )

        if title:
            details.append(title)
    except Exception:
        pass

    try:
        aria = await marker.get_attribute(
            "aria-label"
        )

        if aria:
            details.append(aria)
    except Exception:
        pass

    message = "；".join(
        x for x in details if x
    )

    if not message:
        message = (
            "页面检测到发送失败状态"
        )

    raise PageOperationError(
        f"文字发送失败: {message}"
    )


async def _await_send_terminal_state(
    page: Page,
    timeout_ms: int = 15000,
) -> None:
    """
    等待发送状态结束。
    """

    deadline = (
        asyncio.get_running_loop().time()
        + timeout_ms / 1000
    )

    while (
        asyncio.get_running_loop().time()
        < deadline
    ):
        await _raise_send_failure(page)

        pending = await _find_visible_marker(
            page,
            SEND_PENDING_MARKERS,
        )

        if pending is None:
            return

        await asyncio.sleep(0.2)

    await _raise_send_failure(page)


async def _wait_for_new_outgoing_message(
    page: Page,
    before_marker: str | None,
    timeout_ms: int = 15000,
) -> bool:
    """
    等待新的自己发送消息出现。
    """

    deadline = (
        asyncio.get_running_loop().time()
        + timeout_ms / 1000
    )

    while (
        asyncio.get_running_loop().time()
        < deadline
    ):
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


async def _trigger_send(
    page: Page,
) -> None:
    """
    点击发送按钮。

    找不到发送按钮时使用 Enter。
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
                "发送按钮 click 失败，"
                f"尝试 Enter: {exc}"
            )

    try:
        await page.keyboard.press(
            "Enter"
        )
        return
    except Exception as exc:
        raise PageOperationError(
            "无法点击发送按钮，也无法按 Enter 发送: "
            f"{exc}"
        )


async def _confirm_outgoing_message(
    page: Page,
    before_marker: str | None,
) -> None:
    """
    确认消息已经进入聊天记录。
    """

    await _await_send_terminal_state(
        page,
        timeout_ms=10000,
    )

    success = await _wait_for_new_outgoing_message(
        page,
        before_marker,
        timeout_ms=10000,
    )

    if success:
        return

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
    """

    if not content:
        raise PageOperationError(
            "发送文字为空"
        )

    print()
    print("=" * 70)
    print(
        f"准备发送文字，长度: {len(content)}"
    )
    print(
        f"文字内容: {content!r}"
    )
    print("=" * 70)

    editor = await chat.message_input()

    if editor is None:
        raise PageOperationError(
            "没有找到聊天输入框"
        )

    editor = await _find_real_editable(
        editor
    )

    success = await _input_text_with_fallback(
        editor,
        content,
    )

    if not success:
        print()
        print(
            "文字输入失败，输出输入框详细信息。"
        )

        await _print_editor_debug(
            editor
        )

        raise PageOperationError(
            "文字未能写入聊天输入框"
        )

    ready = await _wait_editor_text(
        editor,
        content,
        timeout_ms=3000,
    )

    if not ready:
        print()
        print(
            "输入框最终确认失败。"
        )

        await _print_editor_debug(
            editor
        )

        raise PageOperationError(
            "文字输入框内容确认失败"
        )

    print(
        "文字已写入输入框: "
        f"{await _get_editor_text(editor)!r}"
    )

    page = editor.page

    before = await _mark_latest_outgoing_message(
        page
    )

    await page.wait_for_timeout(300)

    await _trigger_send(page)

    print("已触发发送动作。")

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
    打开贴纸面板。
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
    page: Page,
    chat: DouyinChat,
    message: Message,
    task_sticker=None,
) -> None:
    """
    根据 Message 类型发送消息。

    注意：
    main.py 当前调用方式是：

        send_message(
            page,
            chat,
            message,
            task_sticker,
        )

    所以这里必须保持 4 个参数。
    """

    message_type = getattr(
        message,
        "type",
        None,
    )

    # ============================================================
    # 文字消息
    # ============================================================

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

    # ============================================================
    # 图片消息
    # ============================================================

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

    # ============================================================
    # 贴纸消息
    # ============================================================

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

        # 如果 Message 自己没有 sticker，
        # 使用 main.py 传入的 task_sticker。
        if sticker is None:
            sticker = task_sticker

        if sticker is None:
            raise PageOperationError(
                "贴纸消息没有 sticker"
            )

        await send_sticker(
            chat,
            sticker,
        )

        return

    # ============================================================
    # 随机消息
    # ============================================================

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

        if isinstance(
            selected,
            str,
        ):
            await send_text(
                chat,
                selected,
            )

            return

        await send_message(
            page,
            chat,
            selected,
            task_sticker,
        )

        return

    # ============================================================
    # 未知消息类型
    # ============================================================

    raise PageOperationError(
        f"不支持的消息类型: "
        f"{message_type!r}"
    )
