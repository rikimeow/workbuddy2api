"""`auto-with-jev` 路由门限：用 TypeSafe Jev 给请求打结构化特征。

背景
----
`auto` 的语义是「让上游智能路由挑模型」，本地看不到它会落到哪个厂商模型。
本模块提供一个**可选**的替代语义 `auto-with-jev`：本地先用 Jev 判断这条请求
需要什么能力，再由 `_GATE_TIERS` 把判断映射成模型档位。

**延迟要提前想清楚，这是本方案的主要代价**
------------------------------------------
门限加在请求关键路径上，每个「会话的第一次」都要多等一次 Jev 往返。实测数据：

| 场景 | 墙钟 |
|---|---|
| 复用连接的正常响应 | **350 ms** |
| 复用连接 + state 较大（1k+ token） | 1.4-1.5 s |
| **不复用 `httpx.Client`**（每次都新建） | **1.4-1.7 s** ← 必须避免 |

第三行是踩过的坑：`httpx.post(...)` 每次都会新建 Client，而新建 Client 会重新
加载 SSL 上下文，本机实测 `create_default_context` 就要 600-720ms。所以本模块
**必须复用 `_client()`**，否则一个 1.2s 的超时会把大部分请求掐成 `timeout`。

缓解手段：会话级缓存（同一会话的多轮只付一次）、`ADMIN_ROUTER_GATE=off` 可整体关闭。
接受这个代价的前提是：上游自己的 `auto` 路由本身就有一层选路延迟。

设计取舍（都很具体，改之前请先读）
--------------------------------
* **Jev 只做语义判断，档位由代码映射。** 不把「选模型」直接交给模型：
  映射规则是可审查的常量、可单测、改了不用重跑推理，也不必把整份模型目录
  塞进 prompt（省 token、省维护）。
* **shadow 默认开启。** 门限只在 `X-Route-Mode: shadow` 或 `ADMIN_ROUTER_GATE=shadow`
  下**只记录不生效**：请求仍按原 `auto` 语义走，但判断结果写进 UsageLog 供对比。
  这是安全上线的前提 —— 门限的返回值会决定真实路由，必须先拿真实流量验证。
* **fail-open 是硬要求。** 超时 / 429 / 5xx / JSON 异常 / 形状不对 —— 一律返回
  `active=False`，请求退回原 `auto` 行为。门限绝不能让一次请求失败。
* **模型名必须白名单校验。** 即使 prompt 里的候选清单被注入，返回值也只能是
  白名单内的 id —— 否则等于给「生图模型混进聊天池」这类事故开了新入口。
* **只发最后一条 user 消息。** 请求体最多 4k 字符，不整段会话出境；
  state 越小 Jev 也越准。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, replace

import httpx

from admin.config import settings

_logger = logging.getLogger("admin.router_gate")

#: 门限用的模型别名。`jev-latest` 会随官方发版漂移；要钉住版本就把响应里的
#: `model` 字段（如 `jev-1.13.0`）写进环境变量，并按自己的节奏升级。
DEFAULT_MODEL = "jev-latest"

#: state 上限（字符）的默认值；实际取自 `settings.ROUTER_GATE_MAX_CHARS`。
MAX_STATE_CHARS = 4000

#: 请求体里最多扫描多少条消息去找最后一条 user 消息。
MAX_SCAN_MESSAGES = 12


@dataclass(frozen=True)
class Tier:
    """一个门限目标：能透传给上游的模型 id + 它适合什么任务。"""

    model: str
    description: str


#: 候选档位。`model` 必须是上游原生接受的 id（`fast-model` / `balanced-model` /
#: `deep-model` 由桌面端 CLI 定义，上游服务端动态路由，本地不用管它会落到哪个厂商模型）。
#: description 会作为 Choice 的 criteria 发给 Jev，是**判断质量的命门**：
#: 宁可写得具体、互斥，也不要写「好 / 一般 / 差」这种没有边界的描述。
GATE_TIERS: tuple[Tier, ...] = (
    Tier("fast-model", "简单问答、闲聊、格式转换、信息抽取、短文本改写；不需要深度推理"),
    Tier("balanced-model", "常规编码、解释代码、总结文档、多轮工具调用；需要中等理解力"),
    Tier("deep-model", "多步推理、数学与逻辑推导、方案权衡、长上下文分析、疑难调试"),
)

#: 生效模式下允许透传给上游的模型 id 全集（= 三个档位）。
#:
#: 校验时用的是这个集合而不是「全部已启用模型」：门限只允许在这三档之间选，
#: 也**天然排除 `auto` 本身** —— 否则就等于把路由权又交回上游，
#: `auto-with-jev` 的语义被悄悄降级成 `auto`。
_ALLOWED_MODELS = frozenset(t.model for t in GATE_TIERS)

QUESTIONS: dict = {
    "capability": {
        "type": "choice",
        "instructions": (
            "这条请求主要需要哪一档模型能力？按答好它**真正需要的能力**判断，"
            "不要只看出现了什么关键词。任务简单就用最低档，不要保守抬高。"
        ),
        "criteria": {t.model: t.description for t in GATE_TIERS},
    },
    "needs_reasoning": {
        "type": "noul",
        "instructions": "答好这条请求是否需要显式的多步推理或思维链？",
    },
    "needs_long_context": {
        "type": "noul",
        "instructions": (
            "这条请求是否需要模型掌握很长的上下文（超过约 8000 字）才能答好？"
        ),
    },
}


@dataclass
class GateResult:
    """一次门限判断的结果。`active=False` 表示「不生效，请按原 auto 走」。"""

    #: 校验后的目标模型；仅在 `active` 为真时非空。
    model: str = ""
    #: 是否在**生效**模式且拿到了可用结果。
    active: bool = False
    #: 是否在影子模式（只记录）。
    shadow: bool = False
    #: Jev 选择的档位与其置信度（影子模式下也记录，供对比分析）。
    picked: str = ""
    confidence: float = 0.0
    reasoning: float = 0.0
    long_context: float = 0.0
    #: 门限自身耗时（毫秒）与失败原因（为空表示成功）。
    ms: int = 0
    fallback: str = ""
    #: 是否命中会话缓存（命中时不产生额外延迟）。
    cached: bool = False

    def summary(self) -> str:
        """压成一行放进 UsageLog.gate_note（列宽 255，够用）。"""
        if self.fallback:
            return f"fallback={self.fallback} ms={self.ms}"
        prefix = "shadow " if self.shadow else ""
        return (
            f"{prefix}gate={self.picked} conf={self.confidence:.2f} "
            f"reason={self.reasoning:.2f} long={self.long_context:.2f} "
            f"ms={self.ms}{' cached' if self.cached else ''}"
        )


#: Jev 只按输入 token 计费（$42/Btok），输出免费；一次门限约 1k token。
#: 基址取自 `settings.TYPESAFE_BASE_URL`（测试可用桩服务覆盖）。
_GATE_PATH = "/v1/systemone"

#: `choice` 落库前的截断长度，必须 <= UsageLog.gate_model 的列宽（VARCHAR(64)）。
#: 不截断的后果不是「难看」而是**整条用量日志丢失**：MySQL 严格模式下超宽 INSERT
#: 报 1406，被 _record_usage 的总 except 吞掉，连 credits/tokens 一起丢
#: —— 与 AGENTS.md 记的 cool_kind 事故是同一形状。
_MAX_PICKED = 64


def _timeout() -> float:
    """门限超时（秒）。官方 SDK 默认 10s 太宽松：门限加在请求关键路径上。

    超过这个时间宁可放弃门限走 auto —— 用户感知到的延迟比「是否选对模型」重要。

    注意 httpx 把单个 float 展开成 connect/read/write/pool **各**为这个值，
    所以最坏情况下（先连上再读取超时）会叠加到约 2 倍；Windows 上超时唤醒
    另有数百毫秒开销。默认值 2.5s 就是按这个叠加效应定的（见 config.py）。
    """
    return max(0.2, settings.ROUTER_GATE_TIMEOUT)


def _max_chars() -> int:
    return max(200, settings.ROUTER_GATE_MAX_CHARS)


def _cache_ttl() -> int:
    return max(0, settings.ROUTER_GATE_CACHE_TTL)


def api_key() -> str:
    """TypeSafe API Key（`TYPESAFE_API_KEY`）。为空时门限整体禁用。"""
    return settings.TYPESAFE_API_KEY


#: 复用的 HTTP 客户端。**必须复用**：`httpx.post(...)` 每次调用都会新建 Client，
#: 而新建 Client 会重新加载 SSL 上下文（本机实测 create_default_context ≈ 600-720ms），
#: 于是单次门限要 1.4-1.7s；复用同一个 Client 后稳定在 ~350ms —— 4 倍差距，
#: 直接决定门限是否可用（见文件头的「延迟」讨论）。
#: httpx.Client 是线程安全的，可被 run_in_threadpool 的多个工作线程共用。
_CLIENT: httpx.Client | None = None
_CLIENT_LOCK = threading.Lock()


def _client() -> httpx.Client:
    """惰性创建共享客户端（首次调用才建，避免拖慢 import、也避免没人用时也开连接）。"""
    global _CLIENT
    if _CLIENT is None:
        with _CLIENT_LOCK:
            if _CLIENT is None:
                _CLIENT = httpx.Client(
                    headers={"Content-Type": "application/json"},
                    # 连接池刻意开小：门限是旁路，不该抢占资源；超时后快速失败即可
                    limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
                    timeout=_timeout(),
                )
    return _CLIENT


def close_client() -> None:
    """关闭共享客户端（测试/优雅退出用）。"""
    global _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is not None:
            try:
                _CLIENT.close()
            except Exception:  # noqa: BLE001
                pass
            _CLIENT = None


def gate_mode(header_value: str | None = None) -> str:
    """决定这次请求走哪种门限模式：``off`` / ``shadow`` / ``on``。

    优先级：请求头 > 环境变量。**请求头只放宽不收紧** ——
    服务端关了（off）就是关了，客户端不能靠一个头把它打开。
    """
    env = settings.ROUTER_GATE
    if env in ("0", "off", "false", "no"):
        return "off"
    if env in ("on", "1", "true", "yes", "active"):
        return "on"
    # env 未识别 → 视为 shadow。请求头允许放宽到 on。
    hdr = (header_value or "").strip().lower()
    if hdr in ("on", "active", "1", "true"):
        return "on"
    return "shadow"


def extract_state(body: dict) -> str:
    """取最后一条 user 消息作为 state（截断到上限）。

    为什么不是整个请求体：Jev 的 64k 预算里 state 最多 32k，而门限要的是
    「这条请求要什么能力」，开头部分足够；同时这也把**出境数据量**压到最小。
    """
    msgs = body.get("messages")
    if not isinstance(msgs, list):
        return ""
    tail = msgs[-MAX_SCAN_MESSAGES:] if len(msgs) > MAX_SCAN_MESSAGES else msgs
    for m in reversed(tail):
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        content = m.get("content")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            # Anthropic / Responses 的 content 是分块列表，只取文本块
            parts = [
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and isinstance(b.get("text"), str)
            ]
            text = "\n".join(p for p in parts if p)
        if text.strip():
            return text[:_max_chars()]
    return ""


def _parse(res: dict) -> tuple[str, float, float, float]:
    """从响应里取出 (档位, 置信度, needs_reasoning, needs_long_context)。

    形状不对时抛异常，由调用方归入 fallback —— 不在这里静默兜底，
    否则「解析失败」和「Jev 真的这么判」会混在一起，影子数据就废了。
    """
    answers = res["answers"]
    cap = answers["capability"]
    picked = cap["choice"]
    conf = float(cap.get("confidence") or 0.0)
    reasoning = float(answers["needs_reasoning"]["noul"])
    long_ctx = float(answers["needs_long_context"]["noul"])
    return picked, conf, reasoning, long_ctx


# ---------------------------------------------------------------------------
# 会话级缓存：多轮对话只付一次门限延迟
# ---------------------------------------------------------------------------
#: 边界刻意做得很小：只缓存**本进程**、只对同一会话生效。
#: 换进程/重启即失效是可以接受的 —— 重新判断一次的成本是 ~1s，而不是正确性问题。
_CACHE: dict[str, tuple[float, GateResult]] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_MAX = 2048


def cache_key(body: dict) -> str:
    """门限缓存的键。复用号池的会话键，保证「同一个会话」在两处的粒度一致。"""
    from admin import pool

    return pool.session_key_for(body)


def _cache_get(key: str) -> GateResult | None:
    if not key:
        return None
    ttl = _cache_ttl()
    if ttl <= 0:
        return None
    now = time.time()
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if not hit:
            return None
        ts, res = hit
        if now - ts > ttl:
            _CACHE.pop(key, None)
            return None
        return res


def _cache_put(key: str, res: GateResult) -> None:
    if not key or res.fallback:
        return  # 失败的判断不缓存，下一轮值得重试
    if _cache_ttl() <= 0:
        return
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAX:
            # 简单粗暴地清掉最旧的一半：这是缓存不是状态，不追求精确 LRU
            for k in sorted(_CACHE, key=lambda k: _CACHE[k][0])[: _CACHE_MAX // 2]:
                _CACHE.pop(k, None)
        _CACHE[key] = (time.time(), res)


# ---------------------------------------------------------------------------
# 调用
# ---------------------------------------------------------------------------
def classify(body: dict, sticky_key: str = "", mode: str = "shadow") -> GateResult:
    """调 Jev 判断请求能力档位。**永不抛异常，永不阻塞超过 timeout。**

    `mode` 取 ``shadow``（默认，只记录）或 ``on``（生效）。调用方拿到结果后：
      * ``res.active``（仅 on 模式）→ 用 ``res.model``（已过白名单）替换解析出的模型；
      * ``res.shadow`` → 只把 ``res.summary()`` 记进日志，路由保持原样。
    失败时两者皆为假，调用方按原 ``auto`` 行为继续。
    """
    active = mode == "on"
    shadow = mode == "shadow"

    key = api_key()
    if not key:
        return GateResult(active=False, shadow=shadow, fallback="no_api_key")

    ck = sticky_key or cache_key(body)
    cached = _cache_get(ck)
    if cached is not None:
        # dataclasses.replace：只改本次请求的标记，保留原始判断结果。
        # `model` 必须在这里按本次的 active 重算：缓存里那份是**写入时**的模式算出来的
        # （shadow 模式下恒为空串），直接沿用会让「先 shadow 后 on」的请求拿到空模型名，
        # 而端点里 `"" in ("auto","")` 为真 → 静默退回 auto，门限再也不生效。
        return replace(
            cached, cached=True, active=active, shadow=shadow,
            model=cached.picked if active else "",
        )

    state = extract_state(body)
    if not state:
        return GateResult(active=False, shadow=shadow, fallback="empty_state")

    t0 = time.perf_counter()
    try:
        res = _client().post(
            f"{settings.TYPESAFE_BASE_URL}{_GATE_PATH}",
            headers={"Authorization": f"Bearer {key}"},
            json={"state": state, "model": DEFAULT_MODEL, "questions": QUESTIONS},
            # 超时逐请求传：客户端是长期复用的单例，而 settings 可能在运行期被改
            # （测试会改；.env 热改后重启也能生效）。把它烘进 Client 会读不到新值。
            timeout=_timeout(),
        )
        ms = int((time.perf_counter() - t0) * 1000)
        if res.status_code != 200:
            return GateResult(ms=ms, shadow=shadow, fallback=f"http_{res.status_code}")
        picked, conf, reasoning, long_ctx = _parse(res.json())
    except httpx.TimeoutException:
        return GateResult(ms=int((time.perf_counter() - t0) * 1000), shadow=shadow,
                          fallback="timeout")
    except Exception as exc:  # noqa: BLE001
        # 网络异常与响应形状异常都归到这里：**任何**非预期情况都不许抛出，
        # 否则会一路冒到端点变成 500（端点没有 try，admin/server.py 也没有全局 handler）。
        _logger.warning("门限调用/解析失败：%s", exc)
        return GateResult(ms=int((time.perf_counter() - t0) * 1000), shadow=shadow,
                          fallback="bad_response")

    # 白名单校验：Jev 可能返回候选清单外的值（幻觉 / prompt 注入）。
    # 注意校的是 `_ALLOWED_MODELS` 而不是全部已启用模型 —— 门限只认这三个档位。
    # `picked` 的类型必须先确认：它来自模型输出，可能是 null/数字/对象/数组，
    # 直接 `in frozenset` 会抛 TypeError: unhashable、直接切片会抛 TypeError
    # —— 两处都在上面那个 try 之外时就是实打实的 500。
    if not isinstance(picked, str) or picked not in _ALLOWED_MODELS:
        return GateResult(ms=ms, shadow=shadow, confidence=conf,
                          picked=str(picked)[:_MAX_PICKED],
                          fallback=f"not_allowed:{str(picked)[:32]}")

    out = GateResult(
        model=picked if active else "", active=active, shadow=shadow,
        picked=picked, confidence=conf,
        reasoning=reasoning, long_context=long_ctx, ms=ms,
    )
    _cache_put(ck, out)
    return out
