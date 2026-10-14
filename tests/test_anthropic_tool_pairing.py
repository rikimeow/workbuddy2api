"""Anthropic→Chat tool 配对回归：顺序、空 id、孤儿清理、validate_tool_pairing。"""
import unittest

from anthropic_adapter import anthropic_request_to_chat, _convert_anthropic_message
from upstream_compat import cleanup_orphan_tool_calls, validate_tool_pairing


def _msg(role, content):
    return {"role": role, "content": content}


class TestUserToolResultOrder(unittest.TestCase):
    def test_text_after_tool_results(self):
        """user 里 text 与 tool_result 混排时，tool 必须在前。"""
        out = _convert_anthropic_message(_msg("user", [
            {"type": "text", "text": "before"},
            {"type": "tool_result", "tool_use_id": "call_a", "content": "ra"},
            {"type": "text", "text": "after"},
        ]))
        self.assertEqual([m["role"] for m in out], ["tool", "user"])
        self.assertEqual(out[0]["tool_call_id"], "call_a")
        self.assertEqual(out[0]["content"], "ra")
        self.assertEqual(out[1]["content"], "beforeafter")

    def test_multi_tool_results_keep_block_order(self):
        out = _convert_anthropic_message(_msg("user", [
            {"type": "tool_result", "tool_use_id": "call_1", "content": "r1"},
            {"type": "text", "text": "mid"},
            {"type": "tool_result", "tool_use_id": "call_2", "content": "r2"},
        ]))
        self.assertEqual([m["role"] for m in out], ["tool", "tool", "user"])
        self.assertEqual([m["tool_call_id"] for m in out[:2]], ["call_1", "call_2"])
        self.assertEqual(out[2]["content"], "mid")

    def test_pure_text_unchanged(self):
        out = _convert_anthropic_message(_msg("user", [{"type": "text", "text": "hi"}]))
        self.assertEqual(out, [{"role": "user", "content": "hi"}])

    def test_pure_tool_result(self):
        out = _convert_anthropic_message(_msg("user", [
            {"type": "tool_result", "tool_use_id": "call_x", "content": "ok"},
        ]))
        self.assertEqual(out, [{"role": "tool", "tool_call_id": "call_x", "content": "ok"}])

    def test_image_after_tools(self):
        out = _convert_anthropic_message(_msg("user", [
            {"type": "tool_result", "tool_use_id": "call_i", "content": "r"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
            {"type": "text", "text": "see image"},
        ]))
        self.assertEqual([m["role"] for m in out], ["tool", "user"])
        self.assertIsInstance(out[1]["content"], list)


class TestToolUseIdFallback(unittest.TestCase):
    def test_null_id_gets_nonempty(self):
        out = _convert_anthropic_message(_msg("assistant", [
            {"type": "tool_use", "id": None, "name": "Bash", "input": {}},
        ]))
        self.assertTrue(out[0]["tool_calls"][0]["id"])

    def test_empty_id_gets_nonempty(self):
        out = _convert_anthropic_message(_msg("assistant", [
            {"type": "tool_use", "id": "", "name": "Bash", "input": {}},
        ]))
        self.assertTrue(out[0]["tool_calls"][0]["id"])

    def test_missing_id_gets_nonempty(self):
        out = _convert_anthropic_message(_msg("assistant", [
            {"type": "tool_use", "name": "Bash", "input": {}},
        ]))
        self.assertTrue(out[0]["tool_calls"][0]["id"])


class TestToolResultContent(unittest.TestCase):
    def test_empty_tool_use_id_dropped(self):
        out = _convert_anthropic_message(_msg("user", [
            {"type": "tool_result", "tool_use_id": "", "content": "x"},
            {"type": "tool_result", "tool_use_id": "call_ok", "content": "y"},
        ]))
        self.assertEqual(out, [{"role": "tool", "tool_call_id": "call_ok", "content": "y"}])

    def test_list_content_with_non_text(self):
        out = _convert_anthropic_message(_msg("user", [
            {"type": "tool_result", "tool_use_id": "c1", "content": [
                {"type": "text", "text": "hello"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA"}},
            ]},
        ]))
        self.assertIn("hello", out[0]["content"])
        self.assertTrue(out[0]["content"])  # 非 text 块不得静默变空

    def test_is_error_prefixed(self):
        out = _convert_anthropic_message(_msg("user", [
            {"type": "tool_result", "tool_use_id": "c1", "content": "boom", "is_error": True},
        ]))
        self.assertTrue(out[0]["content"].startswith("Error: "))
        self.assertEqual(out[0]["tool_call_id"], "c1")


class TestCleanupAndValidate(unittest.TestCase):
    def _history(self):
        return {"messages": [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "A", "arguments": "{}"}},
                {"id": "call_2", "type": "function", "function": {"name": "B", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "call_1", "content": "r1"},
            # call_2 无结果（半截历史）
            {"role": "user", "content": "next"},
        ]}

    def test_cleanup_removes_orphan_pair(self):
        obj = self._history()
        n = cleanup_orphan_tool_calls(obj)
        self.assertGreater(n, 0)
        roles = [m["role"] for m in obj["messages"]]
        self.assertNotIn("tool", roles)  # all-or-nothing：半截 tool_calls 整组删
        self.assertEqual(validate_tool_pairing(obj["messages"]), [])

    def test_cleanup_keeps_matched(self):
        obj = {"messages": [
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "A", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
            {"role": "user", "content": "next"},
        ]}
        n = cleanup_orphan_tool_calls(obj)
        self.assertEqual(n, 0)
        self.assertEqual(len(obj["messages"]), 3)

    def test_validate_detects_user_interrupt(self):
        problems = validate_tool_pairing([
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "A", "arguments": "{}"}},
            ]},
            {"role": "user", "content": "interrupted"},
            {"role": "tool", "tool_call_id": "call_1", "content": "late"},
        ])
        self.assertTrue(any("interrupts" in p for p in problems))

    def test_validate_ok_on_good_history(self):
        problems = validate_tool_pairing([
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "thinking", "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "A", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
            {"role": "user", "content": "next"},
        ])
        self.assertEqual(problems, [])


