"""复用 converter.CredentialManager 操作单个账号的后端会话。

账号凭据以 .info 原文形式存于 MySQL；用时落盘成临时文件交给 CredentialManager，
用完读回（token 可能被刷新），写回 MySQL。
"""
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

import httpx

from converter import BACKEND, CredentialManager  # 复用既有后端鉴权 / 刷新 / 模型 / 额度逻辑

# 连接池：减少 TLS 握手，与 Go 项目 MaxIdleConnsPerHost=20 对齐。
HTTP_LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=20)


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

    def run_cat_travel(self, location_id: int = 4) -> dict:
        """执行一趟猫猫旅行，返回结构化分步结果（供前端逐步提示）。

        步骤语义：
          - adopt  : 无猫时才做（同意协议 → 首次领养），成功 +300 积分
          - depart : 空闲时派出
          - claim  : 到站时领奖，reward 为实发积分

        门槛未达标（first_buddy task not completed yet）不是失败，而是「本次
        无法领养」，标记为 skipped 并说明原因，避免误报为错误。
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
                    "summary": "查询猫档案失败"}

        # ── 2) 无猫则领养 ──────────────────────────────────────────────
        if buddy is None:
            agr = self.buddy_agreement()
            if not agr["ok"]:
                add("agreement", False, f"同意协议失败：{agr['msg'] or agr['status']}")
                return {"ok": False, "credits": credits, "steps": steps,
                        "summary": "同意协议失败"}
            add("agreement", True, "已同意活动协议")

            first = self.buddy_first()
            if first["ok"]:
                # 领养成功即发放 300 积分（上游在 data 里可能回传余额）
                add("adopt", True, "领养成功，已发放 300 积分", reward=300)
            elif self._is_threshold_not_met(first):
                add("adopt", True, "暂不可领养：对话门槛未达标（需先与 WorkBuddy 对话几次）",
                    skipped=True)
                return {"ok": True, "credits": credits, "steps": steps,
                        "summary": "本次无法领养（对话门槛未达标）"}
            else:
                add("adopt", False, f"领养失败：{first['msg'] or first['status']}")
                return {"ok": False, "credits": credits, "steps": steps,
                        "summary": "领养失败"}
        else:
            add("adopt", True, f"已有猫：{buddy.get('name') or buddy.get('id')}", skipped=True)

        # ── 3) 查旅行状态 ──────────────────────────────────────────────
        try:
            st = self.travel_status()
        except Exception as e:
            add("status", False, str(e))
            return {"ok": False, "credits": credits, "steps": steps,
                    "summary": "查询旅行状态失败"}

        state = str(st.get("state") or "").strip()
        record_id = int(st.get("record_id") or 0)

        # ── 4) 到站领奖 / 空闲派出 ─────────────────────────────────────
        if state == "arrived":
            if record_id <= 0:
                add("claim", False, "已到站但缺少 record_id，无法领奖")
                return {"ok": False, "credits": credits, "steps": steps,
                        "summary": "领奖失败（缺少 record_id）"}
            cl = self.travel_claim(record_id)
            if cl["ok"]:
                reward = int((cl["data"] or {}).get("reward_credit") or 0)
                add("claim", True, f"领奖成功，获得 {reward} 积分", reward=reward)
            else:
                add("claim", False, f"领奖失败：{cl['msg'] or cl['status']}")
                return {"ok": False, "credits": credits, "steps": steps,
                        "summary": "领奖失败"}
        elif state == "idle":
            if st.get("daily_limit_reached"):
                add("depart", True, "今日已派出过，明日 00:00 后可再次派出", skipped=True)
            else:
                dp = self.travel_depart(location_id)
                if dp["ok"]:
                    add("depart", True, "已派出猫咪旅行，到站后可领奖")
                else:
                    add("depart", False, f"派出失败：{dp['msg'] or dp['status']}")
                    return {"ok": False, "credits": credits, "steps": steps,
                            "summary": "派出失败"}
        elif state == "traveling":
            add("depart", True, f"猫咪正在旅行中（record={record_id}），到站后可领奖",
                skipped=True)
        else:
            add("status", True, f"未知旅行状态 {state!r}，未执行动作", skipped=True)

        total = sum(s["reward"] for s in steps)
        if total > 0:
            summary = f"完成，共获得 {total} 积分"
        else:
            summary = "完成，本次无新增积分"
        return {"ok": True, "credits": credits, "steps": steps, "summary": summary}

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
        conv = conversation_id or event_id or "00000000-0000-4000-8000-000000000001"
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

    def get_token_expiry(self) -> int:
        """返回 token 到期时间戳（毫秒），0 表示未知。"""
        auth = self.cm._auth or {}
        return auth.get("expiresAt") or 0

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
