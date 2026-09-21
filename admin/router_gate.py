"""Jev 路由门限：接管 `auto`，用 TypeSafe Jev 给请求打结构化特征。

背景
----
`auto` 的语义是「让上游智能路由挑模型」，本地看不到它会落到哪个厂商模型。
本模块**可选地**接管 `auto`：本地先用 Jev 判断这条请求需要什么能力，再由
`GATE_TIERS` 把判断映射成模型档位。客户端不用记新模型名 —— 用 `auto` 就走路限。

三种模式（`ADMIN_ROUTER_GATE`，或请求头 `X-Route-Mode`）：
  off    = 完全不调用，auto 走原有逻辑，零延迟（默认，等于没装这个功能）
  shadow = 只记录不生效：auto 仍按原语义路由，判断写进 UsageLog 供对比
  on     = 按档位替换模型（越界/失败一律退回 auto）

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
* **shadow 是上线前的必经阶段。** 门限的返回值会决定真实路由，必须先拿真实
  流量验证判断质量（尤其置信度低的样本），再切 on。
* **fail-open 是硬要求。** 超时 / 429 / 5xx / JSON 异常 / 形状不对 —— 一律返回
  `active=False`，请求退回原 `auto` 行为。门限绝不能让一次请求失败。
* **模型名必须白名单校验。** 即使 prompt 里的候选清单被注入，返回值也只能是
  白名单内的 id —— 否则等于给「生图模型混进聊天池」这类事故开了新入口。
* **只发最后一条 user 消息。** 请求体最多 4k 字符，不整段会话出境；
  state 越小 Jev 也越准。
"""
from __future__ import annotations

import json
import logging
import os
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

#: 超长 state 被截断时插入的省略标记。
_TRUNCATION_MARK = "\n\n...(中间省略 {n} 字符)...\n\n"

# ---------------------------------------------------------------------------
# 模态检测：三种协议里「图片 / 视频 / 音频」块的 type 名
# ---------------------------------------------------------------------------
# OpenAI 用 `image_url`；Anthropic 用 `image`；Responses 用 `input_image`。
_IMAGE_TYPES = frozenset({"image_url", "image", "input_image"})
_VIDEO_TYPES = frozenset({"video_url", "video", "input_video"})
_AUDIO_TYPES = frozenset({"input_audio", "audio", "audio_url"})

#: token_bucket 分档阈值。**按字符数**（不是估算 token）——理由：
#:   * 口径可预测、可复算，不引入 tokenizer 依赖；
#:   * 与 Jev 文档里长上下文问题的口径一致（官方 Noul 例子写的是「约 8000 字以上」）；
#:   * 估算 token（字符/2）在中文下会系统性低估（1 汉字≈1 token 而非 0.5），
#:     导致「40K 字符的长文档」只算 medium，而它显然该是 long。
#: 真正的 token 数由上游回传、记账时用精确值，两套口径互不干扰。
_TOKEN_MEDIUM_MIN = 8000     # 字符
_TOKEN_LONG_MIN = 32000      # 字符

#: 夜间免费时段（本地时间，含起点不含终点）。hy4-preview 夜间免费就是靠它。
NIGHT_START_HOUR = 23
NIGHT_END_HOUR = 8

#: `recent_output` 的字符上限：只在「多轮迭代」时给 Jev 看被改产出的开头。
#: 刻意很小 —— 目的是让 Jev 知道「在改什么东西」（类型/体量），不是让它读全文；
#: 全文已由 request 之外的信息承载，而这里多发一个字都是**出境数据 + token 成本**。
RECENT_OUTPUT_CHARS = 500


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

#: 生效模式下允许透传给上游的模型 id 全集（= 档位的模型 id）。
#:
#: 校验时用的是这个集合而不是「全部已启用模型」：门限只允许在档位之间选，
#: 也**天然排除 `auto` 本身** —— 否则就等于把路由权又交回上游，门限被悄悄架空。
#: 档位可配后改用 `allowed_models()` 动态计算；这里保留常量作为**默认值**的固化视图。
_DEFAULT_ALLOWED_MODELS = frozenset(t.model for t in GATE_TIERS)

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

