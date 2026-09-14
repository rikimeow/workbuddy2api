"""账号管理：列表 / 新增 / 批量上传 / 扫描本机 / 导入本机 / 刷新余额 / 启用禁用 / 删除 / 注入本机客户端。"""
import glob
import json
import os
import shutil
from datetime import datetime
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from admin import backend, jobrunner
from admin.config import settings
from admin.db import SessionLocal, get_db
from admin.models import Account
from admin.security import require_admin

router = APIRouter(prefix="/api/accounts", tags=["accounts"])


class AccountIn(BaseModel):
    name: Optional[str] = None  # 留空则自动取真实昵称 / uid
    auth_json: str


class AccountBatchIn(BaseModel):
    items: list[AccountIn]


class InjectIn(BaseModel):
    confirm: bool = False


class ImportLocalIn(BaseModel):
    files: list[str] = []  # 指定文件名；含 "all" 或 all=true 表示全部
    all: bool = False


class ExportIn(BaseModel):
    ids: list[int] = []      # 空 = 全部
    include_disabled: bool = False


def _export_items(rows: list[Account]) -> list[str]:
    """把账号记录还原成 .info 原文列表。

    导出的是 auth_json 原文（与上传接受的格式完全一致），
    因此导出文件可以直接再传回来，不需要额外转换。
    """
    items: list[str] = []
    for a in rows:
        raw = (a.auth_json or "").strip()
        if not raw:
            continue
        try:
            json.loads(raw)
        except Exception:
            continue  # 跳过损坏记录，避免整个导出失败
        items.append(raw)
    return items


