"""Small desktop inbox for public and private Kontur.Talk messages."""

from __future__ import annotations

import argparse
import ctypes
import json
import queue
import sys
import threading
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from tkinter import BooleanVar, Canvas, Checkbutton, Frame, Label, Scrollbar, Text, Tk
from typing import Any

import pythoncom

from talk_private_message_reader import (
    PUBLIC_CONVERSATION,
    TalkMessage,
    find_chat_windows,
    prepare_chat_windows,
    scan,
)

BACKGROUND = "#0b1120"
PANEL = "#111827"
FEED = "#0f172a"
TEXT = "#f8fafc"
MUTED = "#94a3b8"
ACCENT = "#38bdf8"
GREEN = "#22c55e"
YELLOW = "#f59e0b"
RED = "#ef4444"
SEPARATOR = "#243044"
SENDER_COLORS = ("#38bdf8", "#a78bfa", "#34d399", "#fb7185", "#fbbf24")

GWL_EXSTYLE = -20
WS_EX_TRANSPARENT = 0x00000020
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
WM_HOTKEY = 0x0312
PM_REMOVE = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000
VK_F10 = 0x79
VK_CONTROL = 0x11
HOTKEY_ID = 0x4B54


@dataclass(frozen=True)
class StatusUpdate:
    state: str
    text: str


