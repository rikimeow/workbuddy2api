"""per-key 并发上限：迁移 / CRUD / allow / middleware。"""
import asyncio
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from admin import gateway_limits
from admin.db import Base, get_db
from admin.models import ApiKey
from admin.routers import keys
from admin.security import require_admin


def _memory_db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)


class MigrationTests(unittest.TestCase):
    def test_max_concurrent_column_exists(self):
        engine, _ = _memory_db()
        cols = [c["name"] for c in inspect(engine).get_columns("api_keys")]
        self.assertIn("max_concurrent", cols)


class CrudTests(unittest.TestCase):
    def setUp(self):
        self.engine, Session = _memory_db()
        self.db = Session()

    def tearDown(self):
        self.db.close()

    def _client(self):
        app = FastAPI()
        app.include_router(keys.router)
        app.dependency_overrides[require_admin] = lambda: True
        app.dependency_overrides[get_db] = lambda: self.db
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")

    def test_create_patch_max_concurrent(self):
        async def run():
            async with self._client() as c:
                r = await c.post("/api/keys", json={"name": "a", "max_concurrent": 5})
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.json()["max_concurrent"], 5)
                kid = r.json()["id"]

                r = await c.post("/api/keys", json={"name": "b", "max_concurrent": 0})
                self.assertEqual(r.json()["max_concurrent"], 0)

                r = await c.post("/api/keys", json={"name": "c"})
                self.assertIsNone(r.json()["max_concurrent"])

                r = await c.patch(f"/api/keys/{kid}", json={"max_concurrent": 0})
                self.assertEqual(r.status_code, 200)
                rows = (await c.get("/api/keys")).json()["items"]
                by_id = {x["id"]: x for x in rows}
                self.assertEqual(by_id[kid]["max_concurrent"], 0)

                r = await c.patch(f"/api/keys/{kid}", json={"max_concurrent": None})
                self.assertEqual(r.status_code, 200)
                rows = (await c.get("/api/keys")).json()["items"]
                by_id = {x["id"]: x for x in rows}
                self.assertIsNone(by_id[kid]["max_concurrent"])
        asyncio.run(run())


class AllowTests(unittest.TestCase):
    def setUp(self):
        gateway_limits._windows.clear()
        gateway_limits._active.clear()

    def tearDown(self):
        gateway_limits._windows.clear()
        gateway_limits._active.clear()

    def test_per_key_one_slot(self):
        with patch.object(gateway_limits, "_limits", return_value=(0, 4)):
            a1 = gateway_limits.allow("k1", rate=0, concurrent=1)
            self.assertFalse(isinstance(a1, tuple))
            denied = gateway_limits.allow("k1", rate=0, concurrent=1)
            self.assertIsInstance(denied, tuple)
            self.assertEqual(denied[1], 1)
            gateway_limits.release(a1)
            ok = gateway_limits.allow("k1", rate=0, concurrent=1)
            self.assertFalse(isinstance(ok, tuple))
            gateway_limits.release(ok)

    def test_unlimited_zero(self):
        with patch.object(gateway_limits, "_limits", return_value=(0, 1)):
            holds = []
            for _ in range(8):
                r = gateway_limits.allow("k0", rate=0, concurrent=0)
                self.assertFalse(isinstance(r, tuple), r)
                holds.append(r)
            for h in holds:
                gateway_limits.release(h)

    def test_none_follows_global(self):
        with patch.object(gateway_limits, "_limits", return_value=(0, 1)):
            a1 = gateway_limits.allow("kN")  # concurrent=None → 全局 1
            self.assertFalse(isinstance(a1, tuple))
            denied = gateway_limits.allow("kN")
            self.assertIsInstance(denied, tuple)
            gateway_limits.release(a1)

    def test_key_admission_params(self):
        engine, Session = _memory_db()
        session = Session()
        k = ApiKey(key_hash="h1", key_prefix="sk-test", max_concurrent=0)
        session.add(k)
        session.commit()
        kid = k.id
        session.close()

        def fake_get_key_row(db, token):
            if token == "t-unlimited":
                s = Session()
                row = s.query(ApiKey).get(kid)
                return row
            if token == "t-null":
                s = Session()
                row = s.query(ApiKey).filter(ApiKey.key_hash == "h1").first()
                # simulate max_concurrent=None
                row.max_concurrent = None
                s.commit()
                return row
            return None

        with patch("admin.security.get_key_row", side_effect=fake_get_key_row), \
             patch.object(gateway_limits, "_limits", return_value=(60, 4)):
            # monkeypatch import inside function: patch admin.security.get_key_row is enough
            key_id, rate, conc = gateway_limits._key_admission_params("t-unlimited")
            self.assertEqual(key_id, str(kid))
            self.assertEqual(rate, 60)
            self.assertEqual(conc, 0)


class MiddlewareTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        gateway_limits._windows.clear()
        gateway_limits._active.clear()

    def tearDown(self):
        gateway_limits._windows.clear()
        gateway_limits._active.clear()

    async def test_429_when_per_key_exhausted(self):
        async def app(scope, receive, send):
            from starlette.responses import PlainTextResponse
            await PlainTextResponse("ok")(scope, receive, send)

        mw = gateway_limits.GatewayAdmissionMiddleware(app)
        calls = {"n": 0}

        def fake_params(token):
            calls["n"] += 1
            return "k9", 0, 1

        def fake_allow(key, rate=None, concurrent=None):
            if calls.get("held"):
                return None, 2
            calls["held"] = True
            return gateway_limits.Admission(key)

        async def run_one():
            scope = {
                "type": "http",
                "path": "/v1/messages",
                "headers": [(b"x-api-key", b"tok")],
                "method": "POST",
            }

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            sent = []

            async def send(message):
                sent.append(message)

            return scope, receive, send, sent

        with patch.object(gateway_limits, "_key_admission_params", side_effect=fake_params), \
             patch.object(gateway_limits, "allow", side_effect=fake_allow), \
             patch.object(gateway_limits, "release"):
            scope, receive, send, sent = await run_one()
            await mw(scope, receive, send)
            # first request succeeds (held=False → Admission)
            self.assertTrue(any(m.get("type") == "http.response.start" and m["status"] == 200 for m in sent))

            calls["held"] = True  # force deny
            scope, receive, send, sent = await run_one()
            await mw(scope, receive, send)
            starts = [m for m in sent if m.get("type") == "http.response.start"]
            self.assertEqual(starts[0]["status"], 429)


if __name__ == "__main__":
    unittest.main()
