"""Read private Kontur.Talk meeting messages through Windows UI Automation.

This is a local prototype for the desktop Kontur.Talk application. It does not
use Talk credentials, intercept network traffic, or send messages. Opening a
conversation does mark its messages as read in Talk.

Examples:
    python talk_private_message_reader.py --once --include-read
    python talk_private_message_reader.py --watch
    python talk_private_message_reader.py --watch --organizer "Имя преподавателя"

Dependency:
    pywinauto==0.6.9
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from pywinauto import Desktop
except ImportError as exc:  # pragma: no cover - depends on the local Windows machine
    raise SystemExit(
        "Не найден pywinauto. Установите его командой:\n"
        "  uv pip install --python .venv\\Scripts\\python.exe pywinauto==0.6.9"
    ) from exc


TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
UNREAD_RE = re.compile(r"\s+(\d+)$")
ROLE_SUFFIX_RE = re.compile(
    r"\s+(?:гость|организатор|администратор|модератор)(?:\s+\d+)?$",
    re.IGNORECASE,
)
CHAT_WIDTH = 620


@dataclass(frozen=True)
class TalkMessage:
    captured_at: str
    conversation: str
    sender: str
    displayed_time: str
    text: str

    @property
    def fingerprint(self) -> tuple[str, str, str, str]:
        return (self.conversation, self.sender, self.displayed_time, self.text)


@dataclass(frozen=True)
class Conversation:
    name: str
    label: str
    unread: int | None


def _configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def _wait_for(predicate: Callable[[], Any], description: str, timeout: float = 5.0) -> Any:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            result = predicate()
            if result:
                return result
        except Exception as exc:  # UI trees can be briefly stale after navigation
            last_error = exc
        time.sleep(0.15)
    detail = f": {last_error}" if last_error else ""
    raise RuntimeError(f"Не дождался элемента: {description}{detail}")


def _window_buttons(window: Any) -> list[Any]:
    return window.descendants(control_type="Button")


def _class_name(element: Any) -> str:
    return element.element_info.class_name or ""


def _name(element: Any) -> str:
    return element.window_text().strip()


def _content_bounds(window: Any) -> Any:
    """Return the visible web-content bounds, even when Electron reports a 0x0 window."""
    for item in window.descendants(control_type="Document"):
        rect = item.rectangle()
        if (
            item.element_info.automation_id == "RootWebArea"
            and rect.right > rect.left
            and rect.bottom > rect.top
        ):
            return rect
    return window.rectangle()


def _is_talk_meeting(window: Any) -> bool:
    try:
        has_meeting_controls = any(
            item.element_info.automation_id == "toggle-tools-btn"
            for item in window.descendants()
        )
        has_detached_controls = any(
            _name(item) == "Конференция" and "tab" in _class_name(item)
            for item in window.descendants(control_type="Button")
        )
        return has_meeting_controls or has_detached_controls
    except Exception:
        return False


def find_meeting_window() -> Any:
    candidates = [w for w in Desktop(backend="uia").windows() if _is_talk_meeting(w)]
    if not candidates:
        raise RuntimeError("Окно встречи не найдено. Откройте встречу в приложении Толк.")

    def score(window: Any) -> tuple[int, int]:
        buttons = window.descendants(control_type="Button")
        detached = any(
            _name(item) == "Конференция" and "tab" in _class_name(item)
            for item in buttons
        )
        chat_selected = any(
            _name(item) == "Чат" and "_selected" in _class_name(item)
            for item in buttons
        )
        private_visible = any(_name(item).upper().startswith("ЛИЧНЫЕ") for item in buttons)
        rect = _content_bounds(window)
        area = max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
        return (100 * detached + 10 * chat_selected + 5 * private_visible, area)

    # Talk can detach its controls/chat into a real visible `ktalk` window while
    # the original `Встреча` top-level becomes 0x0. Prefer the detached window:
    # it is also where unread conversation rows continue to be exposed to UIA.
    window = max(candidates, key=score)
    rect = window.rectangle()
    if rect.left < -10000 or rect.top < -10000:
        window.restore()
        time.sleep(0.5)
    return window


def _button(window: Any, predicate: Callable[[Any], bool]) -> Any | None:
    for item in _window_buttons(window):
        if predicate(item):
            return item
    return None


def _private_tab(window: Any) -> Any | None:
    return _button(window, lambda item: _name(item).upper().startswith("ЛИЧНЫЕ"))


def _conversation_header(window: Any) -> Any | None:
    left = _content_bounds(window).left
    return _button(
        window,
        lambda item: (
            _class_name(item) == "_white medium"
            and left <= item.rectangle().left < left + CHAT_WIDTH
            and bool(_name(item))
        ),
    )


def ensure_private_list(window: Any) -> None:
    """Open Chat -> Private and return from a conversation when necessary."""
    private_tab = _private_tab(window)
    if private_tab is None:
        header = _conversation_header(window)
        if header is not None:
            header.invoke()
            private_tab = _wait_for(lambda: _private_tab(window), "вкладка «Личные»")
        else:
            chat = _button(window, lambda item: _name(item) == "Чат")
            if chat is None:
                raise RuntimeError("В окне встречи не найдена кнопка «Чат».")
            chat.invoke()
            private_tab = _wait_for(lambda: _private_tab(window), "панель чата")

    if "_active" not in _class_name(private_tab):
        private_tab.invoke()
        _wait_for(
            lambda: _private_tab(window) is not None
            and "_active" in _class_name(_private_tab(window)),
            "активная вкладка «Личные»",
        )


def _conversation_name(label: str) -> str:
    cleaned = ROLE_SUFFIX_RE.sub("", label)
    cleaned = UNREAD_RE.sub("", cleaned)
    return cleaned.strip()


def list_conversations(window: Any) -> list[Conversation]:
    ensure_private_list(window)
    bounds = _content_bounds(window)
    result: list[Conversation] = []
    for item in _window_buttons(window):
        if _class_name(item) != "global-as-button":
            continue
        rect = item.rectangle()
        if not (
            bounds.left <= rect.left < bounds.left + CHAT_WIDTH
            and bounds.top + 100 < rect.top < bounds.bottom - 150
        ):
            continue
        label = _name(item)
        if not label:
            continue
        unread_match = UNREAD_RE.search(label)
        result.append(
            Conversation(
                name=_conversation_name(label),
                label=label,
                unread=int(unread_match.group(1)) if unread_match else None,
            )
        )
    return result


def _private_search(window: Any) -> Any | None:
    for item in window.descendants(control_type="Edit"):
        if "tl-input__input" in _class_name(item):
            return item
    return None


def _set_private_search(window: Any, value: str) -> None:
    search = _private_search(window)
    if search is None:
        raise RuntimeError("В списке личных чатов не найдено поле поиска.")
    # Chromium's UIA ValuePattern sometimes accepts SetValue without updating
    # the actual React input. Keyboard clearing updates both the DOM and UIA.
    search.set_focus()
    search.type_keys("^a{BACKSPACE}", set_foreground=False)
    if value:
        time.sleep(0.05)
        search.type_keys(value, with_spaces=True, set_foreground=False)
    time.sleep(0.15)


def _open_conversation_by_search(window: Any, name: str) -> None:
    ensure_private_list(window)
    _set_private_search(window, name)
    target = _wait_for(
        lambda: _button(
            window,
            lambda item: _class_name(item) == "global-as-button"
            and _conversation_name(_name(item)) == name,
        ),
        f"результат поиска личного чата с {name}",
    )
    target.invoke()
    _wait_for(lambda: _conversation_header(window), f"чат с {name}")


def _open_conversation(window: Any, conversation: Conversation) -> None:
    ensure_private_list(window)
    target = _button(
        window,
        lambda item: _class_name(item) == "global-as-button"
        and _conversation_name(_name(item)) == conversation.name,
    )
    if target is None:
        raise RuntimeError(f"Личный чат «{conversation.name}» исчез из списка.")
    target.invoke()
    _wait_for(lambda: _conversation_header(window), f"чат с {conversation.name}")


def _message_groups(window: Any) -> list[Any]:
    bounds = _content_bounds(window)
    groups: list[Any] = []
    for item in window.descendants(control_type="Group"):
        class_name = _class_name(item)
        rect = item.rectangle()
        if (
            class_name.split(" ", 1)[0] == "text"
            and bounds.left <= rect.left < bounds.left + CHAT_WIDTH
        ):
            groups.append(item)
    return sorted(groups, key=lambda item: (item.rectangle().top, item.rectangle().left))


def _group_text(group: Any) -> str:
    parts = [_name(item) for item in group.descendants(control_type="Text") if _name(item)]
    if parts:
        return "\n".join(parts).strip()
    return _name(group)


def read_open_conversation(window: Any, conversation: Conversation) -> list[TalkMessage]:
    bounds = _content_bounds(window)
    text_items = [
        item
        for item in window.descendants(control_type="Text")
        if bounds.left <= item.rectangle().left < bounds.left + CHAT_WIDTH
    ]
    times = [item for item in text_items if TIME_RE.fullmatch(_name(item))]
    times.sort(key=lambda item: (item.rectangle().top, item.rectangle().left))

    now = datetime.now().astimezone().isoformat(timespec="seconds")
    messages: list[TalkMessage] = []
    for group in _message_groups(window):
        body = _group_text(group)
        if not body:
            continue
        body_top = group.rectangle().top
        preceding_times = [item for item in times if item.rectangle().top < body_top]
        if not preceding_times:
            continue
        time_item = max(preceding_times, key=lambda item: item.rectangle().top)
        time_top = time_item.rectangle().top
        sender_candidates = [
            item
            for item in text_items
            if abs(item.rectangle().top - time_top) <= 3
            and item is not time_item
            and _name(item)
            and not TIME_RE.fullmatch(_name(item))
        ]
        if not sender_candidates:
            continue
        sender = min(
            sender_candidates,
            key=lambda item: abs(item.rectangle().left - group.rectangle().left),
        )
        messages.append(
            TalkMessage(
                captured_at=now,
                conversation=conversation.name,
                sender=_name(sender),
                displayed_time=_name(time_item),
                text=body,
            )
        )
    return messages


def _return_to_private_list(window: Any) -> None:
    header = _conversation_header(window)
    if header is not None:
        header.invoke()
    _wait_for(lambda: _private_tab(window), "возврат к списку личных чатов")


def _emit(message: TalkMessage, log_path: Path | None) -> None:
    print(
        f"[{message.displayed_time}] {message.sender}: {message.text}",
        flush=True,
    )
    if log_path is None:
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(asdict(message), ensure_ascii=False) + "\n")


def scan(
    window: Any,
    *,
    include_read: bool,
    organizer: str | None,
    seen: set[tuple[str, str, str, str]],
    log_path: Path | None,
    on_message: Callable[[TalkMessage], None] | None = None,
    discover: bool = True,
) -> tuple[int, int]:
    emitted = 0

    def collect(messages: list[TalkMessage]) -> None:
        nonlocal emitted
        for message in messages:
            fingerprint = message.fingerprint
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            if organizer and message.sender.casefold() == organizer.casefold():
                continue
            if on_message is None:
                _emit(message, log_path)
            else:
                on_message(message)
                if log_path is not None:
                    log_path.parent.mkdir(parents=True, exist_ok=True)
                    with log_path.open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(asdict(message), ensure_ascii=False) + "\n")
            emitted += 1

    open_header = _conversation_header(window)
    open_conversation: Conversation | None = None
    if open_header is not None:
        name = _name(open_header)
        open_conversation = Conversation(name=name, label=name, unread=None)
        collect(read_open_conversation(window, open_conversation))
        if not discover:
            return 1, emitted
        _return_to_private_list(window)

    ensure_private_list(window)
    # A previous lookup can leave text in Talk's search field. In that state an
    # unread badge is visible, but the conversation row is filtered out and a
    # watcher sees an empty inbox forever.
    search = _private_search(window)
    if search is not None and _name(search).replace("\ufffc", "").strip():
        _set_private_search(window, "")
    conversations = list_conversations(window)
    # The detached ktalk chat keeps unread counts on conversation rows. Read all
    # conversations only on startup; subsequent passes open just rows with new
    # messages so the cost grows with activity, not with the class size.
    selected = conversations if include_read else [item for item in conversations if item.unread]
    for index, conversation in enumerate(selected):
        _open_conversation(window, conversation)
        messages = _wait_for(
            lambda current=conversation: read_open_conversation(window, current),
            f"сообщения в чате с {conversation.name}",
        )
        collect(messages)
        if index < len(selected) - 1:
            _return_to_private_list(window)
    if not selected and open_conversation is not None:
        _open_conversation_by_search(window, open_conversation.name)
    return len(conversations), emitted


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Собирает личные сообщения встречи Контур.Толка в единый поток."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Сделать один проход (по умолчанию).")
    mode.add_argument(
        "--watch",
        action="store_true",
        help="Следить за новыми сообщениями постоянно.",
    )
    parser.add_argument(
        "--include-read",
        action="store_true",
        help="Также открыть чаты без непрочитанных сообщений.",
    )
    parser.add_argument(
        "--organizer",
        help="Не показывать исходящие сообщения этого отправителя.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Пауза между проверками в режиме --watch, секунд (по умолчанию 1).",
    )
    parser.add_argument(
        "--log",
        type=Path,
        default=Path(".tmp/talk_private_messages.jsonl"),
        help="JSONL-файл для лога; укажите пустую строку, чтобы отключить.",
    )
    return parser.parse_args()


def main() -> int:
    _configure_console()
    args = parse_args()
    log_path = args.log if str(args.log) else None
    if args.interval < 0.2:
        raise SystemExit("--interval должен быть не меньше 0.2 секунды.")

    print("Ищу окно встречи Контур.Толка…", flush=True)
    window = find_meeting_window()
    print(
        "Подключено. Внимание: открытые скриптом сообщения станут прочитанными в Толке.",
        flush=True,
    )

    seen: set[tuple[str, str, str, str]] = set()
    first_pass = True
    try:
        while True:
            total, emitted = scan(
                window,
                include_read=args.include_read and first_pass,
                organizer=args.organizer,
                seen=seen,
                log_path=log_path,
            )
            if first_pass:
                print(f"Личных чатов: {total}; новых строк выведено: {emitted}.", flush=True)
            first_pass = False
            if not args.watch:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nОстановлено.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
