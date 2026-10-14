"""harness 压缩不得吞掉用户真实输入（首轮 'I'm ready to help' 回归）。"""
import unittest

from desensitize import (
    _compact_harness_message,
    _extract_user_payload,
    _prune_runtime_fragments,
    desensitize_messages,
)


def _apply(content, compact=True):
    return desensitize_messages(
        [{"role": "user", "content": content}],
        roles=("system",),
        desensitize_harness_user=True,
        compact_harness=compact,
    )[0]["content"]


class TestExtractUserPayload(unittest.TestCase):
    def test_strips_system_reminder_keeps_question(self):
        text = "<system-reminder>harness</system-reminder>\n\nWhat is 1+1?"
        self.assertEqual(_extract_user_payload(text), "What is 1+1?")

    def test_strips_environment_context(self):
        text = "<environment_context>\n  cwd: /tmp\n</environment_context>\n\nfix the bug"
        self.assertEqual(_extract_user_payload(text), "fix the bug")

    def test_pure_harness_is_empty(self):
        self.assertEqual(_extract_user_payload("<system-reminder>only</system-reminder>"), "")

    def test_multi_reminders(self):
        text = (
            "<system-reminder>a</system-reminder>\n"
            "<system-reminder>b</system-reminder>\n\n"
            "real question"
        )
        self.assertEqual(_extract_user_payload(text), "real question")

    def test_unclosed_reminder_tail(self):
        text = "keep me\n<system-reminder>truncated"
        self.assertEqual(_extract_user_payload(text), "keep me")


class TestNoWipeUserInput(unittest.TestCase):
    def test_compact_preserves_question(self):
        sample = "<system-reminder>ctx</system-reminder>\n\nWhat is 1+1?"
        self.assertEqual(_compact_harness_message("user", sample), "What is 1+1?")

    def test_prune_preserves_question(self):
        sample = "<system-reminder>ctx</system-reminder>\n\nWhat is 1+1?"
        self.assertEqual(_prune_runtime_fragments("user", sample), "What is 1+1?")

    def test_compact_desensitize_preserves_question(self):
        sample = "<system-reminder>ctx</system-reminder>\n\nWhat is 1+1?"
        self.assertEqual(_apply(sample, compact=True), "What is 1+1?")

    def test_prune_desensitize_preserves_question(self):
        sample = "<environment_context>x</environment_context>\n\nhelp me debug"
        out = _apply(sample, compact=False)
        self.assertIn("help me debug", out)
        self.assertNotIn("environment_context", out)

    def test_pure_harness_still_compacted(self):
        sample = "<system-reminder>only harness</system-reminder>"
        out = _apply(sample, compact=True)
        self.assertIn("Repository instructions", out)

    def test_plain_user_untouched(self):
        self.assertEqual(_apply("hello world", compact=True), "hello world")

    def test_chinese_question_preserved(self):
        sample = "<system-reminder>ctx</system-reminder>\n\n帮我看看这个空指针"
        self.assertIn("帮我看看这个空指针", _apply(sample, compact=True))


if __name__ == "__main__":
    unittest.main()
