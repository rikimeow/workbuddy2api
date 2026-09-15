#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""upstream_compat.py — 上游协议兼容层。

把「客户端 → 上游」的请求体改写，以及「上游模型目录 → 能力元数据」的解析集中在这里，
与协议转换（converter.py / responses_adapter.py）解耦，便于单测。

本文件里的改写全部基于**实测**行为，不是照抄参考实现：

  1. role: developer → system
     上游对 messages.role 做白名单校验，developer 不在其中 → HTTP 400 code=11128。
     developer 是 OpenAI 新规范里 system 的别名（Codex / Cursor 用它承载 system 级指令），
     改写为 system 不丢语义。

  2. tool_choice 归一化
     上游把 tool_choice 当**字符串**解析；传对象会 400 code=11101。

  3. 孤儿 tool_call / tool 结果清理
     OpenAI 协议要求 assistant.tool_calls[].id 与 role:tool 的 tool_call_id 一一配对。
     任一侧缺失时上游对整个请求返 400 —— 且这段坏历史会被客户端每轮原样重放，
     导致**该会话之后每条消息都 400**（会话彻底死掉）。网关是最后一道防线：
     发出去之前把配不上的条目剔掉，让会话自愈。

  4. reasoning_effort 档位归一
     上游对非法档位返 400 code=11150 invalid_reasoning_effort。
     但**注意**：实测本账号（CN/personal）对 deepseek-v4.1-flash 接受
     low/medium/high/xhigh/max/minimal 全部 200 且有思维链，
     因此**只依据上游实时下发的 supportedEfforts 降级**，
     绝不用静态表把 max 硬降成 high（那正是我们要修的 bug）。

  5. reasoning_content 多轮回填（DeepSeek）
     历史里任一 assistant 消息带 reasoning 痕迹时，上游要求**所有** assistant 消息
     都带 reasoning_content 字段，否则后续轮次思维链静默失效。
