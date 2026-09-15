"""复用 converter.CredentialManager 操作单个账号的后端会话。

账号凭据以 .info 原文形式存于 MySQL；用时落盘成临时文件交给 CredentialManager，
用完读回（token 可能被刷新），写回 MySQL。
"""
import json
import os
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path

import httpx

from converter import BACKEND, CredentialManager  # 复用既有后端鉴权 / 刷新 / 模型 / 额度逻辑

# 连接池：减少 TLS 握手，与 Go 项目 MaxIdleConnsPerHost=20 对齐。
HTTP_LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=20)

#: 各域名与 UA（成长任务在不同域上报，头形状必须与对应客户端一致）
WEB_BASE = "https://www.workbuddy.cn"      # web 域：资料库等浏览器行为
BILL_BASE = "https://www.codebuddy.cn"     # billing 域：常规业务上报
WEB_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
DESKTOP_UA = "WorkBuddy/5.5.6 WorkBuddy/5.5.6 CLI/2.137.1"

#: 资料库介绍页（Library_read 的 pageURL，必须是真实可访问的文档页）
LIBRARY_DOC_URL = f"{WEB_BASE}/space/d/o0KWYeynteVv06UnAZqIFm"

#: 企鹅教师助手 Buddy 应用 id。
#: 客户端「发现应用」入口里的应用标识，无公开列表接口（探测过 open-platform /
#: buddy 等路径均 404），只能从客户端内置清单取。一组 buddyapp 事件同时满足
#: 「发现应用」和「企鹅教师助手」两个任务。
BUDDY_QQ_APP = ("cb_y5Dy46tPQGGWtueMxXbe", "企鹅教师助手")


def stable_device_id(uid: str, salt: str) -> str:
    """由 uid 稳定派生设备标识（machineId / sessionId 用）。

    为什么必须稳定：同一账号在服务端眼里应当始终是**同一台设备**。
    每次随机 = 频繁换设备 = 明显异常；所有账号共用一个常量则更糟
    （多账号同一设备，是最容易被批量识别的特征）。

    取 master 项目 deriveID 同款算法（md5(salt:uid) 截 36 位），
    不参与任何业务逻辑，仅用于事件指纹。
    """
    import hashlib
    return hashlib.md5(f"{salt}:{uid}".encode()).hexdigest()[:36]



def parse_auth_meta(auth_json: str) -> dict:
    """从 .info 原文里抽取账号元信息（uid / enterpriseId / domain / 昵称）。"""
    try:
        data = json.loads(auth_json)
    except Exception:
        return {}
    auth = data.get("auth") or {}
    acct = data.get("account") or {}
    return {
        "uid": str(acct.get("uid") or ""),
        "enterprise_id": str(acct.get("enterpriseId") or ""),
        "domain": str(auth.get("domain") or ""),
        "nickname": str(acct.get("nickname") or ""),
    }


