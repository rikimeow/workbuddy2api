"""成长计划任务：列表 / 参与 / 完成 / 领奖。

设计要点
--------
* **任务列表不绑账号**：任意一个可用登录态即可拉取任务定义，结果缓存在
  内存里供前端与定时任务复用（任务定义对所有账号一致，仓库里只存"定义"）。
* **完成操作按 task_code 走策略表**（admin/growth_plans.py）：只对已实测
  可行的任务发包，其余标记为需人工，避免盲目请求污染上游日志。
* **串行 + 限速**：逐个账号、逐个任务执行，中间 sleep，避免触发风控。
"""
import time
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from admin import backend, growth_plans
from admin.config import settings
from admin.db import get_db
from admin.models import Account

router = APIRouter(prefix="/api/growth", tags=["growth"])

#: 任务定义缓存 {"tasks": [...], "synced_at": iso}
_task_cache: dict = {}

#: 执行节流（秒）
_EVENT_GAP = 1.2          # 同一任务内两次触发之间
_ACCOUNT_GAP = 2.0        # 两个账号之间
_ACCEPT_SETTLE = 3.0      # accept 之后等参与状态落库，再触发
_VERIFY_WAIT = 1.5        # 触发后等计数落库，再复查
_MAX_TIMES = 10           # 单任务最多触发次数上限

#: 触发事件时优先使用的免费模型（0 倍率），用完再退回低倍率
_PREFERRED_MODELS = ["hy3", "hunyuan-chat"]


def _pick_free_model(db: Session) -> str:
    """挑一个免费（0 倍率）模型，没有则退回传参默认值。

    任务记账不依赖模型输出质量，用免费模型可以把成本压到 0。
    """
    from admin.models import ModelConfig
    for mid in _PREFERRED_MODELS:
        row = (db.query(ModelConfig)
               .filter(ModelConfig.model_id == mid, ModelConfig.enabled == 1)
               .first())
        if row and (row.credit_multiplier or 0) == 0:
            return mid
    row = (db.query(ModelConfig)
           .filter(ModelConfig.enabled == 1, ModelConfig.credit_multiplier == 0)
           .order_by(ModelConfig.model_id.asc()).first())
    return row.model_id if row else "hy3"


def _pick_account(db: Session) -> Account | None:
    """挑一个可用账号（仅用于拉取任务定义，不写任何东西）。"""
    return (db.query(Account)
            .filter(Account.status == "active")
            .order_by(Account.id.asc()).first())


def _classify_all(tasks: list[dict]) -> list[dict]:
    return [growth_plans.classify(t) for t in tasks]


class AcceptIn(BaseModel):
    account_ids: list[int]
    task_codes: list[str]


class RunIn(BaseModel):
    account_ids: list[int]
    task_codes: list[str] | None = None   # 为空表示「所有可自动完成的任务」


class ClaimIn(BaseModel):
    account_ids: list[int]
    task_codes: list[str] | None = None   # 为空表示「所有已完成待领取的任务」


@router.get("/tasks")
def growth_tasks(refresh: bool = False, db: Session = Depends(get_db)):
    """获取全量任务列表（带分级信息），不绑定具体账号。

    任务定义对所有账号一致，所以随便用一个可用登录态拉取即可。
    """
    if _task_cache.get("tasks") and not refresh:
        cached = dict(_task_cache)
        cached["cached"] = True
        return cached

    acc = _pick_account(db)
    if not acc:
        raise HTTPException(400, "没有可用账号，无法拉取任务列表")

    try:
        with backend.AccountSession(acc.auth_json) as s:
            raw = s.growth_tasks()
    except Exception as e:
        # 拉取失败时退回旧缓存，避免面板整体不可用
        if _task_cache.get("tasks"):
            stale = dict(_task_cache)
            stale.update({"cached": True, "stale": True, "error": str(e)})
            return stale
        raise HTTPException(502, f"拉取任务列表失败: {e}")

    _task_cache.clear()
    _task_cache.update({
        "tasks": _classify_all(raw),
        "synced_at": datetime.utcnow().isoformat(timespec="seconds"),
        "source_account_id": acc.id,
        "cached": False,
    })
    return dict(_task_cache)