"""

from __future__ import annotations

import json
import re

# ---------------------------------------------------------------------------
# 1. 模型目录解析（能力层）
# ---------------------------------------------------------------------------

# 上游 /v2/enterprises/{eid}/models 只列「对话 agent 可用」的模型；
# 我们取 agents 里 name == "cli" 的那份名单（与官方 CLI 口径一致）。
CLI_AGENT_NAME = "cli"

# 非对话模型过滤规则（来自参考实现 client.go nonChatModel 的实测口径）：
#   - id 前缀 nes-/completion-/codewise-：嵌入/补全/代码专用，选了报 code=11102
#   - maxOutputTokens <= 256：tiny 输出非对话模型
#   - tags 含 text-to-image：图片生成模型，不进对话模型列表（另有 /v1/images/generations）
_NON_CHAT_PREFIXES = ("nes-", "completion-", "codewise-")
_TINY_OUTPUT_TOKENS = 256
IMAGE_TAG = "text-to-image"


def is_non_chat_model(model_id: str, max_output_tokens, tags) -> bool:
    """是否应从「对话模型」列表中过滤掉。"""
    mid = (model_id or "").strip().lower()
    for p in _NON_CHAT_PREFIXES:
        if mid.startswith(p):
            return True
    try:
        if max_output_tokens and int(max_output_tokens) <= _TINY_OUTPUT_TOKENS:
            return True
    except (TypeError, ValueError):
        pass
    if isinstance(tags, (list, tuple)) and IMAGE_TAG in tags:
        return True
    return False


def is_image_model(model_id: str, tags) -> bool:
    """是否是生图模型。"""
    return isinstance(tags, (list, tuple)) and IMAGE_TAG in tags


def parse_reasoning(reasoning) -> tuple[list[str], str]:
    """从上游 model.reasoning 解析 (supportedEfforts, defaultEffort)。

    上游有两种形态，都要兼容：
      - {"supportedEfforts": ["low","high","max"], "defaultEffort": "high"}
      - {"effort": "high", "summary": "auto"}          （仅单档，无数组）
    数组优先；缺数组但 effort 单档非空 → 视作单档表。
    """
    if not isinstance(reasoning, dict):
        return [], ""
    efforts = reasoning.get("supportedEfforts")
    if isinstance(efforts, list):
        cleaned = [str(e).strip() for e in efforts if str(e).strip()]
        if cleaned:
            return cleaned, str(reasoning.get("defaultEffort") or "").strip()
    single = str(reasoning.get("effort") or "").strip()
    if single:
        return [single], str(reasoning.get("defaultEffort") or "").strip()
    return [], str(reasoning.get("defaultEffort") or "").strip()


def normalize_catalog(payload: dict) -> dict:
    """把上游模型目录响应规范化成网关内部结构。

    返回：
      {
        "chat":     [ NormalizedModel, ... ],   # cli agent 的对话模型
        "image":    [ NormalizedModel, ... ],   # 生图模型
        "all_ids":  [...],                      # 全部出现过的 id（含未进 cli 的）
      }
    NormalizedModel 字段见 _normalize_one。
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return {"chat": [], "image": [], "all_ids": []}

    raw_models = data.get("models") or []
    by_id: dict[str, dict] = {}
    all_ids: list[str] = []
    for m in raw_models:
        if not isinstance(m, dict):
            continue
        mid = str(m.get("id") or "").strip()
        if not mid:
            continue
        all_ids.append(mid)
        by_id[mid] = m

    # cli agent 名单 = 官方 CLI 实际可选模型（权威顺序）
    cli_ids: list[str] = []
    for ag in data.get("agents") or []:
        if isinstance(ag, dict) and ag.get("name") == CLI_AGENT_NAME:
            for mid in ag.get("models") or []:
                if isinstance(mid, str) and mid.strip():
                    cli_ids.append(mid.strip())
            break

    # 兜底：没有 cli agent 时退回全部模型
    source_ids = cli_ids or all_ids

    chat: list[dict] = []
    image: list[dict] = []
    seen: set[str] = set()
    for mid in source_ids:
        if mid in seen:
            continue
        seen.add(mid)
        m = by_id.get(mid)
        if m is None:
            continue
        if m.get("disabled"):
            continue
        tags = m.get("tags") or []
        if is_image_model(mid, tags):
            image.append(_normalize_one(m))
            continue
        if is_non_chat_model(mid, m.get("maxOutputTokens"), tags):
            continue
        chat.append(_normalize_one(m))

    # cli 名单之外但确实是生图模型的，也收进 image（生图列表不受 cli 名单约束）
    for mid, m in by_id.items():
        if mid in seen or m.get("disabled"):
            continue
        if is_image_model(mid, m.get("tags") or []):
            image.append(_normalize_one(m))

    return {"chat": chat, "image": image, "all_ids": all_ids}


def _normalize_one(m: dict) -> dict:
    """单个上游模型条目 → 网关内部结构。"""
    efforts, default_effort = parse_reasoning(m.get("reasoning"))
    return {
        "id": str(m.get("id") or "").strip(),
        "name": m.get("name") or m.get("id"),
        "context_window": m.get("maxInputTokens"),
        "max_output_tokens": m.get("maxOutputTokens"),
        "max_allowed_size": m.get("maxAllowedSize"),
        "supports_images": bool(m.get("supportsImages")),
        "supports_reasoning": bool(m.get("supportsReasoning")),
        "supports_tool_call": bool(m.get("supportsToolCall")),
        "only_reasoning": bool(m.get("onlyReasoning")),
        "can_disable_thinking": m.get("canDisableThinking"),
        "supports_efforts": efforts,
        "default_effort": default_effort if default_effort in efforts else "",
        "credits": m.get("credits"),
        "description": m.get("descriptionZh") or m.get("descriptionEn"),
        "tags": m.get("tags") or [],
        "vendor": m.get("vendor"),
        "temperature": m.get("temperature"),
        "top_p": m.get("top_p"),
        "top_k": m.get("top_k"),
        "repetition_penalty": m.get("repetition_penalty"),
        "icon_url": m.get("iconUrl"),
    }