@router.post("/export")
def export_accounts(
    body: ExportIn,
    _: bool = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """导出账号登录态。

    返回 {items: [.info 原文...]}，与「批量上传」接受的格式一致，
    导出的内容可以直接原样再传回来（round-trip）。

    注意：导出内容含 token，等同账号凭据，请勿外传。
    """
    q = db.query(Account)
    if body.ids:
        q = q.filter(Account.id.in_(body.ids))
    elif not body.include_disabled:
        q = q.filter(Account.status == "active")
    rows = q.order_by(Account.id).all()

    items = _export_items(rows)
    return {
        "total": len(rows),
        "count": len(items),
        "skipped": len(rows) - len(items),
        "items": items,
        # 附带摘要供前端展示（不含凭据）
        "summary": [
            {"id": a.id, "name": a.name, "uid": a.uid, "status": a.status,
             "balance_total": a.balance_total, "balance_remain": a.balance_remain}
            for a in rows
        ],
    }


def _client_auth_dir() -> str:
    d = settings.CLIENT_AUTH_DIR or os.path.expandvars(
        r"%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth"
    )
    return os.path.expandvars(d)


def _apply_meta(acc: Account, auth_json: str):
    meta = backend.parse_auth_meta(auth_json)
    acc.auth_json = auth_json
    if meta.get("uid"):
        acc.uid = meta["uid"]
    if meta.get("enterprise_id"):
        acc.enterprise_id = meta["enterprise_id"]
    if meta.get("domain"):
        acc.domain = meta["domain"]
    # 号池名称：真实昵称 → uid → 兜底（昵称为 null/空串/"null" 视为缺失）
    nick = meta.get("nickname")
    if isinstance(nick, str):
        nick = nick.strip()
    if not acc.name:
        acc.name = (nick if nick and nick.lower() != "null" else None) or meta.get("uid") or "未命名"


def _refresh_balance(acc: Account) -> bool:
    try:
        with backend.AccountSession(acc.auth_json) as sess:
            bal = sess.fetch_balance()
            acc.balance_total = int(bal.get("total", 0) or 0)
            acc.balance_remain = int(bal.get("remain", 0) or 0)
            acc.auth_json = sess.updated_json()  # 回写可能刷新的 token
        acc.last_sync_at = datetime.utcnow()
        return True
    except Exception:
        return False


@router.get("")
def list_accounts(_: bool = Depends(require_admin), db: Session = Depends(get_db)):
    rows = db.query(Account).order_by(Account.id.desc()).all()
    items = [
        {
            "id": a.id,
            "name": a.name,
            "uid": a.uid,
            "enterprise_id": a.enterprise_id,
            "domain": a.domain,
            "status": a.status,
            "balance_total": a.balance_total,
            "balance_remain": a.balance_remain,
            "last_sync_at": a.last_sync_at.isoformat() if a.last_sync_at else None,
            "last_used_at": a.last_used_at.isoformat() if a.last_used_at else None,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        }
        for a in rows
    ]
    summary = {
        "total": len(items),
        "active": sum(1 for i in items if i["status"] == "active"),
        "available": sum(1 for i in items if i["status"] == "active" and i["balance_remain"] > 0),
        "balance_total": sum(i["balance_total"] for i in items),
        "balance_remain": sum(i["balance_remain"] for i in items),
    }
    return {"items": items, "summary": summary}


@router.post("")
def add_account(body: AccountIn, _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    try:
        json.loads(body.auth_json)
    except Exception:
        raise HTTPException(status_code=400, detail="auth_json 不是合法 JSON")
    acc = Account(name=body.name) if body.name else Account()
    _apply_meta(acc, body.auth_json)
    db.add(acc)
    db.commit()
    db.refresh(acc)
    _refresh_balance(acc)
    db.commit()
    return {"id": acc.id, "name": acc.name, "ok": True}


@router.post("/batch")
def batch_add(body: AccountBatchIn, _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    added = 0
    errors = []
    for it in body.items:
        if not it.auth_json or not it.auth_json.strip():
            continue
        try:
            json.loads(it.auth_json)
        except Exception:
            errors.append("跳过一条：auth_json 非法 JSON")
            continue
        acc = Account(name=it.name) if it.name else Account()
        _apply_meta(acc, it.auth_json)
        db.add(acc)
        db.commit()
        db.refresh(acc)
        if _refresh_balance(acc):
            added += 1
        else:
            errors.append(f"账号 {acc.id} 余额刷新失败（凭据可能失效）")
        db.commit()
    return {"added": added, "errors": errors}


@router.get("/scan-local")
def scan_local(_: bool = Depends(require_admin)):
    """扫描本机 WorkBuddy/CodeBuddy 登录态目录，列出发现的账号（不读 token 内容到前端）。"""
    d = _client_auth_dir()
    if not os.path.isdir(d):
        return {"dir": d, "exists": False, "active_uid": None, "items": []}
    active_uid = None
    active_file = os.path.join(d, "workbuddy-desktop.info")
    if os.path.exists(active_file):
        try:
            active_uid = (json.load(open(active_file, encoding="utf-8")).get("account") or {}).get("uid")
        except Exception:
            pass
    items = []
    seen_uids = set()  # 按 uid 去重
    for f in sorted(glob.glob(os.path.join(d, "*.info"))):
        base = os.path.basename(f)
        if base.endswith(".bak") or ".bak-" in base:
            continue
        try:
            data = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        meta = backend.parse_auth_meta(json.dumps(data))
        uid = meta.get("uid") or ""
        # 同 uid 只保留一份（优先保留无时间戳后缀的"实时登录"文件）
        if uid in seen_uids:
            continue
        seen_uids.add(uid)
        # 提取 token 到期时间
        auth = data.get("auth") or {}
        expires_at = auth.get("expiresAt") or 0
        expires_str = ""
        if expires_at:
            try:
                expires_str = datetime.fromtimestamp(expires_at / 1000).strftime("%Y-%m-%d %H:%M:%S")
            except (OSError, ValueError):
                expires_str = str(expires_at)
        # 提取昵称：过滤字面量 "null" 字符串
        raw_nick = meta.get("nickname") or ""
        nickname = raw_nick if raw_nick.strip().lower() not in ("null", "", "none") else ""
        items.append({
            "file": base,
            "uid": uid,
            "nickname": nickname,
            "domain": meta.get("domain") or "",
            "is_active": (uid == active_uid),
            "expires_at": expires_str,
            "expires_ts": expires_at,
        })
    return {"dir": d, "exists": True, "active_uid": active_uid, "items": items}


@router.post("/import-local")
def import_local(body: ImportLocalIn, _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    """把本机登录态目录里的 .info 直接读入号池（服务端读取，不向前端暴露 token）。"""
    d = _client_auth_dir()
    if not os.path.isdir(d):
        raise HTTPException(status_code=500, detail=f"客户端登录目录不存在: {d}")
    names = list(body.files)
    if body.all or "all" in names:
        names = [
            os.path.basename(f) for f in glob.glob(os.path.join(d, "*.info"))
            if not (f.endswith(".bak") or ".bak-" in f)
        ]
    added = 0
    errors = []
    seen = {a.uid for a in db.query(Account).all()}  # 已存在的 uid 跳过，避免重复导入
    for name in names:
        path = os.path.join(d, name)
        if not os.path.isfile(path):
            errors.append(f"文件不存在: {name}")
            continue
        try:
            auth = open(path, encoding="utf-8").read()
            json.loads(auth)
        except Exception:
            errors.append(f"{name}: 读取/解析失败")
            continue
        meta = backend.parse_auth_meta(auth)
        uid = meta.get("uid")
        if uid and uid in seen:
            continue  # 同 uid 多文件 / 已存在，只导入一次
        seen.add(uid)
        acc = Account()
        _apply_meta(acc, auth)
        db.add(acc)
        db.commit()
        db.refresh(acc)
        if _refresh_balance(acc):
            added += 1
        else:
            errors.append(f"账号 {acc.id}({acc.name}) 余额刷新失败（凭据可能失效）")
        db.commit()
    return {"added": added, "errors": errors}


@router.get("/{acc_id}/export")
def export_one_account(
    acc_id: int,
    _: bool = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """导出单个账号的登录态（.info 原文）。

    与批量导出格式一致，可直接用「批量上传」导回。
    """
    acc = db.query(Account).filter(Account.id == acc_id).first()
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")
    raw = (acc.auth_json or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="该账号没有可导出的凭据")
    try:
        json.loads(raw)
    except Exception:
        raise HTTPException(status_code=400, detail="凭据内容不是合法 JSON，无法导出")
    return {
        "id": acc.id,
        "name": acc.name,
        "uid": acc.uid,
        "item": raw,
    }


@router.post("/{acc_id}/inject")
def inject_to_client(acc_id: int, body: InjectIn, _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    """把号池里某账号的 .info 注入到本机客户端的活动登录文件（先备份当前登录态）。"""
    acc = db.query(Account).filter(Account.id == acc_id).first()
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")
    if not body.confirm:
        raise HTTPException(status_code=400, detail="需要 confirm=true 才执行注入")
    d = _client_auth_dir()
    if not os.path.isdir(d):
        raise HTTPException(status_code=500, detail=f"客户端登录目录不存在: {d}")
    target = os.path.join(d, "workbuddy-desktop.info")
    backup = None
    if os.path.exists(target):
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = target + f".bak-{ts}"
        shutil.copy2(target, backup)
    with open(target, "w", encoding="utf-8") as f:
        f.write(acc.auth_json)
    return {"ok": True, "target": target, "backup": backup, "account": acc.name, "uid": acc.uid}


@router.post("/{acc_id}/refresh")
def refresh_account(acc_id: int, _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    acc = db.query(Account).filter(Account.id == acc_id).first()
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")
    ok = _refresh_balance(acc)
    db.commit()
    if not ok:
        raise HTTPException(status_code=502, detail="刷新失败：后端调用异常（凭据/限流）")
    return {"id": acc.id, "balance_total": acc.balance_total, "balance_remain": acc.balance_remain}


@router.get("/{acc_id}/credit-details")
def credit_details(acc_id: int, _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    """获取账号积分明细（每个积分包的总量/剩余/到期时间）。"""
    acc = db.query(Account).filter(Account.id == acc_id).first()
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")
    try:
        with backend.AccountSession(acc.auth_json) as sess:
            packages = sess.fetch_credit_details()
            # 回写可能刷新的 token
            acc.auth_json = sess.updated_json()
            db.commit()
        return {"id": acc_id, "account": acc.name, "packages": packages}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"获取积分明细失败: {e}")


@router.get("/{acc_id}/request-usage")
def request_usage(
    acc_id: int,
    start_time: str = "",
    end_time: str = "",
    page_num: int = 1,
    page_size: int = 10,
    _: bool = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """获取账号模型请求用量（对接 WorkBuddy 已有接口，不自建日志）。"""
    acc = db.query(Account).filter(Account.id == acc_id).first()
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")
    # 默认今天
    if not start_time:
        start_time = datetime.now().strftime("%Y-%m-%d 00:00:00")
    if not end_time:
        end_time = datetime.now().strftime("%Y-%m-%d 23:59:59")
    try:
        with backend.AccountSession(acc.auth_json) as sess:
            data = sess.fetch_request_usage(start_time, end_time, page_num, page_size)
            acc.auth_json = sess.updated_json()
            db.commit()
        return data
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"获取请求用量失败: {e}")


@router.post("/{acc_id}/cat-travel")
def cat_travel(acc_id: int, _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    """对单个账号执行一趟猫猫旅行：同意协议 → 首次领养(+300) → 派出 → 领奖。

    返回结构化分步结果，前端据此逐步提示「领养成功 +300」「领奖成功 +N」，
    无需再去积分明细核对。
    """
    acc = db.query(Account).filter(Account.id == acc_id).first()
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")
    try:
        with backend.AccountSession(acc.auth_json) as sess:
            result = sess.run_cat_travel()
            acc.auth_json = sess.updated_json()  # 回写可能刷新的 token
            db.commit()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"猫猫旅行执行失败: {e}")
    # 领取到积分后顺带刷新余额，让前端列表立即反映最新值
    if result.get("credits"):
        _refresh_balance(acc)
        db.commit()
    return {
        "id": acc.id,
        "account": acc.name,
        "balance_remain": acc.balance_remain,
        **result,
    }


class CatTravelBatchIn(BaseModel):
    ids: list[int] = []  # 指定账号；为空则对全部启用账号执行


#: 猫猫旅行后台任务 key
CAT_JOB_KEY = "cat_travel"


@router.post("/cat-travel/batch-async")
def cat_travel_batch_async(
    body: CatTravelBatchIn,
    _: bool = Depends(require_admin),
):
    """异步批量猫猫旅行：立即返回 job_id，前端轮询进度。

    每个账号要串行做「同意协议 → 补门槛对话 → 领养 → 派猫 → 领奖」，
    同步等容易让 nginx 先超时（默认 60s），所以放到后台线程跑。
    重复点击复用同一个 job，不会叠起并发批量。
    """
    running = jobrunner.RUNNER.get(CAT_JOB_KEY)
    if running and running.status == "running":
        return {"reused": True, **running.snapshot()}

    db = SessionLocal()
    try:
        q = db.query(Account).filter(Account.status == "active")
        if body.ids:
            q = q.filter(Account.id.in_(body.ids))
        ids = [a.id for a in q.order_by(Account.id).all()]
    finally:
        db.close()

    def worker(job: jobrunner.Job) -> dict:
        db = SessionLocal()
        buckets = {"adopted": 0, "adopt_credits": 0,
                   "travel_claimed": 0, "travel_credits": 0,
                   "travel_none": 0, "traveling": 0,
                   "gate_blocked": 0, "error": 0}
        try:
            job.set_phase("执行中")
            for aid in ids:
                acc = db.query(Account).get(aid)
                if not acc:
                    continue
                try:
                    with backend.AccountSession(acc.auth_json) as sess:
                        res = sess.run_cat_travel()
                        acc.auth_json = sess.updated_json()
                        db.commit()
                except Exception as e:
                    item = {"id": aid, "account": acc.name, "ok": False,
                            "credits": 0, "summary": f"执行异常: {e}",
                            "steps": [], "outcome": "error"}
                    buckets["error"] += 1
                    job.add_item(item)
                    continue

                if res.get("credits"):
                    _refresh_balance(acc)
                    db.commit()
                outcome = res.get("outcome") or ("error" if not res.get("ok") else "travel_none")
                if outcome == "adopted":
                    buckets["adopted"] += 1
                    buckets["adopt_credits"] += res.get("credits") or 0
                elif outcome == "travel_claimed":
                    buckets["travel_claimed"] += 1
                    buckets["travel_credits"] += res.get("credits") or 0
                elif outcome in buckets:
                    buckets[outcome] += 1
                else:
                    buckets["travel_none"] += 1

                job.add_item({"id": aid, "account": acc.name,
                              "balance_remain": acc.balance_remain, **res})
                time.sleep(0.5)   # 账号之间留一点间隔，避免打太快
        finally:
            db.close()
        return {
            "total": len(ids),
            "credits": buckets["adopt_credits"] + buckets["travel_credits"],
            "succeeded": sum(1 for r in job.items if r.get("ok")),
            "buckets": buckets,
        }

    job = jobrunner.RUNNER.start(CAT_JOB_KEY, len(ids), worker, title="批量猫猫旅行")
    return job.snapshot()


@router.post("/cat-travel/batch")
def cat_travel_batch(
    body: CatTravelBatchIn,
    _: bool = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """批量执行猫猫旅行；逐个账号串行执行并返回每个账号的结果。

    按结果分类统计，避免把「首次领养 +300」和「旅行到站返现 +7」混在一起 ——
    两者量级差几十倍，合并成一个总数会让人误以为执行无效。
    """
    q = db.query(Account).filter(Account.status == "active")
    if body.ids:
        q = q.filter(Account.id.in_(body.ids))
    rows = q.order_by(Account.id).all()

    results = []
    total_credits = 0
    #: 分类计数：首次领养 / 旅行返现 / 无新增 / 门槛未达标 / 失败
    buckets = {"adopted": 0, "adopt_credits": 0,
               "travel_claimed": 0, "travel_credits": 0,
               "travel_none": 0, "traveling": 0,
               "gate_blocked": 0, "error": 0}
    for acc in rows:
        try:
            with backend.AccountSession(acc.auth_json) as sess:
                res = sess.run_cat_travel()
                acc.auth_json = sess.updated_json()
                db.commit()
        except Exception as e:
            results.append({"id": acc.id, "account": acc.name, "ok": False,
                            "credits": 0, "summary": f"执行异常: {e}", "steps": [],
                            "outcome": "error"})
            buckets["error"] += 1
            continue
        if res.get("credits"):
            total_credits += res["credits"]
            _refresh_balance(acc)
            db.commit()

        outcome = res.get("outcome") or ("error" if not res.get("ok") else "travel_none")
        if outcome == "adopted":
            buckets["adopted"] += 1
            buckets["adopt_credits"] += res.get("credits") or 0
        elif outcome == "travel_claimed":
            buckets["travel_claimed"] += 1
            buckets["travel_credits"] += res.get("credits") or 0
        elif outcome in buckets:
            buckets[outcome] += 1
        else:
            buckets["travel_none"] += 1

        results.append({
            "id": acc.id,
            "account": acc.name,
            "balance_remain": acc.balance_remain,
            **res,
        })
    return {
        "total": len(results),
        "credits": total_credits,
        "succeeded": sum(1 for r in results if r.get("ok")),
        "buckets": buckets,
        "results": results,
    }


@router.patch("/{acc_id}")
def patch_account(
    acc_id: int,
    body: dict,
    _: bool = Depends(require_admin),
    db: Session = Depends(get_db),
):
    acc = db.query(Account).filter(Account.id == acc_id).first()
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")
    if "name" in body:
        acc.name = body["name"]
    if "status" in body and body["status"] in ("active", "disabled"):
        acc.status = body["status"]
    db.commit()
    return {"id": acc.id, "ok": True}


@router.delete("/{acc_id}")
def delete_account(acc_id: int, _: bool = Depends(require_admin), db: Session = Depends(get_db)):
    acc = db.query(Account).filter(Account.id == acc_id).first()
    if not acc:
        raise HTTPException(status_code=404, detail="账号不存在")
    db.delete(acc)
    db.commit()
    return {"id": acc_id, "ok": True}
