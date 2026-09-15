#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_upstream_compat.py — 上游兼容层单元测试。

不依赖网络与登录态，纯逻辑验证。

运行：
    python -m pytest test_upstream_compat.py -v
  或
    python test_upstream_compat.py
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import upstream_compat as uc


# ---------------------------------------------------------------------------
# 模型目录解析
# ---------------------------------------------------------------------------

CATALOG_FIXTURE = {
    "code": 0,
    "data": {
        "models": [
            {"id": "auto", "name": "Auto", "maxInputTokens": 168000, "maxOutputTokens": 32000,
             "supportsImages": True, "supportsReasoning": True, "onlyReasoning": True,
             "reasoning": {"effort": "high", "summary": "auto"}, "tags": ["craft"]},
            {"id": "deepseek-v4.1-flash", "name": "DeepSeek V4.1 Flash",
             "maxInputTokens": 168000, "maxOutputTokens": 128000,
             "supportsImages": True, "supportsReasoning": True, "onlyReasoning": True,
             "reasoning": {"defaultEffort": "high", "supportedEfforts": ["high"]},
             "tags": ["craft"], "credits": 1.0},
            {"id": "glm-5.3", "name": "GLM-5.3", "maxInputTokens": 200000, "maxOutputTokens": 64000,
             "reasoning": {"defaultEffort": "high", "supportedEfforts": ["low", "high", "max"]}},
            {"id": "nes-embed", "name": "NES", "maxOutputTokens": 8192},
            {"id": "tiny", "name": "Tiny", "maxOutputTokens": 128},
            {"id": "hunyuan-image-v3.0", "name": "Hunyuan Image V3", "tags": ["text-to-image"]},
            {"id": "disabled-one", "name": "Disabled", "disabled": True, "maxOutputTokens": 8192},
            {"id": "off-cli", "name": "Off CLI", "maxOutputTokens": 8192},
        ],
        "agents": [
            {"name": "cli", "models": ["auto", "deepseek-v4.1-flash", "glm-5.3",
                                       "nes-embed", "tiny", "disabled-one"]},
            {"name": "general-purpose", "models": ["off-cli"]},
        ],
    },
}


def test_catalog_uses_cli_agent_only():
    cat = uc.normalize_catalog(CATALOG_FIXTURE)
    ids = [m["id"] for m in cat["chat"]]
    assert "off-cli" not in ids, "非 cli agent 的模型不应进入对话列表"
    assert "auto" in ids and "glm-5.3" in ids


def test_catalog_filters_non_chat_and_disabled():
    cat = uc.normalize_catalog(CATALOG_FIXTURE)
    ids = [m["id"] for m in cat["chat"]]
    assert "nes-embed" not in ids, "nes- 前缀应过滤"
    assert "tiny" not in ids, "maxOutputTokens<=256 应过滤"
    assert "disabled-one" not in ids, "disabled 应过滤"


def test_catalog_separates_image_models():
    cat = uc.normalize_catalog(CATALOG_FIXTURE)
    img = [m["id"] for m in cat["image"]]
    chat = [m["id"] for m in cat["chat"]]
    assert "hunyuan-image-v3.0" in img, "text-to-image 应进生图列表"
    assert "hunyuan-image-v3.0" not in chat, "生图模型不应进对话列表"


def test_catalog_parses_reasoning_both_shapes():
    cat = uc.normalize_catalog(CATALOG_FIXTURE)
    by_id = {m["id"]: m for m in cat["chat"]}
    # 数组形态
    assert by_id["glm-5.3"]["supports_efforts"] == ["low", "high", "max"]
    assert by_id["glm-5.3"]["default_effort"] == "high"
    # 单档形态（effort 无数组）
    assert by_id["auto"]["supports_efforts"] == ["high"]
    # 数组形态但 default 不在数组内 → default 置空（不宣称不支持的默认档）
    assert by_id["deepseek-v4.1-flash"]["supports_efforts"] == ["high"]
    assert by_id["deepseek-v4.1-flash"]["default_effort"] == "high"


def test_catalog_default_effort_not_in_efforts_is_dropped():
    m = uc._normalize_one({"id": "x", "reasoning": {"supportedEfforts": ["low"], "defaultEffort": "max"}})
    assert m["default_effort"] == "", "default 不在 supported 内时必须置空"