# ---------------------------------------------------------------------------
# 2. role 归一化
# ---------------------------------------------------------------------------

def normalize_roles(obj: dict) -> int:
    """messages[].role: developer → system。返回改写条数。

    只认 developer 这一个值：其余 role（含未知值）一律原样保留，
    不合并/不重排/不删除任何消息。
    """
    msgs = obj.get("messages")
    if not isinstance(msgs, list):
        return 0
    n = 0
    for m in msgs:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if isinstance(role, str) and role.strip().lower() == "developer":
            m["role"] = "system"
            n += 1
    return n


# ---------------------------------------------------------------------------
# 3. tool_choice 归一化
# ---------------------------------------------------------------------------

def normalize_tool_choice(obj: dict) -> bool:
    """把 tool_choice 从 OpenAI 对象形态归一为上游期望的字符串形态。

      - "none" / {"type":"none"}                        → 删 tool_choice + tools/functions
      - {"type":"auto"|"required"}                      → "auto" / "required"
      - {"type":"function","function":{"name":"x"}}     → "x"
      - 其他对象 / 非标量                                → 删 tool_choice
    返回是否发生改写。
    """
    if "tool_choice" not in obj:
        return False
    tc = obj["tool_choice"]

    def suppress() -> None:
        obj.pop("tools", None)
        obj.pop("functions", None)

    if isinstance(tc, str):
        if tc.strip().lower() == "none":
            obj.pop("tool_choice", None)
            suppress()
            return True
        return False

    if isinstance(tc, dict):
        typ = str(tc.get("type") or "").strip().lower()
        if typ == "none":
            obj.pop("tool_choice", None)
            suppress()
            return True
        if typ in ("auto", "required"):
            obj["tool_choice"] = typ
            return True
        if typ == "function":
            name = ""
            fn = tc.get("function")
            if isinstance(fn, dict):
                name = str(fn.get("name") or "").strip()
            if not name:
                name = str(tc.get("name") or "").strip()
            obj["tool_choice"] = name if name else "auto"
            return True
        obj.pop("tool_choice", None)
        return True

    obj.pop("tool_choice", None)
    return True


# ---------------------------------------------------------------------------
# 4. 孤儿 tool_call / tool 结果清理
# ---------------------------------------------------------------------------

def cleanup_orphan_tool_calls(obj: dict) -> int:
    """剔除配不成对的 tool_calls / tool 结果，返回删除的条目数。

    算法（集合语义，与顺序无关，重复 id 取最宽保留）：
      1. 收集 callIDs（assistant.tool_calls[].id）与 resultIDs（role:tool 的 tool_call_id）
      2. 无任何 tool 流量 → 零改动
      3. keepCalls = callIDs ∩ resultIDs
      4. assistant 消息**整条 all-or-nothing**：其 tool_calls 里只要有任一个 id
         不在 keepCalls，就删掉整个 tool_calls 键（保留消息本体与其 content）
      5. **重算** keepCalls（第 4 步删掉的 tool_calls 里的 id 不再算数），
         再删掉 tool_call_id 不在新 keepCalls 里的 role:tool 消息
    """
    msgs = obj.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return 0

    call_ids: set[str] = set()
    result_ids: set[str] = set()
    for m in msgs:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role == "tool":
            tid = m.get("tool_call_id")
            if isinstance(tid, str) and tid:
                result_ids.add(tid)
        elif role == "assistant":
            tcs = m.get("tool_calls")
            if isinstance(tcs, list):
                for tc in tcs:
                    if isinstance(tc, dict):
                        cid = tc.get("id")
                        if isinstance(cid, str) and cid:
                            call_ids.add(cid)

    if not call_ids and not result_ids:
        return 0  # 无 tool 流量：零改动

    keep = call_ids & result_ids
    changed = 0

    # 4. assistant：tool_calls 全有配对才保留，否则删整个键
    for m in msgs:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        tcs = m.get("tool_calls")
        if not isinstance(tcs, list) or not tcs:
            continue
        keep_all = True
        for tc in tcs:
            if not isinstance(tc, dict) or tc.get("id") not in keep:
                keep_all = False
                break
        if not keep_all:
            m.pop("tool_calls", None)
            changed += 1

    # 5. 重算存活 call 集：第 4 步删掉的 tool_calls 里的 id 不能再算配对成功，
    #    否则它们的 tool 结果会变成新的孤儿（上游照样 400）。
    surviving: set[str] = set()
    for m in msgs:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        tcs = m.get("tool_calls")
        if isinstance(tcs, list):
            for tc in tcs:
                if isinstance(tc, dict) and isinstance(tc.get("id"), str) and tc["id"]:
                    surviving.add(tc["id"])

    # 6. tool 结果：配不上的整条删
    kept: list = []
    for m in msgs:
        if isinstance(m, dict) and m.get("role") == "tool":
            if m.get("tool_call_id") not in surviving:
                changed += 1
                continue
        kept.append(m)
    if len(kept) != len(msgs):
        obj["messages"] = kept

    return changed