#: question key 与类型是**解析契约**，不可配置：
#:   `_parse()` 按名字读 answers[...]，按类型取字段（choice 读 .choice/.confidence，
#:   noul 读 .noul）。改名或改类型 → 解析失败 → 全部 fallback。
#: 后台只能改 instructions 文案；下面的常量同时供后台 API 做校验。
QUESTION_KEYS: tuple[str, ...] = ("capability", "needs_reasoning", "needs_long_context")
QUESTION_TYPES: dict[str, str] = {k: v["type"] for k, v in QUESTIONS.items()}

#: 档位数量边界。
#:
#: 注意**上限不是 Jev 的限制**：官方 Choice 支持最多 255 个选项，且明确建议
#: 「给全量列表而不是缩略版」。这里限 8 是**我们这边的工程取舍**：
#:   * 每个档位 = 一个上游模型，有意义的下游模型本就没几个；
#:   * 档位越多，Jev 的选择越分散、置信度越低（3 选 1 比 10 选 1 可靠得多）；
#:   * 每档的描述都进 criteria，会按输入 token 计费。
#: 下限 2：1 个档位没有选择余地，等于没门限。
MIN_TIERS = 2
MAX_TIERS = 8

#: 合法的门限模式（供后台 API 校验，避免校验逻辑与 `gate_mode` 各写一份而漂移）。
_MODE_VALID = ("off", "shadow", "on")

# ---------------------------------------------------------------------------
# 运行时配置（后台可改，DB > 环境变量 > 默认值）
# ---------------------------------------------------------------------------
#: SystemSetting 里的键名：一个 JSON 承载全部后台可配项。
#:
#: {
#:   "mode": "off"|"shadow"|"on",
#:   "api_key": "...", "base_url": "https://...", "model": "jev-latest",
#:   "tiers": [{"model": "fast-model", "description": "..."}, ...],
#:   "questions": {"capability": {"instructions": "..."}, ...}
#: }
#:
#: 字段全部可选；缺省 = 用下一级来源。空串视为「清除该 DB 项」，退回下一级。
CONFIG_KEY = "router_gate_config"

#: DB 配置缓存时长（秒）。**只影响「别的进程改了值」的收敛延迟** ——
#: 本进程保存时主动调 `invalidate()`，立即生效。单进程部署下基本用不到。
_CFG_TTL = 30.0
_cfg: dict | None = None
_cfg_at = 0.0
_cfg_lock = threading.Lock()


def _load_cfg_raw(db=None) -> dict:
    """从 SystemSetting 读原始配置 dict。失败/缺失/坏 JSON 一律返回 {}（退回下一级来源）。

    **绝不抛异常**：配置读取在请求关键路径上，读不到只该退回默认值。
    """
    from admin.db import SessionLocal
    from admin.models import SystemSetting

    own = db is None
    if own:
        db = SessionLocal()
    try:
        row = db.query(SystemSetting).filter(SystemSetting.key == CONFIG_KEY).first()
        if not row or not (row.value or "").strip():
            return {}
        obj = json.loads(row.value)
        return obj if isinstance(obj, dict) else {}
    except Exception as e:  # noqa: BLE001
        _logger.warning("router_gate 配置读取失败，按默认值处理：%s", e)
        return {}
    finally:
        if own:
            db.close()


def _cfg_snapshot(db=None) -> dict:
    """带 TTL 缓存的配置快照。"""
    global _cfg, _cfg_at
    now = time.time()
    with _cfg_lock:
        if _cfg is not None and (now - _cfg_at) < _CFG_TTL:
            return _cfg
    raw = _load_cfg_raw(db)
    with _cfg_lock:
        _cfg = raw
        _cfg_at = time.time()
        return _cfg


def invalidate() -> None:
    """清配置缓存**和会话判断缓存**。

    判断缓存必须一起清：档位定义改了以后，缓存里的 `GateResult` 引用的还是旧档位，
    继续用会让「后台改了没生效」，而且新旧档位混着用更糟。
    """
    global _cfg, _cfg_at
    with _cfg_lock:
        _cfg = None
        _cfg_at = 0.0
    with _CACHE_LOCK:
        _CACHE.clear()