class TalkInbox:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.root = Tk()
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.stop_event = threading.Event()
        self.message_count = 0
        self.senders: set[str] = set()
        self.seen: set[tuple[str, str, str, str, int]] = set()
        self.empty_hint_visible = True
        self.topmost = BooleanVar(value=args.topmost)
        self.click_through = BooleanVar(value=args.click_through)
        self.click_through_applied: bool | None = None
        self.hotkey_registered = False

        self._configure_window()
        self._build_ui()
        self._load_history(args.log)
        self._register_hotkey()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(100, self._drain_events)
        self.root.after(950, self._apply_click_through)

        self.worker = threading.Thread(target=self._watch, name="talk-reader", daemon=True)
        self.worker.start()

    def _configure_window(self) -> None:
        self.root.title("Толк · сообщения встречи")
        self.root.configure(background=BACKGROUND)
        self.root.minsize(440, 520)
        width, height = 540, 760
        screen_width = self.root.winfo_screenwidth()
        x = max(20, screen_width - width - 32)
        self.root.geometry(f"{width}x{height}+{x}+64")
        self.root.attributes("-topmost", self.topmost.get())
        self.root.after_idle(self._show_once_on_launch)

    def _show_once_on_launch(self) -> None:
        """Make a newly started window visible, then return it to normal behavior."""
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(700, self._release_launch_topmost)

    def _release_launch_topmost(self) -> None:
        self.root.attributes("-topmost", self.topmost.get())

    def _build_ui(self) -> None:
        header = Frame(self.root, background=PANEL, padx=22, pady=18)
        header.pack(fill="x")

        title_row = Frame(header, background=PANEL)
        title_row.pack(fill="x")
        Label(
            title_row,
            text="Сообщения встречи",
            background=PANEL,
            foreground=TEXT,
            font=("Segoe UI Semibold", 17),
        ).pack(side="left")

        status_row = Frame(header, background=PANEL)
        status_row.pack(fill="x", pady=(8, 0))
        self.status_dot = Canvas(
            status_row,
            width=12,
            height=12,
            background=PANEL,
            highlightthickness=0,
        )
        self.status_dot.pack(side="left", padx=(0, 7))
        self.status_oval = self.status_dot.create_oval(2, 2, 10, 10, fill=YELLOW, outline="")

        self.status_label = Label(
            status_row,
            text="Подключаюсь…",
            background=PANEL,
            foreground=MUTED,
            font=("Segoe UI", 10),
        )
        self.status_label.pack(side="left")

        self.counter_label = Label(
            status_row,
            text="Сообщений пока нет",
            background=PANEL,
            foreground=MUTED,
            font=("Segoe UI", 10),
        )
        self.counter_label.pack(side="right")

        feed_frame = Frame(self.root, background=BACKGROUND, padx=14, pady=14)
        feed_frame.pack(fill="both", expand=True)

        self.feed = Text(
            feed_frame,
            background=FEED,
            foreground=TEXT,
            insertbackground=TEXT,
            relief="flat",
            borderwidth=0,
            padx=18,
            pady=18,
            wrap="word",
            cursor="arrow",
            selectbackground="#334155",
            font=("Segoe UI", 13),
            spacing1=2,
            spacing3=3,
        )
        scrollbar = Scrollbar(
            feed_frame,
            command=self.feed.yview,
            background=PANEL,
            activebackground=ACCENT,
            troughcolor=BACKGROUND,
            relief="flat",
            borderwidth=0,
        )
        scrollbar.pack(side="right", fill="y")
        self.feed.configure(yscrollcommand=scrollbar.set)
        self.feed.pack(side="left", fill="both", expand=True)
        self.feed.tag_configure(
            "empty",
            foreground=MUTED,
            justify="center",
            spacing1=170,
            font=("Segoe UI", 12),
        )
        self.feed.tag_configure("time", foreground=MUTED, font=("Segoe UI", 9))
        self.feed.tag_configure(
            "channel_public",
            foreground="#fbbf24",
            font=("Segoe UI Semibold", 9),
        )
        self.feed.tag_configure(
            "channel_private",
            foreground=ACCENT,
            font=("Segoe UI Semibold", 9),
        )
        self.feed.tag_configure("body", foreground=TEXT, font=("Segoe UI", 14), spacing3=10)
        self.feed.tag_configure("separator", foreground=SEPARATOR, font=("Consolas", 8))
        self.feed.insert("end", "Жду сообщения из общего и личных чатов…", "empty")
        self.feed.configure(state="disabled")

        footer = Frame(self.root, background=PANEL, padx=18, pady=12)
        footer.pack(fill="x")
        Checkbutton(
            footer,
            text="Клики сквозь окно",
            variable=self.click_through,
            command=self._apply_click_through,
            background=PANEL,
            foreground=MUTED,
            activebackground=PANEL,
            activeforeground=TEXT,
            selectcolor=PANEL,
            font=("Segoe UI", 10),
            borderwidth=0,
            highlightthickness=0,
        ).pack(side="left")
        Label(
            footer,
            text="Удерживай Ctrl · двигай окно",
            background=PANEL,
            foreground=MUTED,
            font=("Segoe UI", 9),
        ).pack(side="right")

    def _window_handles(self) -> list[int]:
        user32 = ctypes.windll.user32
        handles: list[int] = []
        handle = self.root.winfo_id()
        while handle and handle not in handles:
            handles.append(handle)
            handle = user32.GetParent(handle)
        return handles

    def _apply_click_through(self) -> None:
        user32 = ctypes.windll.user32
        ctrl_held = bool(user32.GetAsyncKeyState(VK_CONTROL) & 0x8000)
        enabled = self.click_through.get() and not ctrl_held
        if enabled == self.click_through_applied:
            return
        for handle in self._window_handles():
            style = user32.GetWindowLongW(handle, GWL_EXSTYLE)
            if enabled:
                style |= WS_EX_TRANSPARENT | WS_EX_LAYERED | WS_EX_NOACTIVATE
            else:
                style &= ~(WS_EX_TRANSPARENT | WS_EX_NOACTIVATE)
            user32.SetWindowLongW(handle, GWL_EXSTYLE, style)
            user32.SetWindowPos(
                handle,
                0,
                0,
                0,
                0,
                SWP_NOMOVE
                | SWP_NOSIZE
                | SWP_NOZORDER
                | SWP_NOACTIVATE
                | SWP_FRAMECHANGED,
            )
        self.click_through_applied = enabled
        self.root.attributes("-topmost", self.topmost.get())

    def _register_hotkey(self) -> None:
        modifiers = MOD_CONTROL | MOD_SHIFT | MOD_NOREPEAT
        self.hotkey_registered = bool(
            ctypes.windll.user32.RegisterHotKey(None, HOTKEY_ID, modifiers, VK_F10)
        )
        self.root.after(50, self._poll_input_state)

    def _poll_input_state(self) -> None:
        message = wintypes.MSG()
        user32 = ctypes.windll.user32
        while user32.PeekMessageW(
            ctypes.byref(message), None, WM_HOTKEY, WM_HOTKEY, PM_REMOVE
        ):
            if message.wParam == HOTKEY_ID:
                self.click_through.set(not self.click_through.get())
        self._apply_click_through()
        if not self.stop_event.is_set():
            self.root.after(50, self._poll_input_state)

    def _watch(self) -> None:
        pythoncom.CoInitialize()
        private_window: Any | None = None
        public_window: Any | None = None
        first_pass = True
        try:
            while not self.stop_event.is_set():
                try:
                    if private_window is None or public_window is None:
                        self.events.put(("status", StatusUpdate("connecting", "Ищу встречу…")))
                        private_window, public_window = find_chat_windows()
                        prepare_chat_windows(private_window, public_window)
                    total, emitted = scan(
                        private_window,
                        public_window=public_window,
                        include_read=self.args.include_history and first_pass,
                        organizer=self.args.organizer,
                        seen=self.seen,
                        log_path=self.args.log,
                        on_message=lambda message: self.events.put(("message", message)),
                        discover=True,
                    )
                    first_pass = False
                    suffix = f" · общий + {total} личн." if total else " · общий"
                    self.events.put(
                        ("status", StatusUpdate("connected", f"Подключено{suffix}"))
                    )
                    if emitted:
                        self.events.put(("pulse", None))
                    self.stop_event.wait(self.args.interval)
                except Exception as exc:
                    private_window = None
                    public_window = None
                    self.events.put(("status", StatusUpdate("error", str(exc))))
                    self.stop_event.wait(2.0)
        finally:
            pythoncom.CoUninitialize()

    def _drain_events(self) -> None:
        try:
            while True:
                event, payload = self.events.get_nowait()
                if event == "message":
                    self._add_message(payload)
                elif event == "status":
                    self._set_status(payload)
                elif event == "pulse":
                    self._pulse()
        except queue.Empty:
            pass
        if not self.stop_event.is_set():
            self.root.after(100, self._drain_events)

    def _sender_tag(self, sender: str) -> str:
        tag = f"sender_{abs(hash(sender)) % len(SENDER_COLORS)}"
        if tag not in self.feed.tag_names():
            color = SENDER_COLORS[abs(hash(sender)) % len(SENDER_COLORS)]
            self.feed.tag_configure(tag, foreground=color, font=("Segoe UI Semibold", 12))
        return tag

    def _load_history(self, log_path: Path | None) -> None:
        if log_path is None or not log_path.exists():
            return
        try:
            lines = log_path.read_text(encoding="utf-8").splitlines()[-500:]
        except OSError:
            return
        for line in lines:
            try:
                message = TalkMessage(**json.loads(line))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if message.sender in {"Встреча", "Сегодня"}:
                continue
            if message.fingerprint in self.seen:
                continue
            if self.args.organizer and message.sender.casefold() == self.args.organizer.casefold():
                continue
            self.seen.add(message.fingerprint)
            self._add_message(message)

    def _add_message(self, message: TalkMessage) -> None:
        self.feed.configure(state="normal")
        if self.empty_hint_visible:
            self.feed.delete("1.0", "end")
            self.empty_hint_visible = False

        is_public = message.conversation == PUBLIC_CONVERSATION
        channel_label = "ОБЩИЙ" if is_public else "ЛИЧНО"
        channel_tag = "channel_public" if is_public else "channel_private"
        self.feed.insert("end", f"{channel_label}  ", channel_tag)
        self.feed.insert("end", message.sender, self._sender_tag(message.sender))
        self.feed.insert("end", f"    {message.displayed_time}\n", "time")
        self.feed.insert("end", f"{message.text}\n", "body")
        self.feed.insert("end", "─" * 54 + "\n", "separator")
        self.feed.configure(state="disabled")
        self.feed.see("end")

        self.message_count += 1
        self.senders.add(message.sender)
        noun = "ученик" if len(self.senders) == 1 else "ученика"
        self.counter_label.configure(
            text=f"{self.message_count} сообщений · {len(self.senders)} {noun}"
        )

    def _set_status(self, update: StatusUpdate) -> None:
        colors = {"connecting": YELLOW, "connected": GREEN, "error": RED}
        self.status_dot.itemconfigure(self.status_oval, fill=colors.get(update.state, MUTED))
        text = update.text
        if len(text) > 45:
            text = text[:42] + "…"
        self.status_label.configure(text=text)

    def _pulse(self) -> None:
        self.status_dot.itemconfigure(self.status_oval, fill=ACCENT)
        self.root.after(700, lambda: self.status_dot.itemconfigure(self.status_oval, fill=GREEN))

    def close(self) -> None:
        self.stop_event.set()
        if self.hotkey_registered:
            ctypes.windll.user32.UnregisterHotKey(None, HOTKEY_ID)
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Окно личных сообщений Контур.Толка.")
    parser.add_argument(
        "--organizer",
        default=None,
        help="Имя преподавателя: его исходящие сообщения скрываются.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=0.3,
        help="Пауза между проверками Толка, секунд.",
    )
    parser.add_argument(
        "--log",
        type=Path,
        default=Path(".tmp/talk_messages.jsonl"),
        help="Локальный JSONL-лог сообщений.",
    )
    parser.add_argument(
        "--topmost",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Закрепить окно поверх остальных (по умолчанию включено).",
    )
    parser.add_argument(
        "--click-through",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Пропускать клики сквозь окно (по умолчанию включено).",
    )
    parser.add_argument(
        "--include-history",
        action="store_true",
        help="При старте открыть все личные диалоги и прочитать историю.",
    )
    args = parser.parse_args()
    if args.interval < 0.2:
        parser.error("--interval должен быть не меньше 0.2 секунды")
    return args


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
    TalkInbox(parse_args()).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
