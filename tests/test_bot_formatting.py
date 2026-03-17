from __future__ import annotations

import unittest

from pocket_operator.bot import _format_actor_message


class BotFormattingTests(unittest.TestCase):
    def test_user_messages_use_bold_heading(self) -> None:
        payload, parse_mode = _format_actor_message("user", "continue the work")
        self.assertEqual(parse_mode, "HTML")
        self.assertEqual(payload, "<b>You</b>\ncontinue the work")

    def test_codex_messages_use_preformatted_block(self) -> None:
        payload, parse_mode = _format_actor_message("codex", "line 1\nline 2", label="Codex")
        self.assertEqual(parse_mode, "HTML")
        self.assertEqual(payload, "<b>Codex</b>\n<pre>line 1\nline 2</pre>")

    def test_system_messages_stay_plain(self) -> None:
        payload, parse_mode = _format_actor_message("system", "session ended")
        self.assertIsNone(parse_mode)
        self.assertEqual(payload, "session ended")


if __name__ == "__main__":
    unittest.main()