def test_catalog_carries_capability_fields():
    cat = uc.normalize_catalog(CATALOG_FIXTURE)
    by_id = {m["id"]: m for m in cat["chat"]}
    d = by_id["deepseek-v4.1-flash"]
    assert d["context_window"] == 168000
    assert d["max_output_tokens"] == 128000
    assert d["supports_images"] is True
    assert d["supports_reasoning"] is True
    assert d["only_reasoning"] is True


def test_catalog_handles_empty_and_malformed():
    assert uc.normalize_catalog({})["chat"] == []
    assert uc.normalize_catalog({"data": None})["chat"] == []
    assert uc.normalize_catalog({"data": {"models": [], "agents": []}})["chat"] == []


# ---------------------------------------------------------------------------
# role 归一化
# ---------------------------------------------------------------------------

def test_developer_role_becomes_system():
    obj = {"messages": [{"role": "developer", "content": "x"},
                        {"role": "user", "content": "y"}]}
    assert uc.normalize_roles(obj) == 1
    assert obj["messages"][0]["role"] == "system"
    assert obj["messages"][1]["role"] == "user"


def test_other_roles_untouched():
    obj = {"messages": [{"role": "system", "content": "a"},
                        {"role": "Developer", "content": "b"},
                        {"role": "weird", "content": "c"}]}
    uc.normalize_roles(obj)
    assert obj["messages"][0]["role"] == "system"
    assert obj["messages"][1]["role"] == "system", "大小写不敏感"
    assert obj["messages"][2]["role"] == "weird", "未知 role 原样保留"


# ---------------------------------------------------------------------------
# tool_choice 归一化
# ---------------------------------------------------------------------------

def test_tool_choice_object_forms():
    cases = [
        ({"type": "auto"}, "auto"),
        ({"type": "required"}, "required"),
        ({"type": "function", "function": {"name": "Bash"}}, "Bash"),
    ]
    for tc, want in cases:
        obj = {"tool_choice": copy.deepcopy(tc)}
        uc.normalize_tool_choice(obj)
        assert obj["tool_choice"] == want, f"{tc} → {obj.get('tool_choice')} want {want}"


def test_tool_choice_none_suppresses_tools():
    obj = {"tool_choice": "none", "tools": [{"x": 1}], "functions": [{"y": 2}]}
    uc.normalize_tool_choice(obj)
    assert "tool_choice" not in obj
    assert "tools" not in obj and "functions" not in obj


def test_tool_choice_object_none_suppresses_tools():
    obj = {"tool_choice": {"type": "none"}, "tools": [{"x": 1}]}
    uc.normalize_tool_choice(obj)
    assert "tool_choice" not in obj and "tools" not in obj


def test_tool_choice_string_auto_untouched():
    obj = {"tool_choice": "auto"}
    assert uc.normalize_tool_choice(obj) is False
    assert obj["tool_choice"] == "auto"


def test_tool_choice_unknown_object_deleted():
    obj = {"tool_choice": {"type": "weird"}}
    uc.normalize_tool_choice(obj)
    assert "tool_choice" not in obj


# ---------------------------------------------------------------------------
# 孤儿 tool_call 清理
# ---------------------------------------------------------------------------

def _msgs(*ms):
    return {"messages": list(ms)}


def test_no_tool_traffic_zero_change():
    obj = _msgs({"role": "user", "content": "hi"},
                {"role": "assistant", "content": "yo"})
    before = copy.deepcopy(obj)
    assert uc.cleanup_orphan_tool_calls(obj) == 0
    assert obj == before


def test_orphan_call_dropped():
    obj = _msgs({"role": "assistant", "content": None,
                 "tool_calls": [{"id": "c1", "type": "function",
                                 "function": {"name": "Bash", "arguments": "{}"}}]})
    assert uc.cleanup_orphan_tool_calls(obj) == 1
    assert "tool_calls" not in obj["messages"][0], "配不上的 tool_calls 键应整个删除"
    assert "role" in obj["messages"][0] or obj["messages"][0].get("content") is None


def test_all_or_nothing_per_assistant_message():
    """2 个 call，只有 c1 有结果 → 两个都丢（不做部分保留）。"""
    obj = _msgs(
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "A", "arguments": "{}"}},
            {"id": "c2", "type": "function", "function": {"name": "B", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "r1"},
    )
    uc.cleanup_orphan_tool_calls(obj)
    assert "tool_calls" not in obj["messages"][0]
    assert len(obj["messages"]) == 1, "孤儿 tool 结果应被整条删除"


