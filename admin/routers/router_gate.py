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
from admin.config import settings
from admin.db import get_db
from admin.models import ModelConfig, SystemSetting
from admin.security import require_admin

_logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/router-gate", tags=["router-gate"])

#: 所有可配项的字段名（PUT 时按这些取；未知字段忽略）
_FIELDS = ("mode", "api_key", "base_url", "model", "tiers", "questions",
           "providers", "active_provider")


class TierIn(BaseModel):
    model: str = ""
    description: str = ""


class ProviderIn(BaseModel):
    """一个门限服务分组。

    `api_key` 的约定与顶层同名：**留空 = 保留原 Key**（前端只拿得到掩码，
    回传掩码会被服务端当空处理，见 `rg._coerce_provider`）。
    """

    id: str = ""
    name: str = ""
    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None
    enabled: bool | None = None


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
    #: 分组列表：**整块替换**（与 tiers 同语义）。不传 = 不动分组结构。
    providers: list[ProviderIn] | None = None
    #: 当前选中的分组 id。空串 = 清除（退回第一个分组）。
    active_provider: str | None = None


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


def _validate_providers(providers: list[ProviderIn], cfg: dict) -> list[dict]:
    """分组校验。**这是新的护栏所在**（与 `_validate_tiers` 同级）。

    硬约束与理由：
      * 数量 1~`MAX_PROVIDERS` —— 0 个分组会让门限彻底没凭据可用；
      * id 非空且唯一 —— 重复 id 会让「选中哪个」变成不确定，且 Key 合并会串组；
      * id 不用 `auto` 之类保留名没意义，但**禁止改官方组的 id**不需要额外规则：
        官方组只是「未配分组时的默认单组」，一旦显式配置，id 就是数据；
      * `base_url` 必须非空 —— 空基址会拼出相对 URL，httpx 直接抛异常；
      * 同 id 的 Key 留空 = **继承旧 Key**（前端拿不回明文，不继承就会一保存就抹掉）；
        但若旧记录也没有该 id，则视为「这个组没配 Key」，允许保存（只是不可用）。
    """
    if not (1 <= len(providers) <= rg.MAX_PROVIDERS):
        raise HTTPException(
            status_code=400,
            detail=f"分组数量需在 1~{rg.MAX_PROVIDERS} 之间，当前 {len(providers)} 个")

    # 旧记录索引：只收录**确实存在 DB 里**的 Key，用于「留空/掩码 = 不改动」。
    # 不能用 `rg.effective_providers(cfg)`：没有 providers 配置时它会返回
    # 那个「Key 来自环境变量」的遗留单组，于是继承就等于把 env 固化进 DB
    # —— 之后改 `TYPESAFE_API_KEY` 不生效（DB 值优先），而用户看到的是「改了没用」。
    # 官方组靠**读取期**继承 env（`_coerce_provider`），所以这里不收 env 值也不影响可用性。
    old: dict[str, str] = {}
    raw_old = cfg.get("providers")
    if isinstance(raw_old, list):
        for item in raw_old:
            if isinstance(item, dict):
                oid = str(item.get("id") or "").strip()
                okey = str(item.get("api_key") or "").strip()
                if oid and okey and "****" not in okey:
                    old.setdefault(oid, okey)
    out: list[dict] = []
    seen: set[str] = set()
    for i, p in enumerate(providers, 1):
        pid = (p.id or "").strip()
        if not pid:
            raise HTTPException(status_code=400, detail=f"第 {i} 个分组缺少 id")
        if pid in seen:
            raise HTTPException(status_code=400, detail=f"分组 id「{pid}」重复")
        seen.add(pid)

        url = rg.normalize_base_url(p.base_url or "")
        if not url:
            raise HTTPException(status_code=400,
                                detail=f"分组「{pid}」缺少 base_url（API 基址不能为空）")

        key = (p.api_key or "").strip()
        if "****" in key:
            # 前端把掩码原样回传：当「不改」处理，而不是存一串假 Key 进去
            key = ""
        if not key and pid in old:
            # 同 id 继承**上一轮显式存进 DB 的** Key（前端拿不回明文，不继承会一保存就抹掉）
            key = old[pid]

        out.append({
            "id": pid,
            "name": (p.name or "").strip() or pid,
            "api_key": key,
            "base_url": url,
            "model": (p.model or "").strip() or rg.DEFAULT_MODEL,
            "enabled": True if p.enabled is None else bool(p.enabled),
        })
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

    # 分组可用性：挑出「有了等于没有」的组，否则用户以为配了备份、其实兜底不到。
    provs = rg.effective_providers(cfg)
    dead = [p.id for p in provs if not p.usable]
    if dead:
        warns.append(
            "分组 " + "、".join(f"「{i}」" for i in dead)
            + " 缺 API Key，不参与自动兜底（选中它时门限会整体退回 auto）")
    if len(rg.failover_candidates(cfg)) < 2 and not dead:
        warns.append("只有 1 个可用分组：该组失败时没有备份可兜底（门限会退回 auto）")
    return warns