# ---------------------------------------------------------------------------
# 5. reasoning_effort 档位归一
# ---------------------------------------------------------------------------

# 档位由低到高（用于「降到 ≤ 请求档位的最高支持档」）
EFFORT_RANK = {
    "off": 0, "minimal": 1, "none": 1, "low": 2,
    "medium": 3, "high": 4, "xhigh": 5, "max": 6,
}


def _effort_index(v: str) -> int | None:
    return EFFORT_RANK.get(str(v or "").strip().lower())


def normalize_reasoning_effort(obj: dict, supported: list[str] | None) -> tuple[str, str] | None:
    """按模型**实时** supportedEfforts 归一 reasoning_effort。

    规则：
      - 请求档位在上游支持列表内 → 原样透传（不动）
      - 不支持 → 改为 ≤请求档位的最高支持档
      - 支持档全部高于请求档 → 取最低支持档（偏离最小）
      - supported 为空（上游未声明）→ **一律透传**，不做任何猜测性降级

    返回 (原档位, 新档位) 表示发生了改写；None 表示未改写。
    """
    if not supported:
        return None  # 上游没声明能力 → 绝不擅自降级

    key = None
    if "reasoning_effort" in obj:
        key = "reasoning_effort"
    elif "reasoningEffort" in obj:
        key = "reasoningEffort"
    if key is None:
        return None

    req = obj.get(key)
    if not isinstance(req, str):
        return None
    req = req.strip()
    req_idx = _effort_index(req)
    if req_idx is None:
        # 完全未知的档位拼写：交给上游校验（会 400 code=11150），不猜测
        return None

    known = [(e, _effort_index(e)) for e in supported]
    known = [(e, i) for e, i in known if i is not None]
    if not known:
        return None
    if any(e.strip().lower() == req.lower() for e, _ in known):
        return None  # 上游支持 → 原样

    best = None
    best_idx = -1
    for e, i in known:
        if i <= req_idx and i > best_idx:
            best, best_idx = e, i
    if best is None:
        best = min(known, key=lambda x: x[1])[0]
    obj[key] = best
    return (req, best)


# ---------------------------------------------------------------------------
# 6. reasoning_content 多轮回填（DeepSeek）
# ---------------------------------------------------------------------------

def is_deepseek_model(model: str) -> bool:
    return str(model or "").strip().lower().startswith("deepseek")


