"""Four-cookie form uses the verified scoped session login path."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import unittest

from tests import test_osid as support
from tests.test_login_recovery import Slot, GeminiClient, worker, gateway
from slot import cookie_deploy_payload, decode_cookie_export, cookie_export_value


def four():
    return dict(psid='synthetic-id', psidts='synthetic-ts', osid='synthetic-osid', sid='synthetic-sid', account_index=1)


class SidTests(unittest.IsolatedAsyncioTestCase):
    setUp = support.OsidTests.setUp
    asyncTearDown = support.OsidTests.asyncTearDown

    async def test_four_cookie_deployment_survives_restart_and_uses_scoped_sid(self):
        async def init(slot, proxy=None):
            slot.state['initializing'] = False
        with patch.object(Slot, 'init_client', init), patch.object(worker, 'get_runtime_models', AsyncMock(return_value=[])):
            response = await worker.worker_deploy_slot(9, SimpleNamespace(json=AsyncMock(return_value=four())))
            await worker._slot_tasks[9]
        self.assertEqual(response.status_code, 202)
        restored = Slot.from_env_file(9, self.path)
        rows = decode_cookie_export(restored.cookie_bundle)
        self.assertEqual({r['name'] for r in rows}, {'__Secure-1PSID', '__Secure-1PSIDTS', 'OSID', 'SID'})
        self.assertEqual(cookie_export_value(rows, 'SID'), 'synthetic-sid')
        self.assertEqual(restored.account_index, 1)
        client = GeminiClient(cookie_entries=rows, account_index=restored.account_index)
        self.assertEqual(client.cookies.get('SID', domain='.google.com'), 'synthetic-sid')
        self.assertEqual(client.cookies.get('OSID', domain='gemini.google.com'), 'synthetic-osid')
        self.assertTrue(client._full_cookie_session)
        self.assertNotIn('Authorization', client._authentication_headers())
        self.assertNotIn('synthetic-sid', response.body.decode())

    async def test_invalid_or_partial_sid_rejected_before_saving(self):
        before = self.path.read_bytes()
        bodies = [dict(sid='synthetic'), dict(four(), sid='bad\nVALUE=x'), dict(four(), sid=123),
                  dict(four(), osid=''), dict(four(), cookies_json=[{}])]
        for body in bodies:
            for endpoint in [gateway.deploy_cookie, worker.worker_deploy_slot]:
                with self.subTest(endpoint=endpoint.__name__), self.assertRaises(worker.HTTPException) as exc:
                    await endpoint(9, SimpleNamespace(json=AsyncMock(return_value=body)))
                self.assertEqual(exc.exception.status_code, 400)
        self.assertEqual(self.path.read_bytes(), before)

    async def test_four_fields_replace_an_old_complete_session(self):
        from tests.test_cookie_import import exported
        from slot import normalize_cookie_export
        slot = Slot.from_env_file(9, self.path)
        await slot.save_credentials(self.path, '', '', cookies=normalize_cookie_export(exported()), account_index=2)
        rows, index = cookie_deploy_payload(four())
        await slot.save_credentials(self.path, '', '', cookies=rows, account_index=index)
        saved = decode_cookie_export(slot.cookie_bundle)
        self.assertEqual(len(saved), 4)
        self.assertEqual(cookie_export_value(saved, 'SAPISID'), '')
        self.assertEqual(slot.account_index, 1)