def effective_tiers(cfg: dict | None = None) -> tuple[Tier, ...]:
    """生效的档位定义：DB 配置 > 内置默认。

    只做**形状**过滤（非空 str），不在这里做策略校验 —— 策略校验属于写入时的
    后台 API（要给出明确报错），读取路径只求「能用默认就用默认，坏数据不崩」。
    """
    cfg = cfg if cfg is not None else _cfg_snapshot()
    raw = cfg.get("tiers")
    if not isinstance(raw, list) or not (MIN_TIERS <= len(raw) <= MAX_TIERS):
        return GATE_TIERS
    out: list[Tier] = []
    for item in raw:
        if not isinstance(item, dict):
            return GATE_TIERS
        model = item.get("model")
        desc = item.get("description")
        if not isinstance(model, str) or not model.strip():
            return GATE_TIERS
        if not isinstance(desc, str) or not desc.strip():
            return GATE_TIERS
        out.append(Tier(model.strip(), desc.strip()))
    # 重复档位 id 会让 criteria 静默覆盖、白名单丢档 → 视为坏配置，退回默认
    if len({t.model for t in out}) != len(out):
        return GATE_TIERS
    return tuple(out)


def effective_questions(cfg: dict | None = None) -> dict:
    """生效的提问定义：档位可配 → `capability.criteria` 必须跟着重建。

    key 与 type 固定（解析契约），只有 instructions 文案可被覆盖。
    """
    cfg = cfg if cfg is not None else _cfg_snapshot()
    tiers = effective_tiers(cfg)
    custom = cfg.get("questions") if isinstance(cfg.get("questions"), dict) else {}

    out: dict = {
        "capability": {
            "type": "choice",
            "instructions": QUESTIONS["capability"]["instructions"],
            "criteria": {t.model: t.description for t in tiers},
        },
    }
    for k in ("capability", "needs_reasoning", "needs_long_context"):
        spec = custom.get(k)
        if isinstance(spec, dict):
            text = spec.get("instructions")
            if isinstance(text, str) and text.strip():
                out.setdefault(k, {"type": QUESTION_TYPES[k]})["instructions"] = text.strip()
    for k in ("needs_reasoning", "needs_long_context"):
        out.setdefault(k, {"type": QUESTION_TYPES[k],
                           "instructions": QUESTIONS[k]["instructions"]})
    return out


def allowed_models(cfg: dict | None = None) -> frozenset[str]:
    """生效的模型白名单（由档位派生）。

    替代原先的模块级 `_ALLOWED_MODELS`：档位可配后必须动态计算。
    仍**天然排除 `auto`** —— 只要写入时拒绝了 auto（后台 API 会拒），
    这里就永远不含它，路由权不会被交回上游。
    """
    return frozenset(t.model for t in effective_tiers(cfg))


#: 各配置项的**代码内置默认值**。用于 `resolve()` 判断「当前值到底来自环境变量
#: 还是内置默认」——光比 `settings.X` 分不出来，因为 config.py 自己就带默认值。
_DEFAULTS: dict[str, str] = {
    "api_key": "",
    "base_url": "https://api.typesafe.ai",
    "mode": "off",
    "model": DEFAULT_MODEL,
}


def resolve(name: str, cfg: dict | None = None) -> tuple[str, str]:
    """解析一项运行时配置，返回 (值, 来源)。

    来源：``db`` > ``env`` > ``default``。空串一律视为「未配置」而跳过，
    这样后台把 key 清空就能干净地退回环境变量。

    「来源」是给后台 UI 显示用的（用户要能看出「我到底在用后台值还是环境变量」），
    所以 `env` 与 `default` 要分准：与内置默认值相同即报 `default`。
    """
    cfg = cfg if cfg is not None else _cfg_snapshot()
    raw = cfg.get(name)
    if isinstance(raw, str) and raw.strip():
        return raw.strip(), "db"
    env_map = {
        "api_key": settings.TYPESAFE_API_KEY,
        "base_url": settings.TYPESAFE_BASE_URL,
        "mode": settings.ROUTER_GATE,
        "model": DEFAULT_MODEL,
    }
    env_val = (env_map.get(name) or "").strip()
    default = _DEFAULTS.get(name, "")
    if env_val and env_val != default:
        return env_val, "env"
    return (env_val or default), "default"


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