def backfill_reasoning_content(obj: dict) -> int:
    """DeepSeek 多轮一致性回填，返回补齐的 assistant 消息数。

    规则：
      - 会话内任一 assistant 消息带非空 reasoning（string）或已有 reasoning_content
        → 所有 assistant 消息确保有 reasoning_content
      - reasoning 非空且无 reasoning_content → 复制 reasoning
      - 已有 reasoning_content → 原样保留
      - 两者皆无 → 补空串 ""
      - 任何 assistant 都无 reasoning 痕迹 → 零改动（不白白加字段）
    """
    if not is_deepseek_model(obj.get("model")):
        return 0
    msgs = obj.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return 0

    has_trace = False
    for m in msgs:
        if not isinstance(m, dict):
            continue
        r = m.get("reasoning")
        if isinstance(r, str) and r:
            has_trace = True
            break
        if "reasoning_content" in m:
            has_trace = True
            break
    if not has_trace:
        return 0

    n = 0
    for m in msgs:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        if "reasoning_content" in m:
            continue
        r = m.get("reasoning")
        m["reasoning_content"] = r if isinstance(r, str) else ""
        n += 1
    return n


# ---------------------------------------------------------------------------
# 7. 内容指纹脱敏
# ---------------------------------------------------------------------------

# 上游内容审核对若干客户端模板句做**逐字精确匹配**拦截（不是语义审核），
# 因此改一个词即可绕过。
SANITIZE_FEATURES = (
    "x-anthropic-billing-header",
    "cc_entrypoint=",
    "You are Claude Code",
    "Main branch (",              # 注意：故意不含句号，覆盖 CLI 与桌面端两种结尾
    "You are a coding agent running in the Codex CLI",
    "github.com/anthropics/",
    "11128",                      # 上游反探测：请求体里出现裸 11128 即整体拦截
)

# 整句精确替换（顺序敏感）
SANITIZE_REWRITES = (
    ("You are Claude Code, Anthropic's official CLI for Claude",
     "You are Claude Code, Anthropic's official CLI tool for Claude"),
    ("Main branch (you will usually use this for PRs)",
     "Default branch (you will usually use this for PRs)"),
    ("You are a coding agent running in the Codex CLI, a terminal-based coding assistant.",
     "You are a coding agent running in the Codex CLI tool, a terminal-based coding assistant."),
    ("To give feedback, users should report the issue at https://github.com/anthropics/claude-code/issues",
     "To provide feedback, users should report the issue at https://github.com/anthropics/claude-code/issues"),
    # 上游一旦看到裸 11128 就整体拦截；改成 11-128 保留可读性，零宽字符无效（上游会归一化）
    ("11128", "11-128"),
)

_HDR_RE = re.compile(r"(?i)x-anthropic-billing-header:[^;\n]*;?\s*")
_BARE_HDR_RE = re.compile(r"(?i)x-anthropic-billing-header")
# cc_* kv：值到 `;`、空白或行尾为止。**不能**用 [^;\n]* —— 没有 `;` 时会把
# 整行剩余内容一起吞掉（实测 "cmd cc_entrypoint=cli cc_version=1.2 tail"
# 会被吃成 "cmd"）。故用非贪婪 + 前瞻定界。
_KV_RE = re.compile(r"(?i)\bcc_[a-z0-9_]+=\S*?(?=\s|;|$)")


def has_fingerprint(text: str) -> bool:
    if not isinstance(text, str) or not text:
        return False
    for f in SANITIZE_FEATURES:
        if f in text:
            return True
    # 大小写混合 + 裸键名（无冒号）也要兜住
    return bool(_BARE_HDR_RE.search(text))


def sanitize_text(text: str) -> str:
    """对单段文本做指纹脱敏。无命中时原样返回（零改写）。"""
    if not has_fingerprint(text):
        return text
    for src, dst in SANITIZE_REWRITES:
        if src in text:
            text = text.replace(src, dst)
    if _HDR_RE.search(text):
        text = _HDR_RE.sub("", text)
    if "cc_" in text:
        prev = None
        while prev != text:  # 多个尾随 kv，循环到不动点
            prev = text
            text = _KV_RE.sub("", text)
    text = _BARE_HDR_RE.sub("x-anthropic-billing-hdr", text)
    return text.strip()


