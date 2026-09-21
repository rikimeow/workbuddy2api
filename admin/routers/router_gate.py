"""Jev 路由门限的后台配置 API。

对应「设置」页的门限面板。可配项（存 `SystemSetting.router_gate_config`，
一条 JSON 承载全部）：

  * `mode`      —— off / shadow / on（开 on 前置校验「已配置 API Key」）
  * `api_key`   —— Jev API Key（或兼容服务的 Key）
  * `base_url`  —— API 基址（兼容服务可改）
  * `model`     —— 门限模型（jev-latest 或钉住的版本号）
  * `tiers`     —— 档位定义（模型 id + 能力描述）**判断质量的命门**
  * `questions` —— 三个问题的 instructions 文案

配置优先级：数据库（本 API 写入）> 环境变量 > 内置默认。

**服务端是唯一防线**：前端只是体验优化。写入时的校验（见 `_validate`）拦掉
「关掉护栏」的配置 —— 尤其是档位 id 设为 `auto`（会让门限把路由权交回上游）
与档位 id 重复（会让 criteria 静默覆盖）。
"""
import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from admin import router_gate as rg
from admin.db import get_db
from admin.models import ModelConfig, SystemSetting
from admin.security import require_admin

_logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/router-gate", tags=["router-gate"])

#: 所有可配项的字段名（PUT 时按这些取；未知字段忽略）
_FIELDS = ("mode", "api_key", "base_url", "model", "tiers", "questions")


class TierIn(BaseModel):
    model: str = ""
    description: str = ""


class QuestionsIn(BaseModel):
    #: key 由代码固定，这里只收 instructions
    capability: str | None = None
    needs_reasoning: str | None = None
    needs_long_context: str | None = None


class GateConfigIn(BaseModel):
    """PUT 入参。**全部可选**：只传部分字段时，其余保持 DB 里已有的值。

    约定：传 `""`（空串）= 清除该 DB 项（退回环境变量）；
    完全不传该字段 = 不动它。前端用这个区分「清空」与「不改」。
    """

    mode: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None
    tiers: list[TierIn] | None = None
    questions: QuestionsIn | None = None


# ---------------------------------------------------------------------------
# 读写 SystemSetting
# ---------------------------------------------------------------------------
def _read_cfg(db: Session) -> dict:
    row = db.query(SystemSetting).filter(SystemSetting.key == rg.CONFIG_KEY).first()
    if not row or not (row.value or "").strip():
        return {}
    try:
        obj = json.loads(row.value)
        return obj if isinstance(obj, dict) else {}
    except Exception as e:  # noqa: BLE001
        _logger.warning("门限配置无法解析，按空处理：%s", e)
        return {}


def _write_cfg(db: Session, cfg: dict) -> None:
    val = json.dumps(cfg, ensure_ascii=False)
    row = db.query(SystemSetting).filter(SystemSetting.key == rg.CONFIG_KEY).first()
    if row:
        row.value = val
    else:
        db.add(SystemSetting(key=rg.CONFIG_KEY, value=val))
    db.commit()
    # 本进程立即生效（清配置缓存 + 清会话判断缓存）
    rg.invalidate()


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------
def _mask(secret: str) -> str:
    """把密钥打码成 `apikey_2338****cade949`。**接口永不回显完整值。**"""
    s = (secret or "").strip()
    if not s:
        return ""
    if len(s) <= 12:
        return s[:2] + "****"
    return f"{s[:12]}****{s[-7:]}"


def _validate_tiers(tiers: list[TierIn]) -> list[dict]:
    """档位校验：**这是护栏所在，必须硬校验**。

    拒绝 `auto` 的理由：生效模式的模型白名单由档位派生，`auto` 一旦进白名单，
    门限就可能把 `auto` 原样发回上游 —— 等于把路由权交还上游、门限被架空。
    """
    if not (rg.MIN_TIERS <= len(tiers) <= rg.MAX_TIERS):
        raise HTTPException(
            status_code=400,
            detail=f"档位数量需在 {rg.MIN_TIERS}~{rg.MAX_TIERS} 之间，当前 {len(tiers)} 个")
    out: list[dict] = []
    seen: set[str] = set()
    for i, t in enumerate(tiers, 1):
        model = (t.model or "").strip()
        desc = (t.description or "").strip()
        if not model:
            raise HTTPException(status_code=400, detail=f"第 {i} 个档位缺少模型 id")
        if model.lower() == "auto":
            raise HTTPException(
                status_code=400,
                detail=f"第 {i} 个档位不能是 auto —— 那会让门限把路由权交回上游")
        if not desc:
            raise HTTPException(status_code=400, detail=f"档位「{model}」缺少能力描述"
                                                       f"（Jev 靠它区分档位，不能为空）")
        if model in seen:
            raise HTTPException(status_code=400, detail=f"档位「{model}」重复")
        seen.add(model)
        out.append({"model": model, "description": desc})
    return out


def _validate_questions(q: QuestionsIn) -> dict:
    """只允许改 instructions 文案；key 由代码固定，不接收新 key。"""
    out: dict = {}
    for k in rg.QUESTION_KEYS:
        v = getattr(q, k, None)
        if v is None:
            continue
        if not isinstance(v, str) or not v.strip():
            raise HTTPException(status_code=400, detail=f"问题「{k}」的文案不能为空")
        out[k] = {"instructions": v.strip()}
    return out