def _run_accept(acc: Account, codes: list[str]) -> dict:
    """在一个账号上执行参与，并回写可能被刷新的凭据。"""
    with backend.AccountSession(acc.auth_json) as s:
        try:
            st = s.growth_accept(codes)
        finally:
            acc.auth_json = _updated(s, acc)
    return st


@router.post("/accept")
def growth_accept(payload: AcceptIn, db: Session = Depends(get_db)):
    """批量参与任务（未参与的任务不会累计进度）。"""
    results = []
    for aid in payload.account_ids:
        acc = db.query(Account).get(aid)
        if not acc:
            results.append({"account_id": aid, "ok": False, "msg": "账号不存在"})
            continue
        try:
            st = _run_accept(acc, payload.task_codes)
            results.append({"account_id": aid, "ok": True, "results": st})
        except Exception as e:
            results.append({"account_id": aid, "ok": False, "msg": str(e)})
        time.sleep(_ACCOUNT_GAP)
    db.commit()
    return {"results": results}


def _find_task(session, task_code: str) -> dict | None:
    """重新读取单个任务的最新状态（accept 后刷新用）。"""
    try:
        for t in session.growth_tasks():
            if t.get("task_code") == task_code:
                return t
    except Exception:
        pass
    return None


def _updated(session, acc: Account) -> str:
    """AccountSession 关闭前读回最新凭据（token 可能被刷新）。"""
    try:
        return session.updated_json()
    except Exception:
        return acc.auth_json


def run_accounts(account_ids: list[int], task_codes: list[str] | None,
                 db: Session) -> dict:
    """对一批账号执行「自动参与 + 触发完成」。供接口与定时任务共用。

    这里沉淀了串行执行与节流逻辑，定时任务必须复用本函数，
    避免绕过 accept 落库等待而出现「任务不成功」的问题。
    """
    model = _pick_free_model(db)
    out = []
    for aid in account_ids:
        acc = db.query(Account).get(aid)
        if not acc:
            out.append({"account_id": aid, "ok": False, "msg": "账号不存在"})
            continue

        acc_log = {"account_id": aid, "name": acc.name, "model": model,
                   "ok": True, "tasks": []}
        try:
            with backend.AccountSession(acc.auth_json) as s:
                tasks = s.growth_tasks()
                by_code = {t.get("task_code"): t for t in tasks}

                wanted = task_codes or [
                    t.get("task_code") for t in tasks
                    if growth_plans.plan_for(t.get("task_code") or "").actionable
                    and t.get("accept_status") != "claimed"
                ]

                for code in wanted:
                    info = by_code.get(code) or {}
                    plan = growth_plans.plan_for(code)
                    item = {"task_code": code, "title": info.get("title") or code,
                            "level": plan.level}

                    if info.get("accept_status") == "claimed":
                        item.update({"ok": True, "skipped": "已领取"})
                        acc_log["tasks"].append(item)
                        continue
                    if not plan.actionable:
                        item.update({"ok": False, "skipped": plan.reason or "需人工完成"})
                        acc_log["tasks"].append(item)
                        continue

                    # 参与（未参与的任务不计进度）
                    if info.get("accept_status") == "not_accepted":
                        try:
                            s.growth_accept([code])
                            time.sleep(_ACCEPT_SETTLE)  # 等参与状态落库
                            info = _find_task(s, code) or info
                        except Exception as e:
                            item["accept_error"] = str(e)

                    prog = info.get("progress") or {}
                    target = prog.get("target")
                    current = prog.get("current") or 0
                    times = (target - current) if isinstance(target, int) and target > current else 1
                    times = max(1, min(times, _MAX_TIMES))

                    fire_model = plan.model or model
                    fired = 0
                    for _ in range(times):
                        r = s.growth_fire_event(plan.event_codes, model=fire_model)
                        if r.get("ok"):
                            fired += 1
                        else:
                            item["last_error"] = r.get("msg")
                        time.sleep(_EVENT_GAP)

                    time.sleep(_VERIFY_WAIT)
                    try:
                        now = {t.get("task_code"): t for t in s.growth_tasks()}
                        np_ = (now.get(code) or {}).get("progress") or {}
                        item["progress"] = f"{np_.get('current')}/{np_.get('target')}"
                        item["status"] = (now.get(code) or {}).get("accept_status")
                    except Exception:
                        pass
                    item.update({"ok": fired > 0, "fired": fired, "times": times})
                    if plan.model:
                        item["model"] = plan.model
                    acc_log["tasks"].append(item)

                acc.auth_json = _updated(s, acc)
        except Exception as e:
            acc_log.update({"ok": False, "msg": str(e)})
        out.append(acc_log)
        time.sleep(_ACCOUNT_GAP)

    db.commit()
    return {"results": out, "model": model}


