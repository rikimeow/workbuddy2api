#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""客户端参数档案的 HTTP 接口（探测 / 查看 / 保存 / 重置）。

背景：出站请求要伪装成官方桌面端，涉及 UA、版本号、风控头、桌面指纹等一批
参数。这些值需要「本地探测 → 后台保存 → 线上同步」这条链路，所以给它们
一套 REST 接口（见 `admin/client_profile.py` 的说明）。

接口一览::

    GET  /api/client-profile              当前生效值 + 每项来源 + 已保存值
    POST /api/client-profile/detect       现场探测本机客户端（不保存）
    PUT  /api/client-profile              保存（merge 语义，可只改一个字段）
    POST /api/client-profile/reset        清空保存值，回到纯探测

安全：全部需要管理员鉴权（`require_admin`）。档案本身不含凭据，但
「伪装参数」被随意改动会直接影响线上稳定性，所以不对外开放。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from admin import client_profile as cprofile
from admin.db import get_db
from admin.security import require_admin

_logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/client-profile", tags=["client-profile"])


class SaveIn(BaseModel):
    """保存请求。

    profile 只需给出要改的字段（merge 语义）：比如只改 os_version，
    其余保持不动。merge=False 则整体替换。
    """

    profile: dict = Field(default_factory=dict)
    source: str | None = Field(
        default=None,
        description="auto=优先现场探测 / saved=钉住已保存值（线上实例应选 saved）")
    merge: bool = True


def _payload(db: Session) -> dict:
    """组装给前端的完整视图。"""
    eff = cprofile.effective(db)
    sav = cprofile.saved()
    det = cprofile.snapshot_live()
    return {
        "effective": eff,
        "saved": sav,
        "detected": det,
        "sources": cprofile.sources(db),
        "source": cprofile.source(),
        "defaults": cprofile.DEFAULTS,
        # 有探测值 = 这台机器上确实装了客户端（线上通常为空）
        "has_client": bool(det),
        "diff_saved_vs_effective": cprofile.diff(sav, eff),
    }


@router.get("")
def get_profile(_: bool = Depends(require_admin), db: Session = Depends(get_db)):
    return _payload(db)


@router.post("/detect")
def detect(_: bool = Depends(require_admin)):
    """现场探测本机客户端（只读，不写库）。

    线上服务器上探测结果通常为空 —— 这正是需要「本地探测后同步过去」的原因。
    """
    det = cprofile.snapshot_live()
    return {
        "ok": True,
        "detected": det,
        "has_client": bool(det),
        "message": ("探测到本机客户端" if det else
                    "未探测到本机 WorkBuddy 桌面端（线上实例属正常；"
                    "请在装了客户端的机器上探测后同步过来）"),
    }


@router.put("")
def save(body: SaveIn, _: bool = Depends(require_admin),
         db: Session = Depends(get_db)):
    # profile 可以为空 —— 只切换取值策略（source）也是合法操作：
    # 「钉住已保存值」与「优先现场探测」的切换不涉及任何字段改动。
    if not isinstance(body.profile, dict):
        raise HTTPException(status_code=400, detail="profile 必须是对象")
    if not body.profile and body.source is None:
        raise HTTPException(status_code=400,
                            detail="profile 与 source 不能同时为空")
    if body.source is not None and body.source not in cprofile._VALID_SOURCES:
        raise HTTPException(
            status_code=400,
            detail=f"source 只能是 {list(cprofile._VALID_SOURCES)}")
    res = cprofile.save(body.profile, new_source=body.source, db=db,
                        merge=body.merge)
    if not res.get("ok"):
        raise HTTPException(status_code=400,
                            detail="；".join(res.get("errors") or ["保存失败"]))
    _logger.info("客户端参数档案已更新（source=%s，改动=%s）",
                 res.get("source"), sorted((body.profile or {}).keys()))
    return {"ok": True, "errors": res.get("errors") or [],
            "source": res.get("source"), **_payload(db)}


@router.post("/reset")
def reset(_: bool = Depends(require_admin), db: Session = Depends(get_db)):
    cprofile.reset(db)
    return {"ok": True, **_payload(db)}