class AccountSession:
    """把一个账号的 auth_json 包成可用的后端会话。"""

    def __init__(self, auth_json: str):
        self._path = tempfile.mktemp(suffix=".info")
        with open(self._path, "w", encoding="utf-8") as f:
            f.write(auth_json)
        self.cm = CredentialManager(Path(self._path))

    def get_headers(self, extra: dict | None = None) -> dict:
        return self.cm.get_headers(extra=extra)

    def fetch_models(self) -> list:
        return self.cm.fetch_models()

    def fetch_balance(self) -> dict:
        return self.cm.fetch_balance()

    def fetch_credit_details(self) -> list[dict]:
        """获取积分明细（每个积分包的总量/剩余/到期时间）。

        对应截图中的「版本基础用量」「权益赠送包」等条目。
        剩余额度使用 CycleCapacityRemain（当前周期剩余），与官方界面「累积剩余」对齐；
        CapacityRemain 仅作为账号层级总剩余保留在字段 account_remain 中供参考。
        """
        data = self.cm._request_backend("POST", "/v2/billing/meter/get-user-resource", {})
        resp = data.get("data", {}).get("Response", {}).get("Data", {}) or {}
        packages = []
        for a in resp.get("Accounts") or []:
            if a.get("CapacityUnit") != "credits":
                continue
            # CycleEndTime = 当前周期结束时间（如 "2026-09-30 23:59:59"）
            # DeductionEndTime = 绝对到期时间戳（毫秒），0 表示永不过期
            # ExpiredTime = 已过期时间（通常为空串，表示未过期）
            cycle_end = a.get("CycleEndTime") or ""
            deduction_end_ts = a.get("DeductionEndTime") or 0
            packages.append({
                "name": a.get("PackageName") or a.get("Name") or "未命名",
                "total": a.get("CapacitySize") or 0,
                # 真实可用额度以当前周期剩余为准（体验版用完时 CapacityRemain 仍可能为 500）
                "remain": a.get("CycleCapacityRemain") or 0,
                "used": a.get("CycleCapacityUsed") or 0,
                "account_remain": a.get("CapacityRemain") or 0,
                "account_used": a.get("CapacityUsed") or 0,
                "cycle_start": a.get("CycleStartTime") or "",
                "cycle_end": cycle_end,
                "deduction_end_ts": deduction_end_ts,
                "deduction_end": datetime.fromtimestamp(deduction_end_ts / 1000).strftime("%Y-%m-%d %H:%M:%S")
                    if isinstance(deduction_end_ts, (int, float)) and deduction_end_ts > 0 else "",
                "status": a.get("Status"),
                "package_code": a.get("PackageCode") or "",
            })
        return packages

    def fetch_request_usage(self, start_time: str, end_time: str, page_num: int = 1, page_size: int = 10) -> dict:
        """获取模型请求用量（对接 WorkBuddy 已有接口，不自建日志）。"""
        return self.cm._request_backend("POST", "/billing/meter/get-user-request-usage", {
            "startTime": start_time,
            "endTime": end_time,
            "pageNum": page_num,
            "pageSize": page_size,
        })

    # -----------------------------------------------------------------------
    # 每日签到领取 100 积分（Buddy 加油站活动）
    # -----------------------------------------------------------------------

    def get_checkin_status(self) -> dict:
        """查询当前账号的签到活动状态。

        返回后端 data 字段（含 active / today_checked_in / end_time / activity_name 等）。
        end_time 即活动结束时间，是「下次停止领取」配置的依据。
        """
        data = self.cm._request_backend_soft("POST", "/v2/billing/meter/checkin-activity-status", {})
        return data.get("data") or {}

    def claim_daily_checkin(self) -> dict:
        """执行每日签到领取。

        成功返回 {"ok": True, "credit": int, "streak_days": int}；
        业务失败（已领/无资格/活动结束）返回 {"ok": False, "code": int, "status": str}。
        """
        data = self.cm._request_backend_soft("POST", "/v2/billing/meter/daily-checkin", {})
        code = data.get("code")
        payload = data.get("data") or {}
        if code and code != 0:
            return {"ok": False, "code": code, "status": _map_checkin_status(code), "msg": data.get("msg")}
        credit = payload.get("credit")
        if credit is None:
            credit = data.get("credit")
        streak = payload.get("streak_days")
        if streak is None:
            streak = data.get("streak_days")
        return {"ok": True, "credit": credit or 0, "streak_days": streak or 0}

    # -----------------------------------------------------------------------
    # 猫猫旅行（/activity/growth/buddy/*）
    #
    # 流程：同意协议 → 首次领养（+300 积分）→ 派出 → 到站领奖。
    # 域为 chatBase（CN = copilot.tencent.com，不带 /v2 前缀），与 billing 域不同。
    # -----------------------------------------------------------------------

    #: 领养门槛未达标的业务错误关键词（HTTP 400 时出现），属预期而非失败。
    BUDDY_TASK_INCOMPLETE_MARKER = "first_buddy task not completed yet"

    def _growth(self, method: str, path: str, body: dict | None = None) -> dict:
        """发 growth 域请求；返回 {ok, status, code, msg, data}，不抛异常。

        与 billing 域的 `_request_backend_soft` 不同：growth 域的「门槛未达」
        等业务失败走 HTTP 400，需要调用方读取 msg 判定，故这里把结果结构化返回。
        """
        headers = self.cm.get_headers()
        url = f"{BACKEND}{path}"
        try:
            with httpx.Client(timeout=15, limits=HTTP_LIMITS) as c:
                if method.upper() == "GET":
                    r = c.get(url, headers=headers)
                else:
                    r = c.post(url, headers=headers, json=body if body is not None else {})
        except Exception as e:
            return {"ok": False, "status": 0, "code": None, "msg": f"网络失败: {e}", "data": {}}
        try:
            payload = r.json()
        except Exception:
            return {
                "ok": False, "status": r.status_code, "code": None,
                "msg": f"非 JSON 响应 HTTP {r.status_code}: {r.text[:200]}", "data": {},
            }
        code = payload.get("code")
        ok = r.status_code == 200 and code == 0
        return {
            "ok": ok,
            "status": r.status_code,
            "code": code,
            "msg": payload.get("msg") or "",
            "data": payload.get("data") or {},
        }

    def buddy_info(self) -> dict | None:
        """查询当前猫档案；None 表示无猫（data.buddy 为 null），即尚未领养。"""
        res = self._growth("GET", "/activity/growth/buddy/info")
        if not res["ok"]:
            raise RuntimeError(f"查询猫档案失败: {res['msg']}")
        buddy = res["data"].get("buddy")
        return buddy if isinstance(buddy, dict) and buddy else None

    def buddy_agreement(self) -> dict:
        """同意活动协议（幂等，重复调用无副作用）。"""
        return self._growth("POST", "/activity/growth/buddy/agreement", {"agree": True})

    def buddy_first(self) -> dict:
        """首次领养。成功即发放 300 积分。"""
        return self._growth("POST", "/activity/growth/buddy/first", {})

    def travel_status(self) -> dict:
        """查询猫猫旅行状态：state(idle/traveling/arrived) / record_id / reward_credit。"""
        res = self._growth("GET", "/activity/growth/buddy/travel/status")
        if not res["ok"]:
            raise RuntimeError(f"查询旅行状态失败: {res['msg']}")
        return res["data"] or {}

    def travel_depart(self, location_id: int = 4) -> dict:
        """派出猫旅行。4 个地点收益/时长区间完全相同，固定用 4（古镇客栈）。"""
        return self._growth("POST", "/activity/growth/buddy/travel/depart", {"location_id": location_id})

    def travel_claim(self, record_id: int) -> dict:
        """领取到站奖励；成功时 data.reward_credit 为实发积分。"""
        return self._growth("POST", "/activity/growth/buddy/travel/claim", {"record_id": record_id})

    def _is_threshold_not_met(self, res: dict) -> bool:
        """判定「领养门槛未达标」：HTTP 400 + first_buddy 关键词。"""
        return (
            res.get("status") == 400
            and self.BUDDY_TASK_INCOMPLETE_MARKER in str(res.get("msg", "")).lower()
        )

    #: 领养门槛所需的最小对话次数。服务端原文为
    #: "first_buddy task not completed yet (need at least one conversation)"，
    #: 实测发 1 次 chat_request_send 事件即可让 first_buddy 变为 completed。
    BUDDY_GATE_CHATS = 1

    def _clear_buddy_gate(self) -> tuple[bool, str]:
        """尝试清除领养门槛（first_buddy 任务）。

        实测结论：发一次带 chat_request_send 的对话事件，first_buddy 即从
        not_accepted 直接变为 completed，无需先调 accept。事件走的是正常的
        /v2/chat/completions（免费模型 + max_tokens=1，HTTP 200，成本为 0）。

        Returns:
            (是否已达标, 说明文字)
        """
        for i in range(self.BUDDY_GATE_CHATS):
            try:
                r = self.growth_fire_event(["chat_request_send"])
            except Exception as e:
                return False, f"触发对话异常：{e}"
            if not r.get("ok"):
                return False, f"触发对话失败：{r.get('msg') or r.get('status')}"
            if i + 1 < self.BUDDY_GATE_CHATS:
                time.sleep(1.5)

        # 复查任务是否达标
        time.sleep(1.5)
        try:
            for t in self.growth_tasks():
                if t.get("task_code") == "first_buddy":
                    if t.get("accept_status") in ("completed", "claimed"):
                        return True, "已完成首次对话，门槛达标"
                    pr = t.get("progress") or {}
                    return False, (f"对话后仍未达标（{pr.get('current')}/{pr.get('target')}）")
        except Exception as e:
            return False, f"复查任务状态异常：{e}"
        return False, "未找到 first_buddy 任务"

    def run_cat_travel(self, location_id: int = 4) -> dict:
        """执行一趟猫猫旅行，返回结构化分步结果（供前端逐步提示）。

        步骤语义：
          - agreement : 无猫时才做，同意活动协议
          - gate      : 无猫且门槛未达标时，补一次对话以解锁领养
          - adopt     : 无猫时才做，首次领养，成功 +300 积分
          - depart    : 空闲时派出
          - claim     : 到站时领奖，reward 为实发积分

        领养门槛：领养要求 first_buddy 任务完成，该任务的条件是
        「至少一次对话」。本流程会自动补上这次对话（免费模型、成本 0），
        因此新手账号也能一次跑通，不需要人工先去聊一句。
        """
        steps: list[dict] = []
        credits = 0

        def add(step: str, ok: bool, message: str, reward: int = 0, skipped: bool = False):
            nonlocal credits
            credits += reward
            steps.append({
                "step": step, "ok": ok, "skipped": skipped,
                "reward": reward, "message": message,
            })

        # ── 1) 查猫档案 ────────────────────────────────────────────────
        try:
            buddy = self.buddy_info()
        except Exception as e:
            add("info", False, str(e))
            return {"ok": False, "credits": credits, "steps": steps,
                    "summary": "查询猫档案失败", "outcome": "error"}

        # ── 2) 无猫则领养 ──────────────────────────────────────────────
        if buddy is None:
            agr = self.buddy_agreement()
            if not agr["ok"]:
                add("agreement", False, f"同意协议失败：{agr['msg'] or agr['status']}")
                return {"ok": False, "credits": credits, "steps": steps,
                        "summary": "同意协议失败", "outcome": "error"}
            add("agreement", True, "已同意活动协议")

            first = self.buddy_first()

            # 门槛未达标：自动补一次对话后再试，避免新手账号必然失败
            if not first["ok"] and self._is_threshold_not_met(first):
                add("gate", True, "领养门槛未达标，自动补一次对话以解锁")
                gate_ok, gate_msg = self._clear_buddy_gate()
                add("gate", gate_ok, gate_msg, skipped=not gate_ok)
                if gate_ok:
                    first = self.buddy_first()

            if first["ok"]:
                # 领养成功发放 300 积分；上游在 data 里回传实际到账值
                data = first.get("data") or {}
                got = int(data.get("credit") or 0)
                energy = int(data.get("energy") or 0)
                reward = got if got > 0 else 300
                msg = f"领养成功，已发放 {reward} 积分"
                if energy:
                    msg += f" + {energy} 能量"
                badge = (data.get("badge") or {}).get("name")
                if badge:
                    msg += f"，解锁徽章「{badge}」"
                add("adopt", True, msg, reward=reward)
                return {"ok": True, "credits": credits, "steps": steps,
                        "summary": msg, "outcome": "adopted"}
            if self._is_threshold_not_met(first):
                add("adopt", True, "本次无法领养：对话门槛未达标", skipped=True)
                return {"ok": True, "credits": credits, "steps": steps,
                        "summary": "本次无法领养（对话门槛未达标）",
                        "outcome": "gate_blocked"}
            add("adopt", False, f"领养失败：{first['msg'] or first['status']}")
            return {"ok": False, "credits": credits, "steps": steps,
                    "summary": "领养失败", "outcome": "error"}

        add("adopt", True, f"已有猫：{buddy.get('name') or buddy.get('id')}", skipped=True)

        # ── 3) 查旅行状态 ──────────────────────────────────────────────
        try:
            st = self.travel_status()
        except Exception as e:
            add("status", False, str(e))
            return {"ok": False, "credits": credits, "steps": steps,
                    "summary": "查询旅行状态失败", "outcome": "error"}

        state = str(st.get("state") or "").strip()
        record_id = int(st.get("record_id") or 0)

        # ── 4) 到站领奖 / 空闲派出 ─────────────────────────────────────
        if state == "arrived":
            if record_id <= 0:
                add("claim", False, "已到站但缺少 record_id，无法领奖")
                return {"ok": False, "credits": credits, "steps": steps,
                        "summary": "领奖失败（缺少 record_id）", "outcome": "error"}
            cl = self.travel_claim(record_id)
            if cl["ok"]:
                reward = int((cl["data"] or {}).get("reward_credit") or 0)
                add("claim", True, f"领奖成功，获得 {reward} 积分", reward=reward)
                outcome = "travel_claimed" if reward > 0 else "travel_none"
            else:
                add("claim", False, f"领奖失败：{cl['msg'] or cl['status']}")
                return {"ok": False, "credits": credits, "steps": steps,
                        "summary": "领奖失败", "outcome": "error"}
        elif state == "idle":
            outcome = "travel_none"
            if st.get("daily_limit_reached"):
                add("depart", True, "今日已派出过，明日 00:00 后可再次派出", skipped=True)
            else:
                dp = self.travel_depart(location_id)
                if dp["ok"]:
                    add("depart", True, "已派出猫咪旅行，到站后可领奖")
                else:
                    add("depart", False, f"派出失败：{dp['msg'] or dp['status']}")
                    return {"ok": False, "credits": credits, "steps": steps,
                            "summary": "派出失败", "outcome": "error"}
        elif state == "traveling":
            outcome = "traveling"
            add("depart", True, f"猫咪正在旅行中（record={record_id}），到站后可领奖",
                skipped=True)
        else:
            outcome = "unknown"
            add("status", True, f"未知旅行状态 {state!r}，未执行动作", skipped=True)

        total = sum(s["reward"] for s in steps)
        if total > 0:
            summary = f"完成，共获得 {total} 积分"
        else:
            summary = "完成，本次无新增积分"
        return {"ok": True, "credits": credits, "steps": steps,
                "summary": summary, "outcome": outcome}

    # -----------------------------------------------------------------------
    # 成长计划任务（/v2/activity/growth/tasks*）
    #
    # 与猫猫旅行同域（chatBase）。完整流程：
    #   拉列表 → 参与(accept) → 触发行为 → 领奖(claim)
    #
    # 关键机制（逆向得出）：任务进度由「请求体里的 extra_vars.growthEvent」
    # 驱动，而非独立的上报接口。事件形如：
    #   extra_vars.growthEvent = '[{"eventCode":"chat_request_send","id":"<会话ID>"}]'
    # 服务端只按 eventCode 记账，不校验模型是否真的被调用。
    # -----------------------------------------------------------------------

    def growth_tasks(self) -> list[dict]:
        """拉取成长任务列表（含进度与状态）。"""
        res = self._growth("GET", "/v2/activity/growth/tasks")
        if not res["ok"]:
            raise RuntimeError(f"拉取任务列表失败: {res['msg']}")
        return res["data"].get("tasks") or []

    def growth_accept(self, task_codes: list[str]) -> dict:
        """批量参与任务。

        未参与（not_accepted）的任务不会累计进度，必须先 accept。
        返回 {task_code: status}，status 为 accepted / already_accepted。
        """
        res = self._growth("POST", "/v2/activity/growth/tasks/accept",
                           {"task_codes": list(task_codes)})
        if not res["ok"]:
            raise RuntimeError(f"参与任务失败: {res['msg']}")
        return {r.get("task_code"): r.get("status")
                for r in (res["data"].get("results") or [])}

    def growth_claim(self, task_code: str) -> dict:
        """领取单任务奖励。

        注意路径形态与其它 growth 接口不同：是 /activity/growth/tasks/{code}/claim
        （无 v2 前缀，任务码在路径中，POST 空体）。
        """
        return self._growth("POST", f"/activity/growth/tasks/{task_code}/claim")

    def growth_profile(self) -> dict:
        """成长档案（等级 / 已完成数等）。"""
        res = self._growth("GET", "/v2/activity/growth/profile")
        return res["data"] if res["ok"] else {}

    def growth_fire_event(self, event_codes: list[str], model: str = "hy3",
                          event_id: str | None = None,
                          conversation_id: str | None = None) -> dict:
        """通过带 growthEvent 的模型请求触发任务进度。

        发的是一个「完全合法」的请求（正常响应 200），避免在上游留下
        异常日志：真实模型 + max_tokens=1，只取最小输出。
        默认用免费 0 倍率模型（hy3），成本为零。

        Args:
            event_codes: 事件名列表，如 ["chat_request_send"]。
            model: 使用的模型，默认免费模型。
            event_id: 事件 id（会写入 growthEvent）。
            conversation_id: 会话 id，用于服务端去重与归因。
        """
        # 会话 id 必须每个账号、每次调用都不同。
        # 之前这里是一个硬编码的假 UUID，导致所有账号共用同一个会话 id ——
        # 这种「多账号同会话」是很容易被批量识别的特征，也影响服务端归因。
        conv = conversation_id or event_id or str(uuid.uuid4())
        events = [{"eventCode": c, "id": conv} for c in event_codes]
        body = {
            "model": model,
            "stream": True,
            "max_tokens": 1,  # 最小输出：只为触发记账，不需要真实内容
            "messages": [{"role": "user", "content": "hi"}],
            "extra_vars": {"growthEvent": json.dumps(events, ensure_ascii=False)},
        }
        headers = self.cm.get_headers()
        headers["Content-Type"] = "application/json"
        try:
            with httpx.Client(timeout=60, limits=HTTP_LIMITS) as c:
                with c.stream("POST", f"{BACKEND}/v2/chat/completions",
                              headers=headers, json=body) as r:
                    status = r.status_code
                    # 读完（或读到足够判断的量）后关闭，避免连接悬挂
                    n = 0
                    for _ in r.iter_lines():
                        n += 1
                        if n > 50:
                            break
        except Exception as e:
            return {"ok": False, "status": 0, "msg": f"网络失败: {e}"}
        return {"ok": status == 200, "status": status,
                "msg": "" if status == 200 else f"HTTP {status}"}

    # ------------------------------------------------------------------
    # 事件上报（POST /v2/report）
    # ------------------------------------------------------------------
    # 有一部分成长任务不吃 chat/completions 的 growthEvent，而是要求客户端
    # 上报**真实业务事件**。这类事件必须带上与对应客户端一致的指纹头，
    # 否则要么不计数，要么被当成异常客户端。
    #
    # 三个域各有一套形状，不能混用：
    #   billing (codebuddy.cn)     常规业务事件（灵感案例等）
    #   chat    (copilot.tencent.com) 桌面端事件
    #   web     (workbuddy.cn)     浏览器行为（资料库等）
    # ------------------------------------------------------------------

    def _sess_auth(self) -> dict:
        return (self.cm._session() or {}).get("auth") or {}

    def _sess_acct(self) -> dict:
        return (self.cm._session() or {}).get("account") or {}

    def _uid(self) -> str:
        return str(self._sess_acct().get("uid") or "")

    def _nick(self) -> str:
        return str(self._sess_acct().get("nickname") or "")

    def _domain(self) -> str:
        return self._sess_auth().get("domain") or "copilot.tencent.com"

    def _billing_headers(self) -> dict:
        """billing 域头：CLI 形状。"""
        return {
            "Authorization": "Bearer " + (self._sess_auth().get("accessToken") or ""),
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "CLI/2.63.2 CodeBuddy/2.63.2",
            "Origin": BILL_BASE,
            "Referer": BILL_BASE + "/",
            "X-User-Id": self._uid(),
            "X-Domain": self._domain(),
        }

    def _web_headers(self, page_url: str) -> dict:
        """web 域头：浏览器形状。

        X-Domain 必须显式覆盖成 web 域：auth 里的 domain 可能是
        copilot.tencent.com，发往 www.workbuddy.cn 会造成跨域不一致。
        """
        return {
            "Authorization": "Bearer " + (self._sess_auth().get("accessToken") or ""),
            "Accept": "application/json",
            "Content-Type": "application/json",
            "x-client-platform": "web",
            "Origin": WEB_BASE,
            "Referer": page_url,
            "User-Agent": WEB_UA,
            "X-User-Id": self._uid(),
            "X-Domain": WEB_BASE,
        }

    def report_billing_event(self, events: list[dict]) -> dict:
        """向 billing 域上报业务事件。"""
        uid = self._uid()
        arr = []
        for e in events:
            m = dict(e)
            m.setdefault("userId", uid)
            arr.append(m)
        return self._post_report(BILL_BASE + "/v2/report",
                                 self._billing_headers(), arr)

    def report_web_event(self, event_code: str, page_url: str,
                         element_id: str, element_name: str) -> dict:
        """向 web 域上报一次浏览器元素点击（资料库任务用）。"""
        now = int(time.time() * 1000)
        uid = self._uid()
        ev = {
            "eventCode": event_code, "timestamp": now, "reportDelay": 0,
            "pageURL": page_url, "elementId": element_id,
            "elementName": element_name,
            "os": "Win32", "arch": "", "osVersion": "10.0", "userAgent": WEB_UA,
            "machineId": stable_device_id(uid, "webmachine"),
            "userId": uid, "userNickname": self._nick(),
        }
        return self._post_report(WEB_BASE + "/v2/report",
                                 self._web_headers(page_url), [ev])

    def _post_report(self, url: str, headers: dict, events: list[dict]) -> dict:
        """上报事件；返回 {ok, status, code, msg}，不抛异常。"""
        try:
            with httpx.Client(timeout=20, limits=HTTP_LIMITS) as c:
                r = c.post(url, headers=headers, json=events)
        except Exception as e:
            return {"ok": False, "status": 0, "code": None,
                    "msg": f"网络失败: {e}"}
        try:
            payload = r.json()
        except Exception:
            payload = {}
        code = payload.get("code") if isinstance(payload, dict) else None
        ok = r.status_code == 200 and code == 0
        return {"ok": ok, "status": r.status_code, "code": code,
                "msg": "" if ok else f"HTTP {r.status_code} code={code}"}

    def fire_playbook_prompt(self) -> dict:
        """灵感案例任务：上报一次「使用官方案例提示词」。

        事件必须带齐 skills/expertId 等业务字段——上游按内容判断是不是
        真实使用案例，只发一个空壳事件不计数。
        """
        now = int(time.time() * 1000)
        uid = self._uid()
        cid = str(uuid.uuid4())
        ev = {
            "eventCode": "playbook_prompt_send", "timestamp": now,
            "reportDelay": 0, "id": f"pb-{now}", "name": "playbook",
            "type": "other", "promptLength": 12, "isOfficial": 1,
            "skills": "", "skillNames": "", "expertId": "", "expertName": "",
            "categoryId": "", "categoryName": "", "query": "",
            "source": "discover", "conversationId": cid,
            "requestId": f"{cid}-{now}", "ext1": "discover", "userId": uid,
        }
        return self.report_billing_event([ev])

    def fire_library_read(self) -> dict:
        """资料库任务：上报一次资料库介绍页的点击。"""
        return self.report_web_event(
            "web_element_click", LIBRARY_DOC_URL,
            "library_doc_intro_click", "WorkBuddy资料库介绍")

    def _chat_headers(self) -> dict:
        """chat 域头：市场/场景等接口用（CLI 形状，X-Domain 走账号自己的域）。"""
        return {
            "Authorization": "Bearer " + (self._sess_auth().get("accessToken") or ""),
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "CLI/2.63.2 CodeBuddy/2.63.2",
            "Origin": BILL_BASE,
            "Referer": BILL_BASE + "/",
            "X-User-Id": self._uid(),
            "X-Domain": self._domain(),
        }

    def _chat_json(self, path: str, body: dict | None = None) -> dict:
        """向 chat 域发请求并返回 data 段；失败返回 {}。"""
        url = BACKEND.rstrip("/") + path
        try:
            with httpx.Client(timeout=25, limits=HTTP_LIMITS) as c:
                if body is None:
                    r = c.get(url, headers=self._chat_headers())
                else:
                    r = c.post(url, headers=self._chat_headers(), json=body)
            if r.status_code != 200:
                return {}
            return (r.json() or {}).get("data") or {}
        except Exception:
            return {}

    # ------------------------------------------------------------------
    # 真实对象来源（全部只读；绝不编造 id）
    # ------------------------------------------------------------------
    # 这些任务要求事件里带「真实存在的对象 id」。id 一律从官方接口现拉，
    # 拉不到就放弃该任务，绝不退化成自造 id —— 伪造业务对象一旦被后端
    # 核对就会暴露。
    # ------------------------------------------------------------------

    def _next_index(self, n: int) -> int:
        """返回 0..n-1 的轮转下标，每次调用递增。

        同一个任务要跑 N 次，必须每次用**不同的**对象 id ——
        服务端按 id 去重，重复报同一个 id 只算一次
        （实测 team 任务连报 3 次同一个专家，进度只 +2）。
        用实例计数器而不是时间片，保证连续调用一定取到不同值。
        """
        if n <= 0:
            return 0
        i = getattr(self, "_rotor", 0)
        self._rotor = i + 1
        return i % n

    def fetch_experts(self, team_only: bool = False,
                      keyword: str | None = None, limit: int = 6) -> list[dict]:
        """真实专家列表（市场接口）。team_only 只取专家团。"""
        body: dict = {"page": 1, "page_size": 50}
        if team_only:
            # 市场支持按 expert_type 过滤；不带该参数时 400 个专家里只有 1 个 team
            body["expert_type"] = "team"
        if keyword:
            body["keyword"] = keyword
        data = self._chat_json("/v2/operation-platform/market/expert/list", body)
        out = []
        for e in data.get("experts") or []:
            eid = e.get("expert_id") or e.get("source_id")
            if not eid:
                continue
            etype = e.get("expert_type") or "agent"
            if team_only and etype != "team":
                continue
            out.append({
                "id": eid, "expertType": etype,
                "name": e.get("display_name_zh") or e.get("profession_zh") or eid,
                "category": (e.get("categories") or [""])[0] or "",
                "version": e.get("version") or "",
            })
            if len(out) >= limit:
                break
        return out

    def fetch_scenes(self, limit: int = 6) -> list[dict]:
        """真实场景（模板）列表。"""
        data = self._chat_json("/console/as/support/scenes?locale=zh-CN")
        out = []
        for s in data.get("scenes") or []:
            if s.get("id") is None:
                continue
            out.append({"id": str(s["id"]), "name": s.get("name") or ""})
            if len(out) >= limit:
                break
        return out

    def fetch_appearance_themes(self, keyword: str = "和平精英") -> list[dict]:
        """真实外观主题资源（走 billing 域）。"""
        url = BILL_BASE + "/v2/operation-platform/appearance/resources"
        body = {"platform": "client", "kind": "theme", "version": "2.63.2",
                "lang": "zh-CN"}
        try:
            with httpx.Client(timeout=25, limits=HTTP_LIMITS) as c:
                r = c.post(url, headers=self._chat_headers(), json=body)
            if r.status_code != 200:
                return []
            data = (r.json() or {}).get("data") or {}
        except Exception:
            return []
        out = []
        for x in data.get("resources") or []:
            nm = x.get("name") or ""
            if keyword and keyword not in nm and keyword.lower() not in nm.lower():
                continue
            if not x.get("id"):
                continue
            out.append({"id": x["id"], "name": nm,
                        "vipLevel": x.get("vip_level") or "free",
                        "series": x.get("series") or "craft"})
        return out

    # ------------------------------------------------------------------
    # 具体任务触发器
    # ------------------------------------------------------------------

    def fire_expert_use(self, team_only: bool = False) -> dict:
        """召唤专家任务：用真实专家 id 上报 expert_actual_use。

        每次调用取一个不同专家（按 uid 轮转，避免永远只报第一个）。
        target 次数由调用方循环完成。
        """
        teams = team_only
        experts = self.fetch_experts(team_only=teams, limit=12)
        if not experts:
            return {"ok": False, "status": 0,
                    "msg": "取不到真实专家 id，跳过（不自造）"}
        e = experts[self._next_index(len(experts))]
        now = int(time.time() * 1000)
        cid = str(uuid.uuid4())
        ev = {
            "eventCode": "expert_actual_use", "timestamp": now, "reportDelay": 0,
            "mode": "CLOUD", "id": e["id"], "name": e["name"],
            "expertTitle": e["name"], "type": e["category"],
            "expertType": "team" if teams else (e["expertType"] or "agent"),
            "source": "builtin", "version": e["version"], "cost": 0,
            "characterCount": 12, "conversationId": cid,
            "requestId": f"{cid}-{now}", "messageId": f"{cid}-{now}",
            "requestModelId": "deepseek-v4-flash",
            "requestModelName": "DeepSeek V4 Flash", "userId": self._uid(),
        }
        return self.report_billing_event([ev])

    def fire_expert_team(self) -> dict:
        """召唤专家团任务：只取 expert_type=team 的真实团队。"""
        return self.fire_expert_use(team_only=True)

    def fire_template_use(self) -> dict:
        """使用模板任务：用真实场景 id 上报 agent_task_created_with_template。"""
        scenes = self.fetch_scenes(limit=12)
        if not scenes:
            return {"ok": False, "status": 0, "msg": "取不到真实场景 id，跳过"}
        s = scenes[self._next_index(len(scenes))]
        now = int(time.time() * 1000)
        cid = str(uuid.uuid4())
        ev = {
            "eventCode": "agent_task_created_with_template", "timestamp": now,
            "reportDelay": 0, "isCustomModel": True, "id": s["id"],
            "name": s["name"], "requestId": f"{cid}-{now}",
            "conversationId": cid, "userId": self._uid(),
        }
        return self.report_billing_event([ev])

    def fire_lighthouse_expert(self) -> dict:
        """轻量云专家任务：关键词筛出真实轻量云专家后上报。"""
        for kw in ("lighthouse", "轻量云"):
            experts = self.fetch_experts(keyword=kw, limit=5)
            for e in experts:
                name = e["name"] or ""
                if any(k in (e["id"] + name).lower()
                       for k in ("lighthouse", "轻量", "light")):
                    now = int(time.time() * 1000)
                    cid = str(uuid.uuid4())
                    ev = {
                        "eventCode": "expert_actual_use", "timestamp": now,
                        "reportDelay": 0, "mode": "CLOUD", "id": e["id"],
                        "name": name, "expertTitle": name, "type": e["category"],
                        "expertType": e["expertType"] or "agent",
                        "source": "builtin", "version": e["version"], "cost": 0,
                        "characterCount": 12, "conversationId": cid,
                        "requestId": f"{cid}-{now}", "messageId": f"{cid}-{now}",
                        "requestModelId": "deepseek-v4-flash",
                        "requestModelName": "DeepSeek V4 Flash",
                        "userId": self._uid(),
                    }
                    return self.report_billing_event([ev])
        return {"ok": False, "status": 0, "msg": "未找到轻量云专家，跳过"}

    def fire_appearance_skin(self) -> dict:
        """和平精英主题任务：用真实主题 resourceKey 上报换肤。"""
        themes = self.fetch_appearance_themes()
        if not themes:
            return {"ok": False, "status": 0, "msg": "取不到真实主题 id，跳过"}
        t = themes[0]
        now = int(time.time() * 1000)
        ev = {
            "eventCode": "appearance_skin_apply", "timestamp": now,
            "reportDelay": 0, "action": "apply", "source": "settings_close",
            "id": t["id"], "vipLevel": t["vipLevel"], "series": t["series"],
            "type": "unknown", "name": t["name"], "userId": self._uid(),
        }
        return self.report_billing_event([ev])

    # ------------------------------------------------------------------
    # 桌面端事件链（走 chat 域 /v2/report + 桌面指纹）
    # ------------------------------------------------------------------
    # 这几个任务（发现应用 / 企鹅教师助手 / 桌面端对话）上游按「桌面客户端
    # 行为」判定。任务说明里写的「需升级到 5.5.3+」是**客户端侧**的门槛，
    # 服务端只认事件本身，实测直接上报事件链即可完成，无需真的装桌面端。
    # 事件必须带桌面指纹，否则不会被识别为桌面端来源。
    # ------------------------------------------------------------------

    def _desktop_headers(self) -> dict:
        """chat 域桌面指纹请求头。"""
        return {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json;charset=UTF-8",
            "User-Agent": DESKTOP_UA,
            "X-Domain": self._domain(),
            "X-Product": "SaaS",
            "X-Request-ID": stable_device_id(self._uid(), "req")
                            + str(time.time_ns() % 1000000),
            "X-User-Id": self._uid(),
            "Authorization": "Bearer " + (self._sess_auth().get("accessToken") or ""),
        }

    def _desktop_fingerprint(self) -> dict:
        """桌面客户端公共指纹（注入每个事件，覆盖同名业务键）。

        machineId / sessionId 由 uid 稳定派生：同一账号每次上报都是同一台
        「设备」，不要每次随机 —— 频繁换设备反而是异常信号。
        """
        uid = self._uid()
        now = int(time.time() * 1000)
        return {
            "timezone": "Asia/Shanghai", "reportDelay": 2000,
            "userId": uid, "username": self._nick(), "userNickname": self._nick(),
            "product": "SaaS", "releaseDate": 1789036585355,
            "commit": "5f9692923c93033111c51ad7b003eb80204a9b75",
            "ideName": "WorkBuddy", "ideType": "WorkBuddy", "ideVersion": "5.5.6",
            "machineId": stable_device_id(uid, "machine"),
            "sessionId": stable_device_id(uid, "session"),
            "extName": "workbuddy-desktop", "extVersion": "5.5.6",
            "os": "win32", "arch": "x64", "osVersion": "10.0.26220",
            "cpuCores": 20, "memorySize": 24,
            "timestamp": now, "presentAt": now,
        }

    def report_desktop_events(self, events: list[dict]) -> dict:
        """向 chat 域批量上报桌面事件（每个事件注入桌面指纹）。"""
        fp = self._desktop_fingerprint()
        arr = []
        for e in events:
            m = dict(e)
            m.update(fp)
            arr.append(m)
        url = BACKEND.rstrip("/") + "/v2/report"
        try:
            with httpx.Client(timeout=25, limits=HTTP_LIMITS) as c:
                r = c.post(url, headers=self._desktop_headers(), json=arr)
        except Exception as e:
            return {"ok": False, "status": 0, "code": None, "msg": f"网络失败: {e}"}
        try:
            payload = r.json()
        except Exception:
            payload = {}
        code = payload.get("code") if isinstance(payload, dict) else None
        ok = r.status_code == 200 and code == 0
        return {"ok": ok, "status": r.status_code, "code": code,
                "msg": "" if ok else f"HTTP {r.status_code} code={code}"}

    def fire_buddy_app(self) -> dict:
        """发现应用 / 企鹅教师助手：上报五连「进入 Buddy 应用」事件。

        一组事件同时满足 Buddy_App 与 Buddy_App_QQ 两个任务。
        """
        bid, bname = BUDDY_QQ_APP
        ev = []

        def mk(code, extra=None):
            e = {"eventCode": code, "mode": "LOCAL", "buddyId": bid,
                 "buddyName": bname}
            if extra:
                e.update(extra)
            ev.append(e)

        mk("buddyapp_discover_click")
        mk("buddyapp_show", {"elementId": bid, "elementName": bname, "position": 2})
        mk("buddyapp_enter_click", {"elementId": bid, "elementName": bname,
                                    "position": 2, "isFirstPage": "1"})
        mk("buddyapp_auth_confirm_click", {"elementId": bid, "elementName": bname})
        mk("buddyapp_bindaccount_skip_click", {"elementId": bid, "elementName": bname})
        return self.report_desktop_events(ev)

    def fire_desktop_chat(self) -> dict:
        """桌面端对话任务：上报 6 连「桌面端成功对话」事件链。"""
        now = int(time.time() * 1000)
        conv = f"wb-run-rm-{now}"
        reqid = f"{conv}-req"
        msgid = f"{conv}-user"
        mid = "fast-model"
        ev = []

        def mk(code, extra):
            e = {"eventCode": code}
            e.update(extra)
            ev.append(e)

        mk("agent_task_created", {
            "source": "LOCAL", "name": "working", "task_target": "local",
            "mode": "craft", "requestModelId": mid, "requestModelName": mid,
            "has_repo": False, "repo_type": "none", "workspace_type": "empty",
            "has_connector": False, "connector_types": [], "has_mention": False,
            "mention_types": [], "has_template": False, "action": "",
            "template_name": "", "has_expert": False, "expert_id": "",
            "expert_name": "", "expert_industry_id": "", "has_skill": False,
            "skill_names": [], "conversationId": conv, "messageId": msgid,
            "buddyId": "", "buddyName": ""})
        mk("chat_message_send", {
            "messageId": msgid + "-assistant", "historyCount": 0,
            "isContextTruncated": False, "currentStepCount": 1, "traceId": reqid,
            "rootRequestId": reqid, "parentConversationId": conv,
            "agentName": "cli", "agentType": "main"})
        mk("chat_request_send", {
            "inputLength": 24, "isPlan": False, "isAutoExecuteTerminal": False,
            "isAutoModify": False, "codebaseEnable": False, "maxToken": 0,
            "maxSteps": 500, "temperature": 0, "maxRetries": 0,
            "mentionContexts": [], "knowledgeId": [], "knowledgeName": [],
            "codebaseId": "", "mentionContextCount": 0, "command": "",
            "recommendId": "", "skillId": "", "skillCount": 0, "totalCount": 0,
            "traceId": reqid, "rootRequestId": reqid,
            "parentConversationId": conv, "agentName": "cli", "agentType": "main",
            "codebuddy.session_id": conv,
            "codebuddy.conversation_request_id": reqid})
        mk("chat_message_response", {
            "messageId": msgid + "-assistant", "responseModelId": mid,
            "inputToken": 120, "outputToken": 80, "totalToken": 200,
            "cachedTokens": 0, "cachedWriteTokens": 0, "cachedMissTokens": 0,
            "isSuccessful": True, "messageErrorCode": "", "finishReason": "stop",
            "firstTokenAt": now, "traceId": reqid, "conversationId": conv,
            "rootRequestId": reqid, "parentConversationId": conv,
            "agentName": "cli", "agentType": "main",
            "codebuddy.session_id": conv,
            "codebuddy.conversation_request_id": reqid})
        mk("chat_message_status", {
            "messageId": msgid + "-assistant", "messageErrorCode": "0",
            "traceId": reqid, "rootRequestId": reqid,
            "parentConversationId": conv, "agentName": "cli", "agentType": "main"})
        mk("chat_request_response", {
            "mode": "craft", "toolCallCount": 0, "inputToken": 120,
            "outputToken": 80, "totalToken": 200, "cachedTokens": 0,
            "cachedWriteTokens": 0, "cachedMissTokens": 0, "isSuccessful": True,
            "messageErrorCode": "", "finishReason": "stop", "rootRequestId": reqid,
            "parentConversationId": conv})
        return self.report_desktop_events(ev)

    def get_token_expiry(self) -> int:
        """返回 token 到期时间戳（毫秒），0 表示未知。"""
        return self._sess_auth().get("expiresAt") or 0

    def updated_json(self) -> str:
        with open(self._path, "r", encoding="utf-8") as f:
            return f.read()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        try:
            os.unlink(self._path)
        except OSError:
            pass


def _map_checkin_status(code: int) -> str:
    """后端签到业务码 → 语义状态。"""
    return {
        1001: "already_claimed",   # 今日已领取
        1002: "not_eligible",      # 无领取资格
        1003: "event_ended",       # 活动已结束
    }.get(code, "unknown")
