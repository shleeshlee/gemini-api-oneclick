"""Complete browser-session import boundaries and request authentication."""
import hashlib
import importlib
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests import test_osid as osid_tests
from tests.test_login_recovery import Slot, GeminiClient, worker, gateway, token_module, response
from slot import normalize_cookie_export, decode_cookie_export, cookie_export_value, parse_env_file
from gemini_webapi.types import RPCData
from gemini_webapi.constants import GRPC
import httpx


def exported():
    return [dict(name=name, value=value, domain=domain, path='/', hostOnly=host, secure=True,
                 expirationDate=None) for name, value, domain, host in [
        ('__Secure-1PSID', 'synthetic-id', '.google.com', False),
        ('__Secure-1PSIDTS', 'synthetic-ts', '.google.com', False),
        ('SAPISID', 'synthetic-sapisid', '.google.com', False),
        ('OSID', 'synthetic-osid', 'gemini.google.com', True),
        ('OSID', 'wrong-accounts-cookie', 'accounts.google.com', True),
        ('unrelated', 'excluded', '.example.invalid', False),
    ]]


class CookieImportTests(unittest.IsolatedAsyncioTestCase):
    setUp = osid_tests.OsidTests.setUp
    asyncTearDown = osid_tests.OsidTests.asyncTearDown

    def test_filters_domains_expiration_and_preserves_scopes(self):
        raw = exported() + [dict(name='expired', value='old', domain='.google.com', expirationDate=1),
                            dict(name='host-google', value='skip', domain='google.com', hostOnly=True)]
        rows = normalize_cookie_export(json.dumps(raw))
        self.assertEqual({r['name'] for r in rows}, {'OSID', 'SAPISID', '__Secure-1PSID', '__Secure-1PSIDTS'})
        self.assertEqual(cookie_export_value(rows, 'OSID'), 'synthetic-osid')
        c = next(r for r in rows if r['name'] == 'OSID')
        self.assertEqual((c['domain'], c['hostOnly'], c['path']), ('gemini.google.com', True, '/'))

    def test_rejects_malformed_exports_without_echoing_values(self):
        bad = exported()
        bad[0]['value'] = 'private-value\nINJECT=1'
        for raw in ['private-value', {}, [], bad, [dict(name='x', value='x', domain='.google.com')]]:
            with self.subTest(kind=type(raw).__name__), self.assertRaises(ValueError) as exc:
                normalize_cookie_export(raw)
            self.assertNotIn('private-value', str(exc.exception))

    async def test_roundtrip_and_osid_only_edit_preserve_full_session(self):
        rows = normalize_cookie_export(exported())
        slot = Slot.from_env_file(9, self.path)
        await slot.save_credentials(self.path, '', '', cookies=rows, account_index=1)
        restored = Slot.from_env_file(9, self.path)
        self.assertEqual(restored.account_index, 1)
        self.assertEqual(decode_cookie_export(restored.cookie_bundle), rows)
        self.assertEqual(parse_env_file(self.path)['SLOT_PROXY_ID'], 'keep')
        await restored.save_credentials(self.path, '', '', 'new-osid')
        restored = Slot.from_env_file(9, self.path)
        self.assertEqual(restored.account_index, 1)
        self.assertEqual(cookie_export_value(decode_cookie_export(restored.cookie_bundle), 'SAPISID'), 'synthetic-sapisid')
        self.assertEqual(cookie_export_value(decode_cookie_export(restored.cookie_bundle), 'OSID'), 'new-osid')

    async def test_replacing_pair_clears_old_complete_session(self):
        slot = Slot.from_env_file(9, self.path)
        await slot.save_credentials(self.path, '', '', cookies=normalize_cookie_export(exported()), account_index=1)
        await slot.save_credentials(self.path, 'next', 'next-ts')
        self.assertEqual(slot.cookie_bundle, '')
        self.assertEqual(slot.account_index, 0)
        self.assertEqual(Slot.from_env_file(9, self.path).cookie_bundle, '')

    async def test_direct_osid_reload_keeps_bundle_and_index(self):
        slot = Slot.from_env_file(9, self.path)
        await slot.save_credentials(self.path, '', '', cookies=normalize_cookie_export(exported()), account_index=2)
        with patch.object(Slot, 'init_client', AsyncMock()):
            await slot.reload(osid='reloaded-osid')
        self.assertEqual(slot.account_index, 2)
        rows = decode_cookie_export(slot.cookie_bundle)
        self.assertEqual(cookie_export_value(rows, 'SAPISID'), 'synthetic-sapisid')
        self.assertEqual(cookie_export_value(rows, 'OSID'), 'reloaded-osid')

    async def test_worker_accepts_json_and_returns_no_credentials(self):
        async def init(slot, proxy=None):
            slot.state['initializing'] = False
        with patch.object(Slot, 'init_client', init), patch.object(worker, 'get_runtime_models', AsyncMock(return_value=[])):
            r = await worker.worker_deploy_slot(9, SimpleNamespace(json=AsyncMock(return_value={'cookies_json': exported(), 'account_index': 1})))
            await worker._slot_tasks[9]
        self.assertEqual(r.status_code, 202)
        self.assertEqual(worker.slots[9].account_index, 1)
        self.assertNotIn('synthetic-sapisid', r.body.decode())
        self.assertTrue(worker.slots[9].cookie_bundle)

    async def test_gateway_forwards_validated_export_and_index(self):
        context = AsyncMock()
        context.__aenter__.return_value.post.return_value = httpx.Response(202, json={'ok': True})
        with patch.object(gateway, 'WORKER_MODE', True), patch.object(gateway, 'ENVS_DIR', self.root), \
             patch.dict(gateway.containers, {9: gateway.Container(9, 8009)}, clear=True), \
             patch.object(gateway.httpx, 'AsyncClient', return_value=context), \
             patch.object(gateway, 'check_health', AsyncMock()), patch.object(gateway, 'add_log'):
            await gateway.deploy_cookie(9, SimpleNamespace(json=AsyncMock(return_value={'cookies_json': exported(), 'account_index': 1})))
        sent = context.__aenter__.return_value.post.call_args.kwargs['json']
        self.assertEqual(sent['account_index'], 1)
        self.assertEqual(len(sent['cookies_json']), 4)
        self.assertNotIn('psid', sent)

    async def test_bad_json_mixed_inputs_and_indices_do_not_mutate(self):
        before = self.path.read_bytes()
        for body in [{'cookies_json': 'private-invalid'}, {'cookies_json': exported(), 'psid': 'x'},
                     {'cookies_json': exported(), 'account_index': -1}, {'cookies_json': exported(), 'account_index': True}]:
            for handler in [gateway.deploy_cookie, worker.worker_deploy_slot]:
                with self.assertRaises(worker.HTTPException) as exc:
                    await handler(9, SimpleNamespace(json=AsyncMock(return_value=body)))
                self.assertEqual(exc.exception.status_code, 400)
                self.assertNotIn('private-invalid', str(exc.exception))
        self.assertEqual(self.path.read_bytes(), before)

    async def test_rpc_uses_imported_auth_and_correct_account(self):
        rows = normalize_cookie_export(exported())
        client = GeminiClient('shadow-ignored', 'shadow-ts', cookie_entries=rows, account_index=1)
        self.assertEqual(client.cookies.get('__Secure-1PSID', domain='.google.com'), 'synthetic-id')
        self.assertNotIn('cookie_entries', client.kwargs)
        client.client = SimpleNamespace(cookies=client.cookies, post=AsyncMock(return_value=response()), close=AsyncMock())
        with patch('gemini_webapi.client.time.time', return_value=123):
            await GeminiClient._batch_execute.__wrapped__(client, [RPCData(rpcid=GRPC.GET_USER_STATUS, payload='[]')])
        call = client.client.post.call_args
        expected = hashlib.sha1(b'123 synthetic-sapisid https://gemini.google.com').hexdigest()
        self.assertEqual(call.kwargs['headers']['Authorization'], 'SAPISIDHASH 123_' + expected)
        self.assertEqual(call.kwargs['headers']['X-Goog-AuthUser'], '1')
        self.assertIn('/u/1/', str(call.args[0]))
        self.assertEqual(client._authentication_headers().get('X-Goog-AuthUser'), '1')

    async def test_full_import_skips_legacy_cache(self):
        client = GeminiClient(cookie_entries=normalize_cookie_export(exported()))
        session = SimpleNamespace(cookies=client.cookies, get=AsyncMock(return_value=response()))
        with patch.object(token_module, '_add_cached_cookie_tasks', side_effect=AssertionError('stale cache accessed')):
            await token_module.get_access_token(client.cookies, session=session, use_cache=False)

    async def test_writeback_keeps_rotated_full_jar_and_index(self):
        slot = Slot.from_env_file(9, self.path)
        await slot.save_credentials(self.path, '', '', cookies=normalize_cookie_export(exported()), account_index=1)
        jar = GeminiClient(cookie_entries=decode_cookie_export(slot.cookie_bundle)).cookies
        jar.set('SAPISID', 'rotated-sapisid', domain='.google.com')
        jar.set('__Secure-1PSIDTS', 'rotated-ts', domain='.google.com')
        slot.client = SimpleNamespace(cookies=jar)
        slot.state['initializing'] = False
        self.assertTrue(slot.writeback_cookies())
        restored = Slot.from_env_file(9, self.path)
        self.assertEqual(restored.account_index, 1)
        self.assertEqual(cookie_export_value(decode_cookie_export(restored.cookie_bundle), 'SAPISID'), 'rotated-sapisid')
        self.assertEqual(restored.psidts, 'rotated-ts')

    def test_media_preserves_domain_and_path_for_all_imported_cookies(self):
        import main
        rows = normalize_cookie_export(exported())
        rows.append(dict(name='GeminiOnly', value='private', domain='gemini.google.com', path='/app', hostOnly=True, secure=True, expirationDate=None))
        jar = GeminiClient(cookie_entries=rows).cookies
        for module in [main, worker]:
            with httpx.Client(cookies=module._flatten_cookies(jar)) as client:
                for url in ['https://example.invalid/', 'https://lh3.googleusercontent.com/', 'https://accounts.google.com/']:
                    header = client.build_request('GET', url).headers.get('cookie', '')
                    self.assertNotIn('GeminiOnly=', header)
                    self.assertNotIn('OSID=', header)
                    if 'google.com/' not in url:
                        self.assertNotIn('SAPISID=', header)

    async def test_library_media_retains_cookie_restrictions(self):
        from gemini_webapi.types.image import GeneratedImage
        from gemini_webapi.types.video import GeneratedVideo
        expires = int(time.time()) + 3600
        rows = normalize_cookie_export(exported()) + [dict(name='restricted', value='synthetic', domain='.google.com',
            path='/private', secure=True, hostOnly=False, expirationDate=expires)]
        jar = GeminiClient(cookie_entries=rows).cookies
        for kind in ['image', 'video', 'thumbnail']:
            session = SimpleNamespace(headers={}, cookies=None)
            async def intercept(*args, **kwargs):
                matches = [c for c in session.cookies.jar if c.name == 'restricted']
                self.assertEqual(len(matches), 1)
                self.assertEqual((matches[0].domain, matches[0].path, matches[0].secure, matches[0].expires),
                                 ('.google.com', '/private', True, expires))
                raise RuntimeError('offline stop')
            session.get = AsyncMock(side_effect=intercept)
            context = AsyncMock()
            context.__aenter__.return_value = session
            module = importlib.import_module('gemini_webapi.types.' + ('image' if kind == 'image' else 'video'))
            video = GeneratedVideo(url='https://contribution.usercontent.google.com/video', thumbnail_url='https://lh3.googleusercontent.com/thumb', cookies=jar)
            image = GeneratedImage(url='https://lh3.googleusercontent.com/image', cookies=jar)
            self.assertIs(video.cookies, jar)
            self.assertIs(image.cookies, jar)
            with patch.object(module, 'AsyncSession', return_value=context):
                try:
                    if kind == 'image':
                        await image.save(path=str(self.root), filename='test.png')
                    elif kind == 'video':
                        await video.save(path=str(self.root), filename='test.mp4')
                    else:
                        await video.save_thumbnail(path=str(self.root), filename='test.png')
                except RuntimeError:
                    pass
            session.get.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