def _payload(db: Session, cfg: dict | None = None) -> dict:
    """组装 GET/PUT 的响应。

    `api_key` / `base_url` / `model` 三个顶层字段现在返回的是**当前生效分组**的值
    （向后兼容：没有配分组时它就是原来那套扁平配置）。同时新增 `providers`
    列表供后台做分组管理。Key 一律只回掩码。
    """
    cfg = _read_cfg(db) if cfg is None else cfg
    vals = {n: rg.resolve(n, cfg) for n in ("mode",)}
    tiers = rg.effective_tiers(cfg)
    tiers_source = "db" if isinstance(cfg.get("tiers"), list) else "default"
    questions = rg.effective_questions(cfg)

    provs = rg.effective_providers(cfg)
    act = rg.active_provider(cfg)
    provs_source = "db" if isinstance(cfg.get("providers"), list) else "default"
    key_val = act.api_key

    return {
        "mode": vals["mode"][0],
        "mode_source": vals["mode"][1],
        #: key 只回掩码 + 是否已配置；永不回显完整值
        "api_key_configured": bool(key_val),
        "api_key_masked": _mask(key_val),
        "api_key_source": "db" if act.api_key else "default",
        "base_url": act.base_url,
        "base_url_source": provs_source,
        "model": act.model,
        "model_source": provs_source,
        "tiers": [{"model": t.model, "description": t.description} for t in tiers],
        "tiers_source": tiers_source,
        "questions": {k: questions[k]["instructions"] for k in rg.QUESTION_KEYS},
        #: 分组列表。**只回掩码**，明文 Key 永不出接口。
        "providers": [
            {
                "id": p.id,
                "name": p.name,
                "api_key_configured": bool(p.api_key),
                "api_key_masked": _mask(p.api_key),
                "base_url": p.base_url,
                "model": p.model,
                "enabled": p.enabled,
                #: 没有 Key 的组拿去调用必然 401，前端要能直接标出来
                "usable": p.usable,
            }
            for p in provs
        ],
        "providers_source": provs_source,
        "active_provider": act.id,
        "limits": {"min_tiers": rg.MIN_TIERS, "max_tiers": rg.MAX_TIERS,
                   "question_keys": list(rg.QUESTION_KEYS),
                   "max_providers": rg.MAX_PROVIDERS,
                   "provider_fields": list(rg.PROVIDER_FIELDS)},
        #: 「能否开启 on」的前置条件：当前生效分组必须有 Key
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

    if body.providers is not None:
        # 传旧 cfg 进去：同 id 的 Key 要能从**上一轮存的值**继承（前端拿不回明文）
        cfg["providers"] = _validate_providers(body.providers, cfg)

    if body.active_provider is not None:
        want = (body.active_provider or "").strip()
        if want:
            if want not in {p.id for p in rg.effective_providers(cfg)}:
                raise HTTPException(status_code=400,
                                    detail=f"要选中的分组「{want}」不存在")
            cfg["active_provider"] = want
        else:
            cfg.pop("active_provider", None)  # 空串 = 清除，退回第一个分组

    # 「开 on 必须有可用 Key」放到**最后**校验：此时 cfg 已是最终状态，不用再拼 candidate。
    # 原来把这段塞在 mode 分支里、用 candidate 模拟，引入分组后会漏判
    # —— Key 可能来自本次同时提交的 providers。
    if (cfg.get("mode") or "") == "on" and not rg.active_provider(cfg).api_key:
        raise HTTPException(
            status_code=400,
            detail=f"分组「{rg.active_provider(cfg).id}」还没有 Key，"
                   f"请先配置 API Key 再开启生效模式（on）")

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
def test_config(provider_id: str = "",
                _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    """用**指定分组**（默认当前生效分组）发一次真实的最小 Jev 调用。

    为什么要能指定分组：分组的意义就是「官方挂了切备份」，所以必须能**逐个**
    验证每个备份到底通不通 —— 只测当前选中的那个，等于备份永远是未验证状态。
    失败也返回 200 + `ok=false`：这是「诊断结果」，不是本接口自身的错误。
    """
    wanted = (provider_id or "").strip()
    cfg = None
    if wanted:
        cfg = dict(_read_cfg(db))
        # 临时把选中组换成要测的那个：这样 classify() 内部的 failover 会
        # **优先**试它，第一个候选就是我们要验证的目标。
        cfg["active_provider"] = wanted
        if wanted not in {p.id for p in rg.effective_providers(cfg)}:
            return {"ok": False, "detail": f"分组「{wanted}」不存在", "latency_ms": None}

    prov = rg.active_provider(cfg) if cfg is not None else rg.active_provider()
    if not prov.api_key:
        return {"ok": False, "detail": f"分组「{prov.id}」尚未配置 API Key",
                "latency_ms": None, "provider": prov.id}

    body = {"messages": [{"role": "user", "content": "你好"}]}
    # sticky_key 传一个独立值：否则测出来的可能是**上一个会话的缓存**，
    # 那会让「测试通过」变成假阳性（没真的发请求）。
    res = rg.classify(body, mode="shadow", sticky_key=f"__test__:{prov.id}")
    if res.fallback:
        return {"ok": False, "detail": f"调用失败：{res.fallback}",
                "latency_ms": res.ms, "provider": prov.id,
                "base_url": prov.base_url, "model": prov.model}
    return {
        "ok": True, "detail": "连接正常",
        "latency_ms": res.ms, "provider": prov.id,
        "base_url": prov.base_url, "model": prov.model,
        "sample": {"picked": res.picked, "confidence": round(res.confidence, 3)},
    }