def test_fully_paired_untouched():
    obj = _msgs(
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "A", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "r1"},
    )
    before = copy.deepcopy(obj)
    assert uc.cleanup_orphan_tool_calls(obj) == 0
    assert obj == before


def test_orphan_tool_result_message_removed():
    obj = _msgs({"role": "user", "content": "q"},
                {"role": "tool", "tool_call_id": "ghost", "content": "r"})
    uc.cleanup_orphan_tool_calls(obj)
    assert len(obj["messages"]) == 1
    assert obj["messages"][0]["role"] == "user"


def test_out_of_order_pairing_still_matches():
    """tool 结果出现在 assistant 之前也应配对成功（集合语义，与顺序无关）。"""
    obj = _msgs(
        {"role": "tool", "tool_call_id": "c1", "content": "r1"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "A", "arguments": "{}"}}]},
    )
    assert uc.cleanup_orphan_tool_calls(obj) == 0


# ---------------------------------------------------------------------------
# reasoning_effort 归一化
# ---------------------------------------------------------------------------

def test_effort_supported_passes_through():
    obj = {"reasoning_effort": "high"}
    assert uc.normalize_reasoning_effort(obj, ["low", "high", "max"]) is None
    assert obj["reasoning_effort"] == "high"


def test_effort_unsupported_downgrades_to_nearest_below():
    obj = {"reasoning_effort": "max"}
    res = uc.normalize_reasoning_effort(obj, ["low", "high"])
    assert res == ("max", "high")
    assert obj["reasoning_effort"] == "high"


def test_effort_all_supported_higher_floors_to_lowest():
    obj = {"reasoning_effort": "low"}
    res = uc.normalize_reasoning_effort(obj, ["high", "max"])
    assert res == ("low", "high")


def test_effort_unknown_supported_list_never_downgrades():
    """上游未声明能力时绝不擅自降级——这正是要修的 bug（max 被硬降成 high）。"""
    obj = {"reasoning_effort": "max"}
    assert uc.normalize_reasoning_effort(obj, None) is None
    assert obj["reasoning_effort"] == "max"
    assert uc.normalize_reasoning_effort(obj, []) is None
    assert obj["reasoning_effort"] == "max"


def test_effort_camel_case_supported():
    obj = {"reasoningEffort": "low"}
    res = uc.normalize_reasoning_effort(obj, ["high"])
    assert res == ("low", "high")
    assert obj["reasoningEffort"] == "high"


def test_effort_absent_is_noop():
    obj = {}
    assert uc.normalize_reasoning_effort(obj, ["low"]) is None
    assert obj == {}


def test_effort_unknown_spelling_left_to_upstream():
    """完全未知的档位拼写不猜测，交给上游校验（会 400 code=11150）。"""
    obj = {"reasoning_effort": "GARBAGE"}
    assert uc.normalize_reasoning_effort(obj, ["low", "high"]) is None
    assert obj["reasoning_effort"] == "GARBAGE"


def test_effort_case_insensitive_match():
    obj = {"reasoning_effort": "HIGH"}
    assert uc.normalize_reasoning_effort(obj, ["high"]) is None, "大小写不同但同档位应视为支持"


# ---------------------------------------------------------------------------
# reasoning_content 回填
# ---------------------------------------------------------------------------

def test_backfill_copies_reasoning_to_reasoning_content():
    obj = {"model": "deepseek-v4.1-flash", "messages": [
        {"role": "assistant", "content": "a", "reasoning": "thinking..."},
        {"role": "user", "content": "b"},
        {"role": "assistant", "content": "c"},
    ]}
    n = uc.backfill_reasoning_content(obj)
    assert n == 2
    assert obj["messages"][0]["reasoning_content"] == "thinking..."
    assert obj["messages"][2]["reasoning_content"] == ""


def test_backfill_noop_without_trace():
    obj = {"model": "deepseek-v4.1-flash", "messages": [
        {"role": "assistant", "content": "a"},
        {"role": "user", "content": "b"},
    ]}
    assert uc.backfill_reasoning_content(obj) == 0
    assert "reasoning_content" not in obj["messages"][0], "无痕迹时不白白加字段"