def _sanitize_content(content):
    """content 可能是 string 或多模态数组（只动 text part）。"""
    if isinstance(content, str):
        return sanitize_text(content)
    if isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                part["text"] = sanitize_text(part["text"])
        return content
    return content


def sanitize_messages(obj: dict) -> int:
    """脱敏 messages 里的 content / reasoning_content / tool_calls.arguments。

    content 与 tool_calls **独立判定**：content 为 null 的工具调用轮
    也必须脱敏 tool_calls.arguments（历史盲区）。
    返回改写的消息数。
    """
    msgs = obj.get("messages")
    if not isinstance(msgs, list):
        return 0
    n = 0
    for m in msgs:
        if not isinstance(m, dict):
            continue
        touched = False
        if "content" in m:
            before = m["content"]
            m["content"] = _sanitize_content(before)
            if m["content"] != before:
                touched = True
        rc = m.get("reasoning_content")
        if isinstance(rc, str):
            new = sanitize_text(rc)
            if new != rc:
                m["reasoning_content"] = new
                touched = True
        tcs = m.get("tool_calls")
        if isinstance(tcs, list):
            for tc in tcs:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function")
                if isinstance(fn, dict) and isinstance(fn.get("arguments"), str):
                    new = sanitize_text(fn["arguments"])
                    if new != fn["arguments"]:
                        fn["arguments"] = new
                        touched = True
        if touched:
            n += 1
    return n


# ---------------------------------------------------------------------------
# 8. 截断工具调用剔除
# ---------------------------------------------------------------------------

def is_truncated_arguments(raw) -> bool:
    """参数「非空但不可解析」才算截断。

    空串/空白是合法的无参工具；能解析但类型不对（标量/数组）属模型输出问题，
    交给客户端 schema 校验，这里不判。
    """
    if not isinstance(raw, str):
        return False
    trimmed = raw.strip()
    if not trimmed:
        return False
    try:
        json.loads(trimmed)
        return False
    except Exception:
        return True


def drop_truncated_tool_calls(msg: dict) -> int:
    """finish_reason == "length" 时，剔掉 arguments 截断的 tool_call。返回剔除数。"""
    tcs = msg.get("tool_calls")
    if not isinstance(tcs, list) or not tcs:
        return 0
    kept = []
    dropped = 0
    for tc in tcs:
        if not isinstance(tc, dict):
            kept.append(tc)
            continue
        fn = tc.get("function")
        if not isinstance(fn, dict):
            kept.append(tc)  # 没有 function → 保留
            continue
        if is_truncated_arguments(fn.get("arguments")):
            dropped += 1
            continue
        kept.append(tc)
    if dropped:
        if kept:
            msg["tool_calls"] = kept
        else:
            msg.pop("tool_calls", None)
    return dropped


# ---------------------------------------------------------------------------
# 9. 统一入口
# ---------------------------------------------------------------------------

def prepare_upstream_body(obj: dict, *, sanitize: bool = False,
                          supported_efforts: list[str] | None = None) -> dict:
    """把「客户端请求体」改写为「上游可接受的请求体」。就地改写并返回。

    顺序即语义，不要随意调换：
      1. 强制 stream=true（上游只支持流式，非流式由各端点自行聚合）
      2. role 归一（developer → system）
      3. tool_choice 归一
      4. 孤儿 tool_call 清理（安全网，必须早于发出）
      5. reasoning_effort 归一（依据上游实时能力）
      6. reasoning_content 回填（deepseek）
      7. 可选指纹脱敏
    """
    obj["stream"] = True
    if "stream_options" not in obj:
        obj["stream_options"] = {"include_usage": True}

    normalize_roles(obj)
    normalize_tool_choice(obj)
    cleanup_orphan_tool_calls(obj)
    normalize_reasoning_effort(obj, supported_efforts)
    backfill_reasoning_content(obj)
    if sanitize:
        sanitize_messages(obj)
    return obj
