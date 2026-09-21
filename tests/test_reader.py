from __future__ import annotations

import unittest
from unittest.mock import patch

import talk_private_message_reader as reader


class ScanTests(unittest.TestCase):
    def test_fingerprint_keeps_repeated_messages_from_same_minute(self) -> None:
        common = {
            "captured_at": "2026-09-21T12:00:00+03:00",
            "conversation": reader.PUBLIC_CONVERSATION,
            "sender": "Ученик А",
            "displayed_time": "12:00",
            "text": "42",
        }

        first = reader.TalkMessage(**common, occurrence=0)
        second = reader.TalkMessage(**common, occurrence=1)

        self.assertNotEqual(first.fingerprint, second.fingerprint)

    def test_scan_does_not_switch_tabs_without_unread_messages(self) -> None:
        with (
            patch.object(reader, "_conversation_header", return_value=None),
            patch.object(reader, "ensure_private_list"),
            patch.object(reader, "_public_tab", return_value=object()),
            patch.object(reader, "_tab_unread", return_value=0),
            patch.object(reader, "ensure_public_chat") as open_public,
            patch.object(reader, "read_open_conversation") as read_messages,
            patch.object(reader, "list_conversations", return_value=[]),
        ):
            total, emitted = reader.scan(
                object(),
                include_read=False,
                organizer=None,
                seen=set(),
                log_path=None,
            )

        self.assertEqual((total, emitted), (0, 0))
        open_public.assert_not_called()
        read_messages.assert_not_called()

    def test_scan_merges_public_chat_and_unread_private_conversations(self) -> None:
        public_message = reader.TalkMessage(
            captured_at="2026-09-21T12:00:00+03:00",
            conversation=reader.PUBLIC_CONVERSATION,
            sender="Ученик А",
            displayed_time="12:00",
            text="Ответ в общий чат",
        )
        private_message = reader.TalkMessage(
            captured_at="2026-09-21T12:00:01+03:00",
            conversation="Ученик Б",
            sender="Ученик Б",
            displayed_time="12:00",
            text="Ответ лично",
        )
        conversations = [
            reader.Conversation("Ученик Б", "Ученик Б 1", 1),
            reader.Conversation("Ученик В", "Ученик В", None),
        ]
        emitted_messages: list[reader.TalkMessage] = []

        def read_messages(_window: object, conversation: reader.Conversation):
            if conversation.name == reader.PUBLIC_CONVERSATION:
                return [public_message]
            if conversation.name == "Ученик Б":
                return [private_message]
            return []

        with (
            patch.object(reader, "_conversation_header", return_value=None),
            patch.object(reader, "ensure_public_chat"),
            patch.object(reader, "read_open_conversation", side_effect=read_messages),
            patch.object(reader, "ensure_private_list"),
            patch.object(reader, "_public_tab", return_value=object()),
            patch.object(reader, "_tab_unread", return_value=1),
            patch.object(reader, "list_conversations", return_value=conversations),
            patch.object(reader, "_open_conversation"),
            patch.object(reader, "_return_to_private_list"),
            patch.object(reader, "_wait_for", side_effect=lambda predicate, _label: predicate()),
        ):
            total, emitted = reader.scan(
                object(),
                include_read=False,
                organizer=None,
                seen=set(),
                log_path=None,
                on_message=emitted_messages.append,
            )

        self.assertEqual(total, 2)
        self.assertEqual(emitted, 2)
        self.assertEqual(
            [message.conversation for message in emitted_messages],
            [reader.PUBLIC_CONVERSATION, "Ученик Б"],
        )

    def test_scan_reads_separate_public_window_without_unread_counter(self) -> None:
        private_window = object()
        public_window = object()
        public_message = reader.TalkMessage(
            captured_at="2026-09-21T12:00:00+03:00",
            conversation=reader.PUBLIC_CONVERSATION,
            sender="Ученик А",
            displayed_time="12:00",
            text="Сообщение без счётчика",
        )
        public_tab = object()
        emitted_messages: list[reader.TalkMessage] = []

        with (
            patch.object(reader, "_conversation_header", return_value=None),
            patch.object(reader, "ensure_private_list"),
            patch.object(reader, "_same_window", return_value=False),
            patch.object(reader, "_public_tab", return_value=public_tab),
            patch.object(reader, "_class_name", return_value="tab_active"),
            patch.object(
                reader, "read_open_conversation", return_value=[public_message]
            ) as read_messages,
            patch.object(reader, "list_conversations", return_value=[]),
            patch.object(reader, "ensure_public_chat") as open_public,
        ):
            total, emitted = reader.scan(
                private_window,
                public_window=public_window,
                include_read=False,
                organizer=None,
                seen=set(),
                log_path=None,
                on_message=emitted_messages.append,
            )

        self.assertEqual((total, emitted), (0, 1))
        self.assertEqual(emitted_messages, [public_message])
        read_messages.assert_called_once()
        self.assertIs(read_messages.call_args.args[0], public_window)
        open_public.assert_not_called()


if __name__ == "__main__":
    unittest.main()
