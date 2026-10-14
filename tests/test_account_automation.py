"""Offline regression tests with synthetic accounts and an isolated SQLite DB."""
import json
import atexit
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from datetime import datetime, timezone

_work = tempfile.TemporaryDirectory(prefix='wb-automation-test-')
os.environ['ADMIN_DATABASE_URL'] = 'sqlite:///' + (Path(_work.name) / 'test.db').as_posix()
os.environ['ADMIN_GATEWAY_LIMIT_BACKEND'] = 'memory'
os.environ['ADMIN_REDIS_URL'] = ''
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from sqlalchemy import Column, MetaData, Table, text
from admin import db as db_module
from admin.db import Base, engine, SessionLocal
from admin.models import Account, ApiKey, UsageLog, SystemSetting
from admin import backend, post_login
from admin.routers import growth, credits, proxy, logs, login

Base.metadata.create_all(engine)
atexit.register(engine.dispose)


class DatabaseTest(unittest.TestCase):
    def setUp(self):
        with SessionLocal() as db:
            for model in (UsageLog, Account, ApiKey, SystemSetting):
                db.query(model).delete()
            db.commit()

    def account(self, name='offline', **extra):
        with SessionLocal() as db:
            acc = Account(name=name, uid=name, auth_json='{}', status='active', **extra)
            db.add(acc)
            db.commit()
            return acc.id


class ExpiryTests(DatabaseTest):
    def test_sorting_preserves_package_owners_and_pagination(self):
        now = 1800000000000
        for name, days in [('later', 10), ('urgent', 1), ('middle', 4)]:
            expiry = now + days * 86400000
            snap = {'resources': [{'package_code': name, 'display_name': name,
                                  'remaining': 100, 'expire_at': expiry, 'expiring_soon': days <= 7}]}
            self.account(name, credits_snapshot=json.dumps(snap), credits_expiring_soon=100 if days <= 7 else 0,
                         credits_soonest_expire_at=datetime.fromtimestamp(expiry / 1000, timezone.utc).replace(tzinfo=None))
        with SessionLocal() as db, patch.object(credits.credit_rules, 'now_ms', return_value=now):
            first = credits.credit_expiry_stats(db=db, package_limit=2)
            second = credits.credit_expiry_stats(db=db, package_limit=2, package_offset=2)
        self.assertEqual(first['accounts'][0]['account_name'], 'urgent')
        packages = first['packages'] + second['packages']
        self.assertEqual([p['account_name'] for p in packages], ['urgent', 'middle', 'later'])
        self.assertTrue(all(p['account_name'] == p['package_code'] for p in packages))
        self.assertEqual(credits._ms(datetime(2026, 1, 1)), 1767225600000)


class FakeSession:
    def __init__(self, tasks):
        self.tasks = tasks
        self.calls = {'list': 0, 'accept': [], 'fire': 0, 'claim': []}
        self.fail = False

    def __enter__(self): return self
    def __exit__(self, *args): pass
    def growth_tasks(self):
        self.calls['list'] += 1
        return json.loads(json.dumps(self.tasks))
    def growth_accept(self, codes):
        self.calls['accept'].append(codes)
        for t in self.tasks:
            if t['task_code'] in codes:
                t['accept_status'] = 'in_progress'
        return {c: 'accepted' for c in codes}
    def growth_fire_event(self, events, model):
        self.calls['fire'] += 1
        if self.fail:
            return {'ok': False, 'msg': 'timeout'}
        for t in self.tasks:
            if t['accept_status'] == 'in_progress':
                t['accept_status'] = 'completed'
        return {'ok': True}
    def growth_claim(self, code):
        self.calls['claim'].append(code)
        for t in self.tasks:
            if t['task_code'] == code:
                t['accept_status'] = 'claimed'
        return {'ok': True, 'data': {'credit': 100}}
    def fetch_balance(self): return {'total': 1000, 'remain': 1000}
    def updated_json(self): return '{}'
    def last_accept_message(self, code): return 'blocked'


def task(code, status='not_accepted', target=1):
    return {'task_code': code, 'accept_status': status, 'reward_credit': 100,
            'progress': {'target': target, 'current': 0}}