def test_backfill_only_for_deepseek():
    obj = {"model": "glm-5.3", "messages": [
        {"role": "assistant", "content": "a", "reasoning": "t"}]}
    assert uc.backfill_reasoning_content(obj) == 0
    assert "reasoning_content" not in obj["messages"][0]


def test_backfill_does_not_overwrite_existing():
    obj = {"model": "deepseek-v4-pro", "messages": [
        {"role": "assistant", "content": "a", "reasoning_content": "keep"}]}
    uc.backfill_reasoning_content(obj)
    assert obj["messages"][0]["reasoning_content"] == "keep"


def test_is_deepseek_model():
    assert uc.is_deepseek_model("deepseek-v4.1-flash")
    assert uc.is_deepseek_model("DeepSeek-V4-Pro")
    assert not uc.is_deepseek_model("glm-5.3")
    assert not uc.is_deepseek_model("")


# ---------------------------------------------------------------------------
# 指纹脱敏
# ---------------------------------------------------------------------------

def test_sanitize_11128_rewrite():
    assert uc.sanitize_text("error code 11128 happened") == "error code 11-128 happened"


def test_sanitize_claude_code_identity_both_endings():
    a = uc.sanitize_text("You are Claude Code, Anthropic's official CLI for Claude.")
    assert "official CLI tool for Claude" in a
    b = uc.sanitize_text("You are Claude Code, Anthropic's official CLI for Claude, running within the Claude Agent SDK.")
    assert "official CLI tool for Claude" in b, "桌面端逗号结尾也要覆盖"


def test_sanitize_main_branch():
    out = uc.sanitize_text("Main branch (you will usually use this for PRs)")
    assert out.startswith("Default branch (")


def test_sanitize_codex_sentence():
    out = uc.sanitize_text("You are a coding agent running in the Codex CLI, a terminal-based coding assistant.")
    assert "Codex CLI tool" in out


def test_sanitize_feedback_sentence():
    out = uc.sanitize_text(
        "To give feedback, users should report the issue at https://github.com/anthropics/claude-code/issues")
    assert "To provide feedback" in out


def test_sanitize_header_kv_removed():
    out = uc.sanitize_text("x-anthropic-billing-header: abc=1; rest of text")
    assert "abc=1" not in out
    assert "rest of text" in out


def test_sanitize_bare_header_abbreviated():
    out = uc.sanitize_text("see `x-anthropic-billing-header` for details")
    assert "x-anthropic-billing-hdr" in out
    assert "x-anthropic-billing-header" not in out


def test_sanitize_cc_kv_removed():
    out = uc.sanitize_text("cmd cc_entrypoint=cli cc_version=1.2 tail")
    assert "cc_entrypoint" not in out and "cc_version" not in out
    assert "tail" in out


def test_sanitize_clean_text_untouched():
    s = "please use main branch for this repo"
    assert uc.sanitize_text(s) == s, "非模板句不应被改写"


def test_sanitize_identity_without_period_covers_both_endings():
    """模板句故意不带尾部标点：既要覆盖 CLI 的 `.` 结尾，也要覆盖桌面端的 `,` 结尾。

    代价是 `!` 等其它结尾也会被一并改写——这是刻意的取舍（宁可多改，
    也不要漏掉桌面端变体导致整包被 400）。参考实现同样如此。
    """
    for tail in (".", ",", "!", ""):
        s = "You are Claude Code, Anthropic's official CLI for Claude" + tail
        out = uc.sanitize_text(s)
        assert "official CLI tool for Claude" in out, f"结尾 {tail!r} 未被覆盖"


def test_sanitize_non_template_text_untouched():
    """非模板句不应被误伤。"""
    for s in ("You are a helpful assistant.",
              "Claude is a model by Anthropic.",
              "please review the official CLI for Claude docs"):
        assert uc.sanitize_text(s) == s, f"{s!r} 被误改"


def test_sanitize_messages_reaches_tool_arguments_with_null_content():
    """content 为 null 的工具调用轮也必须脱敏 arguments（历史盲区）。"""
    obj = {"messages": [{"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function", "function": {
            "name": "Write", "arguments": json.dumps({"text": "code 11128 here"})}}]}]}
    assert uc.sanitize_messages(obj) == 1
    args = obj["messages"][0]["tool_calls"][0]["function"]["arguments"]
    assert "11128" not in args and "11-128" in args