def _min_conf() -> float:
    """置信度下限（0 = 不设阈值）。低于它时不采用 Jev 的档位、退回 auto。"""
    try:
        return max(0.0, min(1.0, float(settings.ROUTER_GATE_MIN_CONF)))
    except (TypeError, ValueError):
        return 0.5


def api_key() -> str:
    """生效的 Jev API Key：后台配置 > `TYPESAFE_API_KEY`。为空时门限整体禁用。"""
    return resolve("api_key")[0]


def base_url() -> str:
    """生效的 API 基址：后台配置 > `TYPESAFE_BASE_URL`（为兼容服务留的口子）。"""
    return resolve("base_url")[0]


def gate_model() -> str:
    """生效的门限模型名：后台配置 > `jev-latest`。"""
    return resolve("model")[0] or DEFAULT_MODEL


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

    优先级：请求头 > 服务端配置（后台 > 环境变量）。服务端 ``off`` 是锁死的 ——
    客户端不能靠头打开。非 off 时，请求头 ``X-Route-Mode: off`` 可**逐请求关闭**
    门限（逃生门：服务端是 on 时，个别客户端仍可要回纯上游 auto）；
    ``on`` 逐请求强制生效。

    **判定分支结构刻意与改造前逐字一致**，只把「env 值」换成 `resolve("mode")`
    —— 现有 12 项 gate_mode 测试就是这条不变式的回归网。
    """
    env = resolve("mode")[0].strip().lower()
    if env in ("0", "off", "false", "no"):
        return "off"
    env_mode = "on" if env in ("on", "1", "true", "yes", "active") else "shadow"
    hdr = (header_value or "").strip().lower()
    if hdr in ("off", "0", "false", "no"):
        return "off"
    if hdr in ("on", "active", "1", "true"):
        return "on"
    return env_mode


def truncate_head_tail(text: str, limit: int) -> str:
    """超长文本取**首尾两段**，中间用省略标记连接；不超长则原样返回。

    为什么不是「只取开头」（早期实现）：这是 **Lost in the Middle** 直接对应的坑 ——
    Liu et al. 2024（`Lost in the Middle: How Language Models Use Long Contexts`，
    TACL，该现象在 6 个模型家族上复现）发现：**相关信息位于上下文开头或结尾时
    准确率最高，位于中间时显著下降（>30%）**。

    而门限这个具体任务里，「最后一条 user 消息」的**尾部**往往正是用户真正的诉求
    （长背景 + 末尾提问是很常见的写法）。只取开头等于**系统性地丢掉提问本身**，
    只剩背景 —— 这会让 Jev 判不出真实意图。首尾都留才贴合那条 U 型曲线。

    中间加省略标记而不是直接拼接：不加的话两段会被读成一句话中间突然跳变，
    反而可能误判。标记明确告诉模型「这里断开了」。
    """
    text = text or ""
    if limit <= 0 or len(text) <= limit:
        return text
    # 省略标记本身也占预算。关键：标记里的 `{n}` 会被替换成真实省略字符数，
    # 而数字位数会影响标记长度 —— 直接按**最大可能位数**（即 len(text) 的位数）
    # 预留预算，就能保证最终长度一定不超过 limit（omitted <= len(text) 恒成立）。
    mark_len = len(_TRUNCATION_MARK.format(n=len(text)))
    budget = max(0, limit - mark_len)
    head = budget // 2
    tail = budget - head
    omitted = len(text) - head - tail
    return (text[:head]
            + _TRUNCATION_MARK.format(n=omitted)
            + (text[-tail:] if tail else ""))


def _now_hhmm() -> str:
    """本地时间 HH:MM（供 state 里的 ``time`` 字段）。"""
    return time.strftime("%H:%M")


def _is_night(now_hour: int | None = None) -> bool:
    """是否在夜间免费时段（默认 23:00~08:00，跨零点）。

    为什么用代码判而不是问 Jev：这是**确定性事实**，Jev 既不知道现在几点、
    也不该被要求去算。而 hy4-preview 的「夜间免费」正需要这个信号才能被利用。

    时段边界刻意做成常量、可调：上游的「夜间」具体口径未经证实（标签只说
    「夜间免费」，没说几点到几点），所以这里用最常见的 23:00~08:00，
    并允许通过环境变量覆盖。
    """
    h = time.localtime().tm_hour if now_hour is None else now_hour
    start, end = _night_range()
    if start <= end:                      # 不跨零点
        return start <= h < end
    return h >= start or h < end          # 跨零点（默认情形：23 点后或 8 点前）


def _night_range() -> tuple[int, int]:
    """夜间时段的 (起, 止) 小时数。可用环境变量覆盖。"""
    try:
        start = int(os.getenv("ADMIN_ROUTER_GATE_NIGHT_START", str(NIGHT_START_HOUR)))
        end = int(os.getenv("ADMIN_ROUTER_GATE_NIGHT_END", str(NIGHT_END_HOUR)))
        return max(0, min(23, start)), max(0, min(24, end))
    except (TypeError, ValueError):
        return NIGHT_START_HOUR, NIGHT_END_HOUR


def _iter_text_chars(body: dict):
    """遍历请求里所有「文本字符」，同时覆盖两种协议的容器字段。

    踩过的坑：`messages`（OpenAI / Anthropic chat）与 `input`（Responses 协议）
    是两个不同的顶层字段。早期只扫 `messages`，导致 **Responses 请求恒定被判
    `token_bucket=short`** —— 长文档走 Responses 路径时系统性漏判 medium/long。
    而同一文件里的 `_has_modal` 两个字段都扫，两边不一致就是遗漏。

    产出 (文本片段) 生成器，由调用方决定是数长度还是拼接。
    """
    for container in ("messages", "input"):
        node = body.get(container)
        if not isinstance(node, list):
            continue
        for m in node:
            if not isinstance(m, dict):
                continue
            content = m.get("content")
            if isinstance(content, str):
                yield content
            elif isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and isinstance(b.get("text"), str):
                        yield b["text"]


def _token_bucket(body: dict) -> str:
    """按**字符数**分档：``short`` / ``medium`` / ``long``。

    阈值：short < 8000 字符、medium 8K~32K、long >= 32K（见 `_TOKEN_MEDIUM_MIN`
    / `_TOKEN_LONG_MIN` 的说明：为什么按字符而不是估算 token）。

    统计范围是**整个会话的消息**（不只最后一条）：长文档任务的特征是前面挂了
    大量材料，只看最后一句会把它误判成 short。

    只数文本块，忽略图片/音频（base64 体积会把字数撑爆，与「文档有多长」无关）。
    同时覆盖 `messages` 与 Responses 的 `input`（见 `_iter_text_chars`）。
    """
    total_chars = sum(len(t) for t in _iter_text_chars(body))
    if total_chars >= _TOKEN_LONG_MIN:
        return "long"
    if total_chars >= _TOKEN_MEDIUM_MIN:
        return "medium"
    return "short"


def _has_modal(body: dict, types: frozenset) -> bool:
    """请求体里是否含指定模态的块（只做结构判定，不解码内容）。

    递归扫 `messages` 与 Responses 的 `input`，深度限制防止畸形请求炸栈。
    """
    def _scan(node, depth=0) -> bool:
        if depth > 4:
            return False
        if isinstance(node, dict):
            if node.get("type") in types:
                return True
            return any(_scan(v, depth + 1) for v in node.values())
        if isinstance(node, list):
            return any(_scan(v, depth + 1) for v in node)
        return False

    return _scan(body.get("messages")) or _scan(body.get("input"))


def extract_state(body: dict) -> dict:
    """构造结构化 state（dict）：请求文本 + 代码算出的旁路字段。

    为什么是 dict：档位 criteria 里引用了 `token_bucket` / `has_image` /
    `has_video` / `has_audio` / `is_night` 这些**代码能算、Jev 不该猜**的字段
    （让 Jev 去数 token 或看时间既不准也没必要）。Jev 的 `state` 接受任意 JSON。

    字段（缺省一律给明确的 false/值，不留 undefined，避免 criteria 里的反引号
    引用指向空）：
      * ``request``     —— 最后一条 user 消息，超长时首尾各留一半
      * ``turn_index``  —— 这是会话里的第几轮用户发言（1 = 首轮）
      * ``is_refinement``—— 是否在「多轮迭代同一份产出」（见 `_turn_info`）
      * ``recent_output``—— 上一条 assistant 回复的开头（≤500 字符），
        仅在 `is_refinement` 为真时有值；让 Jev 能看到「在改什么东西」
      * ``time``        —— 本地时间 HH:MM
      * ``is_night``    —— 是否在夜间免费时段（默认 23:00~08:00）
      * ``token_bucket``—— short / medium / long（按字符数分档）
      * ``has_image`` / ``has_video`` / ``has_audio`` —— 模态检测

    只发最后一条 user 消息仍是为把**出境数据量**压到最小。
    """
    text = _last_user_text(body)
    turn_index, is_refinement = _turn_info(body)
    out = {
        "request": truncate_head_tail(text, _max_chars()) if text else "",
        "turn_index": turn_index,
        "is_refinement": is_refinement,
        "recent_output": _recent_output(body) if is_refinement else "",
        "time": _now_hhmm(),
        "is_night": _is_night(),
        "token_bucket": _token_bucket(body),
        "has_image": _has_modal(body, _IMAGE_TYPES),
        "has_video": _has_modal(body, _VIDEO_TYPES),
        "has_audio": _has_modal(body, _AUDIO_TYPES),
    }
    return out


def _turn_info(body: dict) -> tuple[int, bool]:
    """返回 (turn_index, is_refinement)。

    `turn_index` = 会话里 user 发言的条数（本次是第几条），1 表示首轮。

    `is_refinement` = 是否属于「多轮迭代同一份产出」——判据是**既有前文 assistant
    回复、且本次请求明显是短指令**。为什么要这个信号：实测（本机 WorkBuddy 使用
    调查）「投放数据分析」这类任务「一轮要改十几次」（删模块、换口径、加交叉分析、
    同比），而门限的档位缓存按会话首句定调 15 分钟 —— 第 2~15 轮的修改请求会被
    首轮档位钉住。把「这是第 N 轮、且像在改东西」告诉 Jev，它才有机会判出
    「这个会话本身是个复杂任务」。

    判据刻意保守（两个条件同时满足才为真）：
      * 之前至少有 1 条 assistant 消息（说明已经产出过东西）
      * 本次 request 较短（< 200 字符）——修改指令通常很短；长请求更像新任务
    """
    containers = [body.get("messages"), body.get("input")]
    user_n = 0
    has_prior_assistant = False
    for node in containers:
        if not isinstance(node, list):
            continue
        for m in node:
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            if role == "user":
                user_n += 1
            elif role == "assistant":
                has_prior_assistant = True
    turn_index = max(1, user_n)
    short_req = len(_last_user_text(body)) < 200
    return turn_index, bool(turn_index > 1 and has_prior_assistant and short_req)


def _recent_output(body: dict) -> str:
    """取**上一条 assistant 回复**的开头（≤ `RECENT_OUTPUT_CHARS` 字符）。

    为什么需要它：只有 `is_refinement=true` 这个标记时，Jev 看得到「用户在改东西」
    却看不到「改的是什么」—— 实测那样反而让它更困惑（置信度从 0.57 掉到 0.39），
    因为它在遵守一条没有信息支撑的指令。给出被改产出的开头，它才能判断
    「这个产出本身是复杂报告还是简单文本」，从而决定该不该升档。

    只取开头（不是首尾）：目的是判断产出的**类型与体量**，开头足够；而且这个字段
    是纯增量成本（出境数据 + Jev 输入 token），越小越好。
    """
    containers = [body.get("messages"), body.get("input")]
    for node in containers:
        if not isinstance(node, list):
            continue
        for m in reversed(node):
            if not isinstance(m, dict) or m.get("role") != "assistant":
                continue
            content = m.get("content")
            text = ""
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                parts = [
                    b.get("text", "")
                    for b in content
                    if isinstance(b, dict) and isinstance(b.get("text"), str)
                ]
                text = "\n".join(p for p in parts if p)
            if text.strip():
                return text[:RECENT_OUTPUT_CHARS]
    return ""


def _last_user_text(body: dict) -> str:
    """取最后一条 user 消息的纯文本（找不到返回空串）。

    只扫描最后 MAX_SCAN_MESSAGES 条：再往前的历史对「这条请求要什么能力」影响很小，
    而扫描窗口越小越省事（这是原实现的取舍，保留）。

    同时覆盖 `messages` 与 Responses 的 `input` —— 早期只扫 `messages`，
    导致 Responses 请求的 `request` 恒为空串、门限直接 `empty_state` 跳过
    （见 `_iter_text_chars` 的说明）。
    """
    for container in ("messages", "input"):
        msgs = body.get(container)
        if not isinstance(msgs, list):
            continue
        tail = msgs[-MAX_SCAN_MESSAGES:] if len(msgs) > MAX_SCAN_MESSAGES else msgs
        for m in reversed(tail):
            if not isinstance(m, dict):
                continue
            # Responses 的 input 项可能没有 role 字段；只有显式声明了非 user
            # 的才跳过（避免把 Responses 的输入项整体漏掉）。
            role = m.get("role")
            if role is not None and role != "user":
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
                return text
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
    # state 现在是 dict（含 time/token_bucket/模态等旁路字段），所以要看
    # `request` 是否真的有内容 —— 空请求没必要花一次 Jev 调用。
    if not state.get("request"):
        return GateResult(active=False, shadow=shadow, fallback="empty_state")

    # 一次取配置快照，保证同一次判断里的档位/提问/白名单来自**同一份配置**
    # （分次取的话，中途被后台改配置会出现「按新档位提问、按旧白名单校验」的错配）
    cfg = _cfg_snapshot()
    questions = effective_questions(cfg)
    whitelist = allowed_models(cfg)

    t0 = time.perf_counter()
    try:
        res = _client().post(
            f"{base_url()}{_GATE_PATH}",
            headers={"Authorization": f"Bearer {key}"},
            json={"state": state, "model": gate_model(), "questions": questions},
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
    # 注意校的是**生效档位集合**而不是全部已启用模型 —— 门限只认档位；
    # 档位可配后由 `allowed_models(cfg)` 动态给出（写入时已拒绝 auto）。
    # `picked` 的类型必须先确认：它来自模型输出，可能是 null/数字/对象/数组，
    # 直接 `in frozenset` 会抛 TypeError: unhashable、直接切片会抛 TypeError
    # —— 两处都在上面那个 try 之外时就是实打实的 500。
    if not isinstance(picked, str) or picked not in whitelist:
        return GateResult(ms=ms, shadow=shadow, confidence=conf,
                          picked=str(picked)[:_MAX_PICKED],
                          fallback=f"not_allowed:{str(picked)[:32]}")

    # 置信度下限：低置信度说明 Jev 在档位间摇摆，采用它等于**赌**。
    # 这里退回 "auto" 交给上游原逻辑 —— `picked` 仍记录（便于事后看它原本想选什么），
    # 但 `active` 保持 False，所以调用方不会替换模型。
    # 为什么必须在这一层拦而不是调用方：**缓存**。低置信度的判断若不拦，会被
    # `_cache_put` 存下来、在 TTL 内被后续每一轮复用（实测 conf=0.25 被路由到最贵档）。
    min_conf = _min_conf()
    if min_conf > 0 and conf < min_conf:
        _logger.info("门限置信度 %.2f < %.2f，退回 auto（Jev 本想选 %s）",
                     conf, min_conf, picked)
        # 不写缓存：低置信度往往说明**这次**的 state 不好判断，下一轮值得重新问，
        # 而不是把这个悬而未决的判断钉住整个会话窗口。
        return GateResult(ms=ms, shadow=shadow, confidence=conf, picked=picked,
                          reasoning=reasoning, long_context=long_ctx,
                          fallback=f"low_confidence:{conf:.2f}<{min_conf:g}")

    out = GateResult(
        model=picked if active else "", active=active, shadow=shadow,
        picked=picked, confidence=conf,
        reasoning=reasoning, long_context=long_ctx, ms=ms,
    )
    _cache_put(ck, out)
    return out
