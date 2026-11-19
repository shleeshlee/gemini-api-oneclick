"""Offline regression checks for proxy persistence and account-only assignment."""
import asyncio
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'app'), str(ROOT / 'lib')]
import httpx
import worker
from slot import Slot, parse_env_file
from app.proxy_settings import ProxySettings
original_read = Path.read_text
with patch.object(Path, 'read_text', lambda p, *a, **kw: '' if p.name == '.env' else original_read(p, *a, **kw)):
    import gateway

HTTPClient = httpx.AsyncClient


class SlotProxyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.file = self.root / 'account20.env'
        self.file.write_text('SECURE_1PSID=synthetic-one\nSECURE_1PSIDTS=synthetic-two\nOTHER=preserved\n')
        self.slot = Slot.from_env_file(20, self.file)
        self.slot.state.update(initializing=False, auth_status='AVAILABLE')
        self.slot.client = SimpleNamespace(_running=True, proxy='http://default.test:80',
                                          client=SimpleNamespace(proxies={'all': 'http://default.test:80'}, cookies={'same': 'jar'}))
        for p in [patch.object(worker, 'slots', {20: self.slot}), patch.object(worker, 'WORKER_MANAGEMENT_KEY', 'synthetic-key'),
                  patch.object(worker, 'PROXY', 'http://default.test:80'), patch.object(worker, '_management_locks', {}),
                  patch.object(worker, '_research_tasks', {}), patch.object(worker, '_slot_tasks', {}),
                  patch('socket.socket.connect', side_effect=AssertionError('No real network'))]:
            p.start(); self.addCleanup(p.stop)

    async def post(self, body, key='synthetic-key'):
        async with HTTPClient(transport=httpx.ASGITransport(app=worker.app), base_url='http://offline.test') as client:
            return await client.post('/worker/proxy/20', json=body, headers={'Authorization': 'Bearer ' + key})

    async def test_assignment_preserves_session_cookies_and_other_settings(self):
        client, jar = self.slot.client, self.slot.client.client.cookies
        with patch.object(self.slot, 'init_client', AsyncMock()) as init:
            response = await self.post({'proxy_id': 'route-a', 'proxy_url': 'http://user:synthetic@proxy.test:80'})
            self.assertEqual(response.status_code, 200)
            init.assert_not_called()
        self.assertIs(self.slot.client, client)
        self.assertIs(self.slot.client.client.cookies, jar)
        self.assertEqual(client.client.proxies, {'all': 'http://user:synthetic@proxy.test:80'})
        values = parse_env_file(self.file)
        self.assertEqual(values['SECURE_1PSID'], 'synthetic-one')
        self.assertEqual(values['OTHER'], 'preserved')
        restored = Slot.from_env_file(20, self.file)
        self.assertEqual((restored.proxy_id, restored.proxy_url), ('route-a', 'http://user:synthetic@proxy.test:80'))
        self.assertNotIn('synthetic@', str(self.slot.health_response()))
        self.assertEqual(stat.S_IMODE(self.file.stat().st_mode), 0o600)

    async def test_clear_assignment_uses_default_and_cookie_update_retains_binding(self):
        await self.post({'proxy_id': 'a', 'proxy_url': 'http://proxy.test:80'})
        await self.slot.save_credentials(self.file, 'synthetic-new', 'synthetic-new-ts')
        self.assertEqual(parse_env_file(self.file)['SLOT_PROXY_ID'], 'a')
        with patch.object(self.slot, 'init_client', AsyncMock()) as init, patch.dict(os.environ, {'HTTP_PROXY': 'http://default.test:80'}):
            self.slot.client = None
            await self.slot.reload_from_env(self.root)
            self.assertEqual(self.slot.proxy_url, 'http://proxy.test:80')
            init.assert_awaited_once_with(proxy='http://default.test:80')
        self.slot.state['initializing'] = False
        response = await self.post({'proxy_id': '', 'proxy_url': ''})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(parse_env_file(self.file)['SLOT_PROXY'], '')

    async def test_initialization_passes_slot_override_to_gemini_client(self):
        self.slot.proxy_url = 'http://chosen.test:80'
        fake = SimpleNamespace(init=AsyncMock(), account_status=SimpleNamespace(name='AVAILABLE'), close=AsyncMock())
        from gemini_webapi.constants import AccountStatus
        fake.account_status = AccountStatus.AVAILABLE
        with patch('gemini_webapi.GeminiClient', return_value=fake) as constructor, \
             patch('gemini_webapi.cookie_audit.CookieAudit.create', return_value=None), \
             patch.object(self.slot, 'probe_free_pro', AsyncMock()):
            await self.slot._init_client('http://default.test:80')
        self.assertEqual(constructor.call_args.kwargs['proxy'], 'http://chosen.test:80')
        self.assertFalse(fake.init.call_args.kwargs['auto_refresh'])

    async def test_busy_or_unauthorized_assignment_does_not_write(self):
        original = self.file.read_text()
        self.assertEqual((await self.post({'proxy_id': 'a', 'proxy_url': 'x'}, key='wrong')).status_code, 401)
        self.slot.active_requests = 1
        self.assertEqual((await self.post({'proxy_id': 'a', 'proxy_url': 'x'})).status_code, 409)
        self.assertEqual(self.file.read_text(), original)

    async def test_failed_file_write_preserves_binding_and_transport(self):
        with patch('slot.os.replace', side_effect=OSError('synthetic failure')):
            response = await self.post({'proxy_id': 'a', 'proxy_url': 'http://new.test'})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.slot.proxy_id, '')
        self.assertEqual(self.slot.client.proxy, 'http://default.test:80')
        self.assertFalse(self.slot.proxy_changing)


class GatewayProxyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = ProxySettings(self.root / 'proxy-settings.json')
        self.settings.commit(proxies={'a': {'id': 'a', 'name': 'A', 'url': 'http://proxy.test:80'}})
        for name, value in {'proxy_settings': self.settings, '_proxy_settings_lock': asyncio.Lock(),
                            '_worker_route_locks': {}, 'COOKIE_MANAGER_PASSWORD': 'synthetic-panel',
                            'WORKER_MODE': True, 'containers': {20: SimpleNamespace(url='http://worker.test/slot/20')},
                            'WORKER_MANAGEMENT_KEY': 'synthetic-management'}.items():
            p = patch.object(gateway, name, value); p.start(); self.addCleanup(p.stop)

    async def request(self, method, path, body=None, key='synthetic-panel'):
        async with HTTPClient(transport=httpx.ASGITransport(app=gateway.app), base_url='http://offline.test') as client:
            return await client.request(method, path, json=body, headers={'Authorization': 'Bearer ' + key})

    def upstream(self, handler):
        return patch.object(gateway.httpx, 'AsyncClient', side_effect=lambda **kw: HTTPClient(transport=httpx.MockTransport(handler), **kw))

    async def test_library_and_selection_persist_and_require_panel_auth(self):
        self.assertEqual((await self.request('GET', '/gateway/proxies', key='wrong')).status_code, 401)
        result = await self.request('POST', '/gateway/proxies', {'name': 'B', 'url': 'socks5://user:synthetic@proxy.test:1080'})
        self.assertEqual(result.status_code, 200)
        calls = []
        def upstream(request):
            self.assertEqual(request.headers['authorization'], 'Bearer synthetic-management')
            calls.append((request.method, str(request.url)))
            return httpx.Response(200, json={'proxy_id': '', 'proxy_url': ''} if request.method == 'GET' else {'ok': True})
        with self.upstream(upstream):
            result = await self.request('POST', '/gateway/proxy/20', {'proxy_id': 'a'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(calls, [('GET', 'http://worker.test/worker/proxy/20'), ('POST', 'http://worker.test/worker/proxy/20')])
        restored = ProxySettings(self.settings.path); restored.load()
        self.assertEqual(restored.assignments, {'20': 'a'})
        self.assertEqual((await self.request('DELETE', '/gateway/proxies/a')).status_code, 409)
        self.assertEqual(stat.S_IMODE(self.settings.path.stat().st_mode), 0o600)

    async def test_worker_failure_does_not_claim_binding_saved(self):
        def upstream(request):
            return httpx.Response(200, json={'proxy_id': '', 'proxy_url': ''}) if request.method == 'GET' else httpx.Response(409, json={})
        with self.upstream(upstream):
            result = await self.request('POST', '/gateway/proxy/20', {'proxy_id': 'a'})
        self.assertEqual(result.status_code, 409)
        self.assertEqual(self.settings.assignments, {})

    async def test_failed_gateway_commit_rolls_worker_back(self):
        posted = []
        def upstream(request):
            if request.method == 'POST':
                posted.append(json.loads(request.content))
            return httpx.Response(200, json={'proxy_id': '', 'proxy_url': ''})
        with self.upstream(upstream), patch.object(self.settings, 'commit', side_effect=OSError('synthetic failure')):
            result = await self.request('POST', '/gateway/proxy/20', {'proxy_id': 'a'})
        self.assertEqual(result.status_code, 500)
        self.assertEqual(posted, [{'proxy_id': 'a', 'proxy_url': 'http://proxy.test:80'}, {'proxy_id': '', 'proxy_url': ''}])

    async def test_empty_cookie_deploy_never_calls_worker(self):
        with patch.object(gateway.httpx, 'AsyncClient', side_effect=AssertionError('Must not deploy')):
            response = await self.request('POST', '/gateway/deploy/20', {'psid': '', 'psidts': ''})
            partial = await self.request('POST', '/gateway/deploy/20', {'psid': 'new', 'psidts': ''})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['deployed'])
        self.assertEqual(partial.status_code, 400)

    async def test_migration_rejects_candidate_with_different_proxy_before_chat(self):
        self.settings.commit(assignments={'20': 'a'})
        account = gateway.Container(20, 8020)
        account.url = 'http://old.test/slot/20'
        gateway.containers[20] = account
        calls = []
        def upstream(request):
            calls.append(request.url.path)
            if request.url.path.endswith('/health'):
                return httpx.Response(200, json={'status': 'healthy', 'auth_status': 'AVAILABLE',
                    'client_ready': True, 'proxy_id': '', 'models': [{'id': 'gemini-flash', 'level': 'flash'}]})
            return httpx.Response(200, json={'proxy_id': '', 'proxy_url': ''})
        with self.upstream(upstream), patch.object(gateway, 'WORKER_CANDIDATE_URLS', {'http://candidate.test'}):
            response = await self.request('POST', '/gateway/worker-route/20', {'worker_url': 'http://candidate.test'})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(account.url, 'http://old.test/slot/20')
        self.assertEqual(calls, ['/slot/20/health', '/worker/proxy/20'])


if __name__ == '__main__':
    unittest.main()