def claim_accounts(account_ids: list[int], task_codes: list[str] | None,
                   db: Session) -> dict:
    """对一批账号领取奖励。供接口与定时任务共用。"""
    out = []
    for aid in account_ids:
        acc = db.query(Account).get(aid)
        if not acc:
            out.append({"account_id": aid, "ok": False, "msg": "账号不存在"})
            continue

        acc_log = {"account_id": aid, "name": acc.name, "ok": True,
                   "claimed": [], "total_credit": 0, "total_energy": 0}
        try:
            with backend.AccountSession(acc.auth_json) as s:
                if task_codes:
                    codes = list(task_codes)
                else:
                    tasks = s.growth_tasks()
                    codes = [t.get("task_code") for t in tasks
                             if t.get("accept_status") == "completed"]

                for code in codes:
                    try:
                        r = s.growth_claim(code)
                        d = r.get("data") or {}
                        credit = d.get("credit") or 0
                        energy = d.get("energy") or 0
                        acc_log["claimed"].append({
                            "task_code": code,
                            "ok": bool(r.get("ok")),
                            "already": bool(d.get("already_claimed")),
                            "credit": credit,
                            "energy": energy,
                            "msg": r.get("msg") or "",
                        })
                        acc_log["total_credit"] += credit
                        acc_log["total_energy"] += energy
                    except Exception as e:
                        acc_log["claimed"].append(
                            {"task_code": code, "ok": False, "msg": str(e)})
                    time.sleep(_EVENT_GAP)
                acc.auth_json = _updated(s, acc)
        except Exception as e:
            acc_log.update({"ok": False, "msg": str(e)})
        out.append(acc_log)
        time.sleep(_ACCOUNT_GAP)

    db.commit()
    return {"results": out}


@router.post("/run")
def growth_run(payload: RunIn, db: Session = Depends(get_db)):
    """执行任务：自动参与 + 触发完成事件。

    只处理策略表里标记为可自动的任务；其它任务跳过并在返回里说明原因。
    """
    return run_accounts(payload.account_ids, payload.task_codes, db)


@router.post("/claim")
def growth_claim(payload: ClaimIn, db: Session = Depends(get_db)):
    """批量领取奖励。

    task_codes 为空时，自动领取所有「已完成但未领取」（completed）的任务。
    """
    return claim_accounts(payload.account_ids, payload.task_codes, db)


@router.get("/accounts/{account_id}/tasks")
def account_tasks(account_id: int, db: Session = Depends(get_db)):
    """单个账号的任务完成情况（面板弹窗用）。"""
    acc = db.query(Account).get(account_id)
    if not acc:
        raise HTTPException(404, "账号不存在")
    try:
        with backend.AccountSession(acc.auth_json) as s:
            tasks = s.growth_tasks()
            profile = s.growth_profile()
    except Exception as e:
        raise HTTPException(502, f"查询失败: {e}")

    items = _classify_all(tasks)
    done = sum(1 for t in items if t.get("accept_status") in ("completed", "claimed"))
    #: 可领取奖励（completed 但未 claim）
    claimable = [t for t in items if t.get("accept_status") == "completed"]
    return {
        "account_id": account_id,
        "name": acc.name,
        "tasks": items,
        "profile": profile,
        "summary": {
            "total": len(items),
            "done": done,
            "claimable": len(claimable),
            "claimable_credit": sum(t.get("reward_credit") or 0 for t in claimable),
            "actionable": sum(1 for t in items if t.get("actionable")
                              and t.get("accept_status") != "claimed"),
        },
    }


@router.get("/plans")
def growth_plans_view():
    """任务策略表（分级依据），便于管理员了解哪些能自动完成。"""
    return {
        "levels": growth_plans.LEVEL_LABEL,
        "plans": [
            {"task_code": p.code, "level": p.level,
             "level_label": growth_plans.LEVEL_LABEL.get(p.level, ""),
             "event_codes": p.event_codes, "reason": p.reason,
             "actionable": p.actionable}
            for p in growth_plans.TASK_PLANS.values()
        ],
    }
