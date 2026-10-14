"""Synthetic encrypted credentials only; no real accounts or desktop keys."""
import base64
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import FastAPI
import httpx
import jwt
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from admin import credential_codec as codec, wb_login
from admin.db import Base, get_db
from admin.models import Account
from admin.routers import accounts
from admin.security import require_admin

KEY = hashlib.sha256(b'synthetic-offline-test-key').digest()
KEY_ID = hashlib.sha256(KEY).hexdigest()[:16]


def encrypted(text):
    nonce = bytes(range(12))
    payload = AESGCM(KEY).encrypt(nonce, text.encode(), codec._aad(KEY_ID))
    envelope = {'suite': 1, 'keyId': KEY_ID,
                'nonce': base64.b64encode(nonce).decode(),
                'authTag': base64.b64encode(payload[-16:]).decode(),
                'ciphertext': base64.b64encode(payload[:-16]).decode()}
    return {'$wbEncrypted': 1, 'envelope': base64.b64encode(json.dumps(envelope).encode()).decode()}


def fixture():
    plain = {'auth': {'accessToken': jwt.encode({'sub': 'offline-user', 'exp': 4102444800}, 'synthetic-test-signing-key-with-32-bytes'),
                      'refreshToken': 'offline-refresh', 'domain': 'example.test'},
             'account': {'uid': 'offline-user', 'nickname': '测试昵称', 'phoneNumber': 'test-phone'},
             'accounts': [{'nickname': '另一个昵称'}]}
    protected = json.loads(json.dumps(plain))
    for area, names in [('auth', ['accessToken', 'refreshToken']), ('account', ['nickname', 'phoneNumber'])]:
        for name in names:
            protected[area][name] = encrypted(plain[area][name])
    protected['accounts'][0]['nickname'] = encrypted('另一个昵称')
    return plain, protected


class CodecTests(unittest.TestCase):
    def test_real_aad_format(self):
        # Fixed format from the installed client's authenticated framing.
        expected = b'WB-AAD\0\x01\0\0\0\x05WBEV1\0\0\0\x06sym-v1\0\0\0\x01\0\0\0\x10' + KEY_ID.encode() + b'\x02\0\0'
        self.assertEqual(codec._aad(KEY_ID), expected)

    def test_plaintext_and_phone_authorization_are_unchanged(self):
        raw = json.dumps(fixture()[0], indent=2)
        with patch.object(codec, '_local_key', side_effect=AssertionError('must not call client')):
            self.assertEqual(codec.normalize_credential(raw), raw)
            self.assertTrue(wb_login.validate_credential(codec.normalize_credential(raw)).ok)
            self.assertEqual(codec.normalize_credential('\ufeff' + raw), raw)

    def test_all_wrappers_decode_without_mutating_original(self):
        plain, protected = fixture()
        raw = json.dumps(protected)
        with patch.object(codec, '_local_key', return_value=(KEY_ID, KEY)) as load:
            decoded = codec.normalize_credential(raw)
        load.assert_called_once()
        self.assertEqual(json.loads(decoded), plain)
        self.assertIn('$wbEncrypted', raw)
        self.assertTrue(wb_login.validate_credential(decoded).ok)

    def test_corrupt_ciphertext_is_rejected(self):
        wrapped = encrypted('test-secret-must-not-leak')
        envelope = json.loads(base64.b64decode(wrapped['envelope']))
        envelope['authTag'] = base64.b64encode(b'bad-auth-tag!!!!').decode()
        wrapped['envelope'] = base64.b64encode(json.dumps(envelope).encode()).decode()
        with self.assertRaises(codec.CredentialDecodeError) as error:
            codec._open_field(wrapped, KEY_ID, KEY)
        self.assertNotIn('test-secret', str(error.exception))

    def test_wrong_key_and_unknown_scheme_are_explicit(self):
        for wrapper, key_id in [(encrypted('token'), 'f' * 16),
                                ({**encrypted('token'), 'scheme': 'asym-v1'}, KEY_ID)]:
            with self.assertRaises(codec.CredentialDecodeError):
                codec._open_field(wrapper, key_id, KEY)

    def test_missing_client_does_not_turn_into_missing_token(self):
        with patch.object(codec, '_local_key', side_effect=codec.CredentialDecodeError('请使用手机号授权登录')):
            with self.assertRaisesRegex(codec.CredentialDecodeError, '手机号授权'):
                codec.normalize_credential(json.dumps(fixture()[1]))

    def test_native_failure_never_exposes_output(self):
        codec._installed_key.cache_clear()
        with patch.object(codec.subprocess, 'run', return_value=SimpleNamespace(returncode=1, stdout=b'private-key', stderr=b'private-token')):
            with self.assertRaises(codec.CredentialDecodeError) as error:
                codec._installed_key('test.exe', 1, 1)
        self.assertNotIn('private', str(error.exception))


class ImportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.session = sessionmaker(bind=self.engine)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.addCleanup(self.engine.dispose)
        self.stack.enter_context(patch.object(codec, '_local_key', return_value=(KEY_ID, KEY)))
        self.stack.enter_context(patch.object(accounts, '_post_login_automation'))
        self.stack.enter_context(patch.object(accounts, '_refresh_balance', return_value=True))
        self.probe = self.stack.enter_context(patch.object(wb_login, 'probe_credential_live',
            return_value=SimpleNamespace(ok=True, profile={'uid': 'offline-user', 'nickname': '上游昵称'}, balance={})))
        app = FastAPI()
        app.include_router(accounts.router)
        app.dependency_overrides[require_admin] = lambda: True
        def db():
            with self.session() as session:
                yield session
        app.dependency_overrides[get_db] = db
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test')
        self.addAsyncCleanup(self.client.aclose)

    def assert_saved_plaintext(self):
        with self.session() as db:
            row = db.query(Account).one()
            data = json.loads(row.auth_json)
            self.assertEqual(data, fixture()[0])
            self.assertNotIn('$wbEncrypted', row.auth_json)
            self.assertIsInstance(row.name, str)

    async def test_single_upload_decrypts_before_live_probe(self):
        response = await self.client.post('/api/accounts', json={'auth_json': json.dumps(fixture()[1])})
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_saved_plaintext()
        self.assertNotIn('$wbEncrypted', self.probe.call_args.args[0])

    async def test_batch_accepts_old_ui_encrypted_name_and_updates_duplicate(self):
        protected = fixture()[1]
        item = {'name': protected['account']['nickname'], 'auth_json': json.dumps(protected)}
        response = await self.client.post('/api/accounts/batch', json={'items': [item, item]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {'added': 1, 'skipped': 1, 'errors': []})
        self.assert_saved_plaintext()

    async def test_bad_ciphertext_does_not_import_or_probe(self):
        protected = fixture()[1]
        protected['auth']['accessToken']['envelope'] = 'invalid-base64'
        response = await self.client.post('/api/accounts/batch', json={'items': [{'auth_json': json.dumps(protected)}]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['added'], 0)
        self.assertEqual(len(response.json()['errors']), 1)
        self.probe.assert_not_called()
        with self.session() as db:
            self.assertEqual(db.query(Account).count(), 0)

    async def test_local_scan_then_import_and_export(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(accounts, '_client_auth_dir', return_value=directory):
            path = Path(directory) / 'workbuddy-desktop.info'
            raw = json.dumps(fixture()[1])
            path.write_text(raw, encoding='utf-8')
            response = await self.client.get('/api/accounts/scan-local')
            self.assertEqual(response.status_code, 200, response.text)
            item = response.json()['items'][0]
            self.assertEqual(item['nickname'], '测试昵称')
            self.assertEqual(item['credential_error'], '')
            self.assertNotIn('envelope', response.text)
            response = await self.client.post('/api/accounts/import-local', json={'all': True})
            self.assertEqual(response.json(), {'added': 1, 'skipped': 0, 'errors': []})
            self.assertEqual(path.read_text(encoding='utf-8'), raw)
            self.assert_saved_plaintext()
            exported = (await self.client.post('/api/accounts/export', json={})).json()['items'][0]
            with patch.object(codec, '_local_key', side_effect=AssertionError('export must be portable')):
                self.assertTrue(wb_login.validate_credential(codec.normalize_credential(exported)).ok)

    async def test_decode_failure_has_actionable_http_error(self):
        with patch.object(codec, '_local_key', side_effect=codec.CredentialDecodeError('未找到客户端，请使用手机号授权登录')):
            response = await self.client.post('/api/accounts', json={'auth_json': json.dumps(fixture()[1])})
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()['detail']['step'], 'decrypt')
            self.assertNotIn('envelope', response.text)
            self.assertIn('手机号授权', response.json()['detail']['message'])


if __name__ == '__main__':
    unittest.main()