class GrowthTests(DatabaseTest):
    def run_fake(self, tasks, codes=None, fail=False):
        aid = self.account()
        s = FakeSession(tasks)
        s.fail = fail
        starts = []
        with SessionLocal() as db, patch.object(growth.backend, 'AccountSession', return_value=s), \
             patch.object(growth, '_pick_free_model', return_value='offline-model'), \
             patch.object(growth, '_EVENT_GAP', 0), patch.object(growth, '_POLL_INTERVAL', .001), \
             patch.object(growth, '_CLAIM_WAIT', .005), patch.object(growth, '_ACCEPT_TIMEOUT', .005):
            result = growth.run_accounts([aid], codes, db, on_task_start=lambda name, item: starts.append(item['title']))
        return result['results'][0], s, starts

    def test_batch_accept_verify_claim_and_persist_balance(self):
        result, s, starts = self.run_fake([task('skill_1'), task('automation_1')])
        self.assertTrue(result['ok'], result)
        self.assertEqual(len(s.calls['accept']), 1)
        self.assertLessEqual(s.calls['list'], 3)
        self.assertEqual(result['credit'], 200)
        self.assertEqual(len(s.calls['claim']), 2)
        self.assertIn('核对任务进度与领取奖励', starts)
        with SessionLocal() as db:
            self.assertEqual(db.query(Account).first().balance_remain, 1000)

    def test_claim_only_has_no_unbound_state_or_event(self):
        result, s, _ = self.run_fake([task('skill_1', 'completed')])
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['credit'], 100)
        self.assertEqual(s.calls['fire'], 0)

    def test_explicit_completed_task_not_executed_again(self):
        result, s, _ = self.run_fake([task('skill_1', 'completed')], ['skill_1'])
        self.assertEqual(s.calls['fire'], 0)
        self.assertEqual(result['credit'], 100)

    def test_failed_event_is_not_retried_ten_times_or_marked_success(self):
        result, s, _ = self.run_fake([task('chat_10', 'in_progress', 10)], fail=True)
        self.assertEqual(s.calls['fire'], 1)
        self.assertFalse(result['ok'])
        self.assertFalse(result['tasks'][0]['ok'])
        self.assertEqual(s.calls['claim'], [])

    def test_failed_accept_never_fires(self):
        with patch.object(FakeSession, 'growth_accept', return_value={'skill_1': 'error'}):
            result, s, _ = self.run_fake([task('skill_1')])
        self.assertFalse(result['ok'])
        self.assertEqual(s.calls['fire'], 0)

    def test_missing_pet_is_adopted_before_accept(self):
        def adopt(s):
            s.tasks[0]['accept_status'] = 'claimed'
            return {'ok': True, 'summary': 'adopted'}
        with patch.object(FakeSession, 'run_cat_travel', adopt, create=True):
            result, s, starts = self.run_fake([task('first_buddy'), task('skill_1')])
        self.assertTrue(result['buddy_gate']['ok'])
        self.assertEqual(s.calls['accept'][0], ['skill_1'])


class QueueTests(DatabaseTest):
    def test_full_workers_do_not_drop_accounts_and_duplicates_coalesce(self):
        ids = [self.account('offline-' + str(i)) for i in range(5)]
        pending = []
        try:
            with patch.object(post_login, '_submit', side_effect=lambda fn, *args: pending.append((fn, args))):
                for aid in ids:
                    post_login.enqueue(aid, 'offline')
                post_login.enqueue(ids[0], 'offline')
            self.assertEqual(len(pending), 5)
            with SessionLocal() as db:
                self.assertEqual(len(post_login.states(db, ids)), 5)
            with patch.object(login, '_run_post_login_automation', return_value={'errors': [], 'growth_credits': 100}):
                for fn, args in pending:
                    fn(*args)
            with SessionLocal() as db:
                self.assertTrue(all(s['status'] == 'completed' for s in post_login.states(db, ids).values()))
        finally:
            post_login._pending.clear()

    def test_restart_resumes_queued_and_running_accounts(self):
        ids = [self.account('offline-' + str(i)) for i in range(3)]
        for aid, state in zip(ids, ['queued', 'running', 'completed']):
            post_login._save(aid, state)
        with patch.object(post_login, 'enqueue') as enqueue:
            post_login.resume()
        self.assertEqual({call.args[0] for call in enqueue.call_args_list}, set(ids[:2]))

    def test_worker_records_failures(self):
        aid = self.account()
        with patch.object(login, '_run_post_login_automation', return_value={'errors': ['upstream unavailable']}):
            post_login._worker(aid, 'offline')
        with SessionLocal() as db:
            self.assertEqual(post_login.states(db, [aid])[aid]['status'], 'failed')


class KeepaliveStream(httpx.SyncByteStream):
    def __init__(self, content): self.content, self.closed = content, False
    def __iter__(self):
        yield self.content
        raise AssertionError('Waited for EOF after the completed RPC/SSE result')
    def close(self): self.closed = True