class TestEndToEnd(unittest.TestCase):
    def test_anthropic_subagent_history_pairs(self):
        """模拟 Claude Code 子 Agent：tool_result 与 system-reminder 混排。"""
        body = {
            "model": "claude-sonnet",
            "system": "sys",
            "messages": [
                {"role": "user", "content": "run agent"},
                {"role": "assistant", "content": [
                    {"type": "text", "text": "spawning"},
                    {"type": "tool_use", "id": "toolu_01", "name": "Agent", "input": {"prompt": "x"}},
                ]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_01", "content": "subagent done"},
                    {"type": "text", "text": "<system-reminder>cwd</system-reminder>"},
                ]},
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "toolu_02", "name": "Bash", "input": {"command": "ls"}},
                ]},
                {"role": "user", "content": [
                    {"type": "text", "text": "ok continue"},
                    {"type": "tool_result", "tool_use_id": "toolu_02", "content": "file.txt"},
                ]},
            ],
            "tools": [{"name": "Agent", "input_schema": {"type": "object"}}],
        }
        chat = anthropic_request_to_chat(body)
        n = cleanup_orphan_tool_calls(chat)
        self.assertEqual(n, 0)
        problems = validate_tool_pairing(chat["messages"])
        self.assertEqual(problems, [])
        # tool 消息必须紧贴在对应 assistant.tool_calls 之后
        roles = [m["role"] for m in chat["messages"]]
        self.assertEqual(roles.count("tool"), 2)
        for i, m in enumerate(chat["messages"]):
            if m.get("role") == "tool":
                self.assertEqual(chat["messages"][i - 1].get("role"), "assistant")


if __name__ == "__main__":
    unittest.main()