def _warnings(db: Session, cfg: dict) -> list[str]:
    """不拒绝但需要提示的问题（静默降级最坑，必须显式告知）。"""
    warns: list[str] = []
    tiers = rg.effective_tiers(cfg)
    enabled = {m.model_id for m in db.query(ModelConfig).filter(ModelConfig.enabled == 1).all()}
    if enabled:
        missing = [t.model for t in tiers if t.model not in enabled]
        if missing:
            warns.append(
                "档位 " + "、".join(f"「{m}」" for m in missing)
                + " 不在已启用的模型白名单里：门限选中它们时会被退回 auto（静默降级）")
    return warns


def _payload(db: Session, cfg: dict | None = None) -> dict:
    """组装 GET/PUT 的响应。"""
    cfg = _read_cfg(db) if cfg is None else cfg
    vals = {n: rg.resolve(n, cfg) for n in ("mode", "api_key", "base_url", "model")}
    key_val = vals["api_key"][0]
    tiers = rg.effective_tiers(cfg)
    tiers_source = "db" if isinstance(cfg.get("tiers"), list) else "default"
    questions = rg.effective_questions(cfg)

    return {
        "mode": vals["mode"][0],
        "mode_source": vals["mode"][1],
        #: key 只回掩码 + 是否已配置；永不回显完整值
        "api_key_configured": bool(key_val),
        "api_key_masked": _mask(key_val),
        "api_key_source": vals["api_key"][1],
        "base_url": vals["base_url"][0],
        "base_url_source": vals["base_url"][1],
        "model": vals["model"][0],
        "model_source": vals["model"][1],
        "tiers": [{"model": t.model, "description": t.description} for t in tiers],
        "tiers_source": tiers_source,
        "questions": {k: questions[k]["instructions"] for k in rg.QUESTION_KEYS},
        "limits": {"min_tiers": rg.MIN_TIERS, "max_tiers": rg.MAX_TIERS,
                   "question_keys": list(rg.QUESTION_KEYS)},
        #: 「能否开启 on」的前置条件：必须有 Key
        "can_enable": bool(key_val),
        "blocked_reason": "" if key_val else "尚未配置 API Key，无法开启生效模式",
        "warnings": _warnings(db, cfg),
    }


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------
@router.get("")
def get_config(_: bool = Depends(require_admin), db: Session = Depends(get_db)):
    """读取当前生效配置（含来源与可开性校验）。"""
    return _payload(db)


@router.put("")
def put_config(body: GateConfigIn,
               _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    """保存配置（部分更新）。服务端硬校验后再落库。"""
    cfg = _read_cfg(db)

    if body.mode is not None:
        mode = (body.mode or "").strip().lower()
        if mode and mode not in rg._MODE_VALID:
            raise HTTPException(status_code=400,
                                detail=f"模式必须是 {'/'.join(rg._MODE_VALID)} 之一")
        if mode == "on":
            # 开 on 的前置：必须有 Key（来自本次入参、DB 已有值、或环境变量）
            candidate = dict(cfg)
            if body.api_key is not None:
                candidate["api_key"] = (body.api_key or "").strip()
            elif not (cfg.get("api_key") or "").strip():
                # 本次没传 key 且 DB 也没有：看环境变量
                pass
            if not rg.resolve("api_key", candidate)[0]:
                raise HTTPException(
                    status_code=400, detail="请先配置 API Key，再开启生效模式（on）")
        cfg["mode"] = mode

    for name in ("api_key", "base_url", "model"):
        v = getattr(body, name, None)
        if v is not None:
            v = v.strip()
            if v:
                cfg[name] = v
            else:
                cfg.pop(name, None)  # 空串 = 清除，退回环境变量/默认

    if body.tiers is not None:
        cfg["tiers"] = _validate_tiers(body.tiers)

    if body.questions is not None:
        cfg["questions"] = _validate_questions(body.questions)

    _write_cfg(db, cfg)
    return _payload(db, cfg)


@router.post("/reset")
def reset_config(_: bool = Depends(require_admin), db: Session = Depends(get_db)):
    """清空全部后台配置，退回环境变量 / 内置默认。"""
    row = db.query(SystemSetting).filter(SystemSetting.key == rg.CONFIG_KEY).first()
    if row:
        db.delete(row)
        db.commit()
    rg.invalidate()
    return _payload(db, {})


@router.post("/test")
def test_config(_: bool = Depends(require_admin), db: Session = Depends(get_db)):
    """用当前生效凭据发一次真实的最小 Jev 调用，验证「配的 Key 到底能不能用」。

    失败也返回 200 + `ok=false`：这是「诊断结果」，不是本接口自身的错误。
    """
    key = rg.api_key()
    if not key:
        return {"ok": False, "detail": "尚未配置 API Key", "latency_ms": None}

    from admin import router_gate
    body = {"messages": [{"role": "user", "content": "你好"}]}
    res = router_gate.classify(body, mode="shadow")
    if res.fallback:
        return {"ok": False, "detail": f"调用失败：{res.fallback}",
                "latency_ms": res.ms, "base_url": rg.base_url(), "model": rg.gate_model()}
    return {
        "ok": True, "detail": "连接正常",
        "latency_ms": res.ms,
        "base_url": rg.base_url(), "model": rg.gate_model(),
        "sample": {"picked": res.picked, "confidence": round(res.confidence, 3)},
    }