def test_sanitize_messages_multimodal_only_text_parts():
    obj = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "code 11128"},
        {"type": "image_url", "image_url": {"url": "http://x/11128.png"}}]}]}
    uc.sanitize_messages(obj)
    assert obj["messages"][0]["content"][0]["text"] == "code 11-128"
    assert obj["messages"][0]["content"][1]["image_url"]["url"] == "http://x/11128.png", \
        "非 text part 不应改动"


def test_sanitize_messages_reasoning_content():
    obj = {"messages": [{"role": "assistant", "content": "ok",
                         "reasoning_content": "I saw 11128"}]}
    uc.sanitize_messages(obj)
    assert obj["messages"][0]["reasoning_content"] == "I saw 11-128"


# ---------------------------------------------------------------------------
# 截断工具调用
# ---------------------------------------------------------------------------

def test_is_truncated_arguments():
    assert uc.is_truncated_arguments("") is False
    assert uc.is_truncated_arguments("   ") is False
    assert uc.is_truncated_arguments("{}") is False
    assert uc.is_truncated_arguments('{"a":1}') is False
    assert uc.is_truncated_arguments("null") is False
    assert uc.is_truncated_arguments("[1,2]") is False
    assert uc.is_truncated_arguments('{"command": "ls -') is True
    assert uc.is_truncated_arguments('{"a":') is True
    assert uc.is_truncated_arguments("not json at all") is True


def test_drop_truncated_tool_calls():
    msg = {"tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "A", "arguments": '{"ok":1}'}},
        {"id": "c2", "type": "function", "function": {"name": "B", "arguments": '{"bad":'}},
    ]}
    assert uc.drop_truncated_tool_calls(msg) == 1
    assert len(msg["tool_calls"]) == 1
    assert msg["tool_calls"][0]["id"] == "c1"


def test_drop_truncated_all_removes_key():
    msg = {"tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "A", "arguments": '{"bad":'}}]}
    assert uc.drop_truncated_tool_calls(msg) == 1
    assert "tool_calls" not in msg


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------

def test_prepare_body_full_pipeline():
    obj = {
        "model": "deepseek-v4.1-flash",
        "stream": False,
        "messages": [
            {"role": "developer", "content": "sys"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "orphan", "type": "function",
                 "function": {"name": "A", "arguments": "{}"}}]},
        ],
        "tool_choice": {"type": "function", "function": {"name": "Bash"}},
        "reasoning_effort": "max",
    }
    uc.prepare_upstream_body(obj, supported_efforts=["high"])
    assert obj["stream"] is True, "上游只支持流式"
    assert obj["stream_options"] == {"include_usage": True}
    assert obj["messages"][0]["role"] == "system"
    assert "tool_calls" not in obj["messages"][1], "孤儿 tool_call 应清理"
    assert obj["tool_choice"] == "Bash"
    assert obj["reasoning_effort"] == "high", "max 不在支持列表 → 降级"


def test_prepare_body_respects_upstream_max_support():
    """上游声明支持 max 时不得降级（防止把 max 硬降成 high 的回归）。"""
    obj = {"model": "glm-5.3", "messages": [], "reasoning_effort": "max"}
    uc.prepare_upstream_body(obj, supported_efforts=["low", "high", "max"])
    assert obj["reasoning_effort"] == "max"


def test_prepare_body_no_capability_keeps_effort():
    obj = {"model": "deepseek-v4.1-flash", "messages": [], "reasoning_effort": "max"}
    uc.prepare_upstream_body(obj, supported_efforts=None)
    assert obj["reasoning_effort"] == "max", "能力未知时必须原样透传"


def test_prepare_body_sanitize_optional():
    base = {"model": "glm-5.3", "messages": [{"role": "user", "content": "code 11128"}]}
    a = copy.deepcopy(base)
    uc.prepare_upstream_body(a, sanitize=False)
    assert a["messages"][0]["content"] == "code 11128", "未开启脱敏时不动内容"
    b = copy.deepcopy(base)
    uc.prepare_upstream_body(b, sanitize=True)
    assert b["messages"][0]["content"] == "code 11-128"


# ---------------------------------------------------------------------------
# 运行器
# ---------------------------------------------------------------------------

def _run_all() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
            print(f"  PASS  {name}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {name}\n        {e}")
        except Exception as e:
            failed += 1
            print(f"  ERROR {name}\n        {type(e).__name__}: {e}")
    print(f"\n{passed} passed, {failed} failed, {len(tests)} total")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
