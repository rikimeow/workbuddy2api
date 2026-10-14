"""Persist account IDs awaiting onboarding; run at most two workers."""
import json
import logging
import queue
import threading
from datetime import datetime

from admin.db import SessionLocal
from admin.models import Account, SystemSetting

_PREFIX = 'post_login_automation:'
_queue = queue.Queue()
_workers = []
_lock = threading.Lock()
_pending = set()
_log = logging.getLogger(__name__)


def _dispatch():
    while True:
        fn, args = _queue.get()
        try:
            fn(*args)
        except Exception:
            _log.exception('自动任务队列执行失败')
        finally:
            _queue.task_done()


def _submit(fn, *args):
    # Called under _lock. Daemon workers do not delay desktop shutdown;
    # persistent queued/running states are recovered at the next startup.
    while len(_workers) < 2:
        worker = threading.Thread(target=_dispatch, daemon=True,
                                  name='wb-post-login-' + str(len(_workers)))
        worker.start()
        _workers.append(worker)
    _queue.put((fn, args))


def _save(account_id, status, result=None):
    with SessionLocal() as db:
        key = _PREFIX + str(account_id)
        row = db.query(SystemSetting).filter(SystemSetting.key == key).first()
        if row is None:
            row = SystemSetting(key=key)
            db.add(row)
        row.value = json.dumps({'status': status, 'updated_at': datetime.utcnow().isoformat(),
                                'result': result}, ensure_ascii=False)
        db.commit()


def _worker(account_id, name):
    try:
        _save(account_id, 'running')
        from admin.routers.login import _run_post_login_automation
        result = _run_post_login_automation(account_id, name)
        _save(account_id, 'failed' if result and result.get('errors') else 'completed', result)
    except Exception:
        _log.exception('登录后自动任务失败 account_id=%s', account_id)
        try:
            _save(account_id, 'failed', {'errors': ['自动任务执行异常，可重新执行成长任务']})
        except Exception:
            _log.exception('自动任务状态保存失败 account_id=%s', account_id)
    finally:
        with _lock:
            _pending.discard(account_id)


def enqueue(account_id, name):
    with _lock:
        if account_id in _pending:
            return
        # Persist before submitting: a restart must not silently lose queued accounts.
        _save(account_id, 'queued')
        _pending.add(account_id)
        try:
            _submit(_worker, account_id, name)
        except Exception:
            _pending.discard(account_id)
            raise


def resume():
    """Called once after database initialization; rewards are claimed idempotently."""
    with SessionLocal() as db:
        rows = db.query(SystemSetting).filter(SystemSetting.key.like(_PREFIX + '%')).all()
        todo = []
        for row in rows:
            try:
                state = json.loads(row.value)
                account_id = int(row.key[len(_PREFIX):])
            except (ValueError, TypeError):
                continue
            if not isinstance(state, dict):
                continue
            if state.get('status') not in ('queued', 'running'):
                continue
            acc = db.query(Account).filter(Account.id == account_id, Account.status == 'active').first()
            if acc:
                todo.append((acc.id, acc.name))
    for account_id, name in todo:
        enqueue(account_id, name)


def states(db, account_ids):
    keys = [_PREFIX + str(aid) for aid in account_ids]
    if not keys:
        return {}
    out = {}
    for row in db.query(SystemSetting).filter(SystemSetting.key.in_(keys)).all():
        try:
            value = json.loads(row.value)
            if isinstance(value, dict):
                out[int(row.key[len(_PREFIX):])] = value
        except (ValueError, TypeError):
            pass
    return out