class BackendTests(unittest.TestCase):
    def test_pet_gate_waits_for_delayed_progress_without_refiring(self):
        with backend.AccountSession('{}') as s, \
             patch.object(s, 'growth_tasks', side_effect=[
                 [task('first_buddy')], [task('first_buddy', 'in_progress')],
                 [task('first_buddy', 'completed')]]), \
             patch.object(s, 'growth_fire_event', return_value={'ok': True}) as fire, \
             patch.object(backend.time, 'sleep'):
            self.assertTrue(s._clear_buddy_gate()[0])
        fire.assert_called_once()

    def test_finished_pet_gate_does_not_send_another_chat(self):
        with backend.AccountSession('{}') as s, \
             patch.object(s, 'growth_tasks', return_value=[task('first_buddy', 'claimed')]), \
             patch.object(s, 'growth_fire_event') as fire:
            self.assertTrue(s._clear_buddy_gate()[0])
        fire.assert_not_called()

    def session(self, handler):
        s = backend.AccountSession('{}')
        s._http = httpx.Client(transport=httpx.MockTransport(handler))
        return s

    def test_mcp_returns_matching_result_without_waiting_for_eof(self):
        stream = KeepaliveStream(b'data: {"jsonrpc":"2.0","method":"notification"}\n\ndata: {"jsonrpc":"2.0","id":2,"result":{"fileId":"123"}}\n\n')
        with self.session(lambda req: httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream)) as s:
            result = s._ardot_mcp_call('offline-token', 'tools/call', rid=2)
        self.assertEqual(result['result']['fileId'], '123')
        self.assertTrue(stream.closed)

    def test_growth_stops_at_finish_instead_of_waiting_for_more_lines(self):
        stream = KeepaliveStream(b'data: {"choices":[{"finish_reason":"stop"}]}\n\n')
        with self.session(lambda req: httpx.Response(200, stream=stream)) as s, patch.object(s.cm, 'get_headers', return_value={}):
            self.assertTrue(s.growth_fire_event(['offline-event'])['ok'])
        self.assertTrue(stream.closed)

    def test_http_200_error_is_not_success(self):
        stream = KeepaliveStream(b'data: {"error":{"message":"offline-error"}}\n\n')
        with self.session(lambda req: httpx.Response(200, stream=stream)) as s, patch.object(s.cm, 'get_headers', return_value={}):
            self.assertFalse(s.growth_fire_event(['offline-event'])['ok'])

    def test_empty_http_200_is_not_a_completed_task_chat(self):
        with self.session(lambda req: httpx.Response(200, content=b'')) as s, patch.object(s.cm, 'get_headers', return_value={}):
            self.assertFalse(s.growth_fire_event(['offline-event'])['ok'])

    def test_json_mcp_response_checks_rpc_identity(self):
        with self.session(lambda req: httpx.Response(200, json={'jsonrpc': '2.0', 'id': 1, 'result': {}})) as s:
            self.assertEqual(s._ardot_mcp_call('offline', 'initialize')['result'], {})
            with self.assertRaises(RuntimeError):
                s._ardot_mcp_call('offline', 'initialize', rid=2)


class LogTests(DatabaseTest):
    def test_existing_database_migrates_without_losing_logs(self):
        with SessionLocal() as db:
            db.add(UsageLog(api_key_id=0, account_id=0, model='offline-old'))
            db.commit()
        # Recreate the synthetic legacy schema; older production SQLite builds
        # support ADD COLUMN but cannot DROP COLUMN, which this fixture must avoid.
        columns = [c for c in UsageLog.__table__.columns if c.name != 'reasoning_effort']
        legacy = Table('usage_logs_old_fixture', MetaData(), *[
            Column(c.name, c.type, primary_key=c.primary_key, nullable=c.nullable) for c in columns])
        legacy.create(engine)
        names = ', '.join(c.name for c in columns)
        with engine.begin() as conn:
            conn.execute(text(f'INSERT INTO usage_logs_old_fixture ({names}) SELECT {names} FROM usage_logs'))
            conn.execute(text('DROP TABLE usage_logs'))
            conn.execute(text('ALTER TABLE usage_logs_old_fixture RENAME TO usage_logs'))
        self.assertTrue(db_module.init_db())
        self.assertTrue(db_module.init_db())
        with SessionLocal() as db:
            row = db.query(UsageLog).filter(UsageLog.model == 'offline-old').one()
            self.assertEqual(row.reasoning_effort, '')

    def test_effort_persisted_and_returned_with_actual_usage(self):
        aid = self.account()
        with SessionLocal() as db:
            key = ApiKey(name='offline', key_hash='offline-key-hash')
            db.add(key)
            db.commit()
            kid = key.id
        logid = proxy._record_usage(kid, aid, 'offline-model', 1.0, None, reasoning_effort='xhigh',
                                   prompt_tokens=10, completion_tokens=2, total_tokens=12)
        with SessionLocal() as db:
            row = db.query(UsageLog).filter(UsageLog.id == logid).one()
            item = logs._serialize([row], {}, {})[0]
        self.assertEqual(item['reasoning_effort'], 'xhigh')
        self.assertEqual(item['total_tokens'], 12)

    def test_all_protocol_efforts_and_no_payload_leak(self):
        self.assertEqual(proxy._reasoning_effort({'reasoning_effort': 'high'}), 'high')
        self.assertEqual(proxy._reasoning_effort({'reasoning': {'effort': 'xhigh'}}), 'xhigh')
        self.assertEqual(proxy._reasoning_effort({'thinking': {'type': 'disabled'}}), 'off')
        self.assertEqual(proxy._reasoning_effort({'thinking': {'budget_tokens': 4096}}), 'budget:4096')
        self.assertEqual(proxy._reasoning_effort({'reasoning_effort': 'secret-prompt'}), '')


if __name__ == '__main__':
    unittest.main()
