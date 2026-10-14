"""Official model capacity observations, cached independently of account cooldowns."""
from datetime import datetime
import logging
import threading
import time

LABELS = {"idle": "空闲", "normal": "空闲", "busy": "轻微拥堵",
          "congested": "极度拥堵", "unavailable": "不可用", "unknown": "未知"}
CACHE_SECONDS = 50
_lock = threading.Lock()
_cache = {}
_logger = logging.getLogger(__name__)


def normalize_response(models, response):
    if not isinstance(response, dict) or response.get("code") != 0 or not isinstance(response.get("data"), list):
        raise ValueError("Invalid model busy response")
    levels = {model: "unknown" for model in models}
    for item in response["data"]:
        if isinstance(item, dict) and isinstance(item.get("model"), str) and item["model"] in levels:
            value = item.get("busy_level")
            levels[item["model"]] = value if isinstance(value, str) and value in LABELS else "unknown"
    return levels


def get_busy_levels(db, models):
    from admin import backend
    from admin.models import Account
    from admin.account_binding import scope_accounts
    from sqlalchemy import or_

    models = sorted(set(models))
    now = datetime.utcnow()
    account = scope_accounts(db.query(Account)).filter(Account.status == "active", *[
        or_(getattr(Account, field).is_(None), getattr(Account, field) <= now)
        for field in ("cool_until", "breaker_until", "degrade_until")
    ]).order_by(Account.id).first()
    unknown = {m: "unknown" for m in models}
    if not models or not account:
        return {"levels": unknown, "observed_at": None, "stale": True}
    # Cache is scoped to the upstream identity and the requested model set.
    key = (account.id, account.uid, tuple(models))
    with _lock:
        cached = _cache.get(key)
        if cached and time.monotonic() - cached["checked"] < CACHE_SECONDS:
            return {k: v for k, v in cached.items() if k != "checked"}
        levels = {}
        try:
            with backend.AccountSession(account.auth_json) as session:
                try:
                    for offset in range(0, len(models), 50):
                        batch = models[offset:offset + 50]
                        levels.update(normalize_response(batch, session.fetch_model_busy(batch)))
                finally:
                    account.auth_json = session.updated_json()
            db.commit()
            result = {"levels": levels, "observed_at": datetime.utcnow().isoformat() + "Z", "stale": False}
        except Exception:
            db.rollback()
            _logger.warning("上游模型繁忙程度获取失败，使用缓存或未知状态")
            result = {"levels": cached["levels"] if cached else unknown,
                      "observed_at": cached["observed_at"] if cached else None, "stale": True}
        # Negative caching also prevents repeated failed requests from every admin view.
        _cache.clear()
        _cache[key] = {**result, "checked": time.monotonic()}
        return result
