"""Offline regressions for failed login, deadlines and isolated slot recovery."""
import asyncio
import importlib
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "app"), str(ROOT / "lib")]
import worker
import slot as slot_module
from slot import Slot
from gemini_webapi import GeminiClient
from gemini_webapi.constants import AccountStatus
from gemini_webapi.exceptions import APIError, AuthError
from curl_cffi.requests import Cookies
from gemini_webapi.types import RPCData
from gemini_webapi.constants import GRPC

token_module = importlib.import_module("gemini_webapi.utils.get_access_token")
_read = Path.read_text
with patch.object(Path, "read_text", lambda p, *a, **kw: "" if p.name == ".env" else _read(p, *a, **kw)):
    import gateway


def response(status=200, url="https://gemini.google.com/app", text='{"SNlM0e":"synthetic"}'):
    return SimpleNamespace(status_code=status, url=url, text=text, cookies=Cookies(), raise_for_status=Mock())


class LoginRecovery(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.patches = [patch("gemini_webapi.cookie_audit.CookieAudit.create", return_value=None),
                        patch.object(worker, "_slot_tasks", {}),
                        patch.object(worker, "slots", {}),
                        patch.object(worker, "ENVS_DIR", self.root)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    async def asyncTearDown(self):
        for task in worker._slot_tasks.values():
            task.cancel()
        await asyncio.gather(*worker._slot_tasks.values(), return_exceptions=True)

    def test_login_redirect_and_upstream_auth_code(self):
        for url in ["https://accounts.google.com/v3/signin/identifier?secret=hidden",
                    "https://consent.google.com/", "https://myaccount.google.com/restrictions/280",
                    "https://gemini.google.com.evil.invalid/app"]:
            with self.subTest(url=url), self.assertRaises(AuthError) as error:
                token_module.validate_login_response(response(url=url))
            self.assertIn("需要重新认证", str(error.exception))
            self.assertNotIn("hidden", str(error.exception))
            self.assertIsNone(error.exception.http_status)
        for status in [401, 403]:
            with self.subTest(status=status), self.assertRaises(AuthError) as error:
                token_module.validate_login_response(response(status=status))
            self.assertEqual(error.exception.http_status, status)

    async def test_login_page_tokens_cannot_pass_authentication(self):
        session = SimpleNamespace(cookies=Cookies(), get=AsyncMock(side_effect=[response(url="https://www.google.com"),
                                  response(url="https://accounts.google.com/v3/signin/identifier")]))
        with patch.object(token_module, "_get_cache_dir", return_value=self.root):
            with self.assertRaises(AuthError) as error:
                await token_module.get_access_token({"__Secure-1PSID": "synthetic", "__Secure-1PSIDTS": "synthetic-ts"}, session=session)
        self.assertIn("需要重新认证", str(error.exception))

    async def test_pending_token_task_cancelled_on_success(self):
        cancelled = asyncio.Event()
        started = asyncio.Event()

        async def slow():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async def fast():
            await started.wait()
            return response(), Cookies()

        def populate(tasks, *args, **kwargs):
            tasks.extend([asyncio.create_task(slow()), asyncio.create_task(fast())])

        session = SimpleNamespace(get=AsyncMock(return_value=response()))
        with patch.object(token_module, "_add_base_cookie_task", populate), \
             patch.object(token_module, "_add_cached_cookie_tasks"):
            await token_module.get_access_token({}, session=session)
        self.assertTrue(cancelled.is_set())

    async def test_401_and_503_init_do_not_retry_or_reenter(self):
        import gemini_webapi.client as module

        for status in [401, 503]:
            with self.subTest(status=status):
                session = SimpleNamespace(headers={}, cookies=Cookies(), post=AsyncMock(return_value=response(status=status)), close=AsyncMock())
                client = GeminiClient()
                with patch.object(module, "AsyncSession", return_value=session), \
                     patch.object(module, "get_access_token", AsyncMock(return_value=("synthetic", "build", "session", Cookies()))):
                    expected = AuthError if status == 401 else APIError
                    with self.assertRaises(expected) as error:
                        await asyncio.wait_for(client.init(auto_refresh=False, verbose=False), timeout=0.2)
                self.assertIn(str(status), str(error.exception))
                session.post.assert_awaited_once()
                session.close.assert_awaited_once()
                self.assertIsNone(client._init_task)
                self.assertFalse(client._lock.locked())

    async def test_refresh_auth_error_preserves_existing_transport(self):
        client = GeminiClient()
        session = SimpleNamespace(post=AsyncMock(return_value=response(status=401)), close=AsyncMock())
        client.client, client._running = session, True
        with self.assertRaises(AuthError):
            await GeminiClient._batch_execute.__wrapped__(client, [RPCData(rpcid=GRPC.GET_USER_STATUS, payload="[]")], close_on_error=False)
        session.close.assert_not_awaited()
        self.assertTrue(client._running)

    async def test_idempotent_close(self):
        client = GeminiClient()
        session = SimpleNamespace(close=AsyncMock())
        client.client = session
        await client.close()
        await client.close()
        session.close.assert_awaited_once()

    async def test_cleanup_failure_or_hang_cannot_hide_401(self):
        import gemini_webapi.client as module
        async def hang():
            await asyncio.Event().wait()
        for cleanup in [RuntimeError("cleanup failure"), hang]:
            session = SimpleNamespace(headers={}, cookies=Cookies(), post=AsyncMock(return_value=response(status=401)),
                                      close=AsyncMock(side_effect=cleanup))
            client = GeminiClient()
            with patch.object(module, "AsyncSession", return_value=session), \
                 patch.object(module, "get_access_token", AsyncMock(return_value=("synthetic", None, None, Cookies()))), \
                 patch.object(module, "_CLEANUP_TIMEOUT", 0.01):
                with self.assertRaises(AuthError) as error:
                    await asyncio.wait_for(client.init(auto_refresh=False, verbose=False), timeout=0.2)
            self.assertEqual(error.exception.http_status, 401)

    async def test_cancelled_library_init_has_bounded_cleanup(self):
        import gemini_webapi.client as module
        async def hang(*args, **kwargs):
            await asyncio.Event().wait()
        session = SimpleNamespace(headers={}, cookies=Cookies(), close=AsyncMock(side_effect=hang))
        client = GeminiClient()
        start = time.monotonic()
        with patch.object(module, "AsyncSession", return_value=session), \
             patch.object(module, "get_access_token", AsyncMock(side_effect=hang)), \
             patch.object(module, "_CLEANUP_TIMEOUT", 0.01):
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(client.init(auto_refresh=False, verbose=False), timeout=0.02)
        self.assertLess(time.monotonic() - start, 0.2)
        self.assertIsNone(client.client)
        self.assertIsNone(client._init_task)

    async def test_slot_reports_auth_failure_without_retry(self):
        slot = Slot(num=9, psid="synthetic", psidts="synthetic-ts")
        fake = SimpleNamespace(init=AsyncMock(side_effect=AuthError("HTTP 401 Unauthorized: 需要重新认证", http_status=401)), close=AsyncMock())
        with patch("gemini_webapi.GeminiClient", return_value=fake):
            with self.assertRaises(AuthError):
                await slot.init_client()
        health = slot.health_response()
        self.assertEqual(health["status"], "auth_failed")
        self.assertEqual(health["last_http_status"], 401)
        self.assertFalse(health["needs_restart"])
        with self.assertRaises(worker.HTTPException) as error:
            await worker._get_client(slot)
        self.assertEqual(error.exception.status_code, 401)
        fake.init.assert_awaited_once()

    async def test_total_deadline_covers_login_and_probe(self):
        for phase in ["login", "probe"]:
            async def hanging(*args, **kwargs):
                await asyncio.Event().wait()
            slot = Slot(num=9, psid="synthetic", psidts="synthetic-ts")
            fake = SimpleNamespace(init=AsyncMock(side_effect=hanging if phase == "login" else None),
                                   account_status=AccountStatus.AVAILABLE, close=AsyncMock())
            with self.subTest(phase=phase), patch("gemini_webapi.GeminiClient", return_value=fake), \
                 patch.object(slot_module, "INIT_TIMEOUT_SECONDS", 0.02), \
                 patch.object(Slot, "probe_free_pro", AsyncMock(side_effect=hanging if phase == "probe" else None)):
                with self.assertRaises(TimeoutError):
                    await slot.init_client()
            self.assertEqual(slot.health_response()["status"], "init_timeout")
            self.assertFalse(slot.state["initializing"])
            fake.close.assert_awaited_once()

    async def test_cancellation_does_not_leave_initializing(self):
        started = asyncio.Event()
        async def hanging(*args):
            started.set()
            await asyncio.Event().wait()
        slot = Slot(num=9)
        with patch.object(Slot, "_init_client", hanging):
            task = asyncio.create_task(slot.init_client())
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(slot.state["initializing"])
        self.assertEqual(slot.state["last_error_type"], "init_cancelled")

    async def test_reload_is_background_and_isolates_other_slot(self):
        release = asyncio.Event()
        seen = []
        async def reload(slot, envs):
            seen.append(slot.num)
            await release.wait()
            slot.state["initializing"] = False
        target, other = Slot(9), Slot(1, client=object())
        other_client = other.client
        worker.slots.update({9: target, 1: other})
        with patch.object(Slot, "reload_from_env", reload), patch.object(worker, "get_runtime_models", AsyncMock()):
            result = await asyncio.wait_for(worker.worker_reload_slot(9), timeout=0.1)
            self.assertEqual(result.status_code, 202)
            second = await worker.worker_reload_slot(9)
            self.assertFalse(json.loads(second.body)["started"])
            await asyncio.sleep(0)
            self.assertEqual(seen, [9])
            self.assertIs(other.client, other_client)
            release.set()
            await worker._slot_tasks[9]

    async def test_failed_slot_accepts_new_cookie_without_old_auth_result(self):
        target = Slot(9)
        target.state.update(initializing=False, auth_status="UNAUTHENTICATED", last_error_type="auth_failed", last_error="old 401")
        worker.slots[9] = target
        release = asyncio.Event()
        async def reload(slot, env):
            await release.wait()
        request = SimpleNamespace(json=AsyncMock(return_value={"psid": "synthetic-new", "psidts": "synthetic-ts"}))
        with patch.object(Slot, "reload_from_env", reload), patch.object(worker, "get_runtime_models", AsyncMock()):
            result = await worker.worker_deploy_slot(9, request)
            self.assertEqual(result.status_code, 202)
            health = json.loads(result.body)["health"]
            self.assertEqual(health["auth_status"], "unknown")
            self.assertEqual(health["last_error"], "")
            self.assertEqual(health["status"], "initializing")
            release.set()
            await worker._slot_tasks[9]

    async def test_gateway_202_never_treats_old_auth_as_new_failure(self):
        import httpx
        c = gateway.Container(9, 8009)
        context = AsyncMock()
        context.__aenter__.return_value.post.return_value = httpx.Response(202, json={"ok": True, "health": {"auth_status": "UNAUTHENTICATED"}})
        context.__aenter__.return_value.get.return_value = httpx.Response(200, json={"status": "initializing", "auth_status": "unknown"})
        request = SimpleNamespace(json=AsyncMock(return_value={"psid": "synthetic", "psidts": "synthetic-ts"}))
        with patch.object(gateway, "WORKER_MODE", True), patch.dict(gateway.containers, {9: c}, clear=True), \
             patch.object(gateway, "ENVS_DIR", self.root), patch.object(gateway.httpx, "AsyncClient", return_value=context), \
             patch.object(gateway, "add_log"):
            result = await gateway.deploy_cookie(9, request)
        self.assertTrue(result["ok"])
        self.assertTrue(c.initializing)
        self.assertFalse(c.needs_cookie)

    async def test_stopped_timeout_is_not_reported_as_preparing(self):
        c = gateway.Container(9, 8009)
        c.last_error_type, c.last_error = "init_timeout", "初始化超时（总时限 60 秒）"
        request = SimpleNamespace(body=AsyncMock(return_value=b"{}"), headers={}, method="POST")
        with patch.dict(gateway.containers, {9: c}, clear=True), patch.dict(gateway.container_groups, {}, clear=True):
            with self.assertRaises(gateway.HTTPException) as error:
                await gateway.proxy(request, "images/generations")
        self.assertNotIn("正在准备", error.exception.detail)
        self.assertIn("初始化超时", error.exception.detail)

    async def test_worker_failure_finishes_without_automatic_retry(self):
        slot = Slot(9)
        async def fail(slot, env):
            slot.state.update(initializing=False, last_error_type="auth_failed", last_error="HTTP 401 Unauthorized")
            raise AuthError("HTTP 401 Unauthorized", http_status=401)
        with patch.object(Slot, "reload_from_env", fail):
            worker.schedule_slot_init(slot, reload=True)
            await worker._slot_tasks[9]
        self.assertEqual(slot.state["last_error_type"], "auth_failed")
        self.assertTrue(worker._slot_tasks[9].done())

    async def test_delete_cancels_only_selected_recovery(self):
        started = asyncio.Event()
        stopped = asyncio.Event()
        async def hang(slot, env):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        selected, other = Slot(9), Slot(1, client=object())
        worker.slots.update({9: selected, 1: other})
        with patch.object(Slot, "reload_from_env", hang):
            worker.schedule_slot_init(selected, reload=True)
            await started.wait()
            result = await worker.worker_delete_slot(9)
        self.assertTrue(result["ok"])
        self.assertTrue(stopped.is_set())
        self.assertNotIn(9, worker._slot_tasks)
        self.assertIs(worker.slots[1], other)

    async def test_gateway_connection_error_clears_loading_and_keeps_file(self):
        import httpx
        c = gateway.Container(9, 8009)
        env = self.root / "account9.env"
        env.write_text("SECURE_1PSID=old\nSECURE_1PSIDTS=old-ts\n")
        context = AsyncMock()
        context.__aenter__.return_value.post.side_effect = httpx.ConnectError("offline")
        request = SimpleNamespace(json=AsyncMock(return_value={"psid": "synthetic", "psidts": "synthetic-ts"}))
        with patch.object(gateway, "WORKER_MODE", True), patch.dict(gateway.containers, {9: c}, clear=True), \
             patch.object(gateway, "ENVS_DIR", self.root), patch.object(gateway.httpx, "AsyncClient", return_value=context), \
             patch.object(gateway, "add_log"):
            with self.assertRaises(gateway.HTTPException):
                await gateway.deploy_cookie(9, request)
        self.assertFalse(c.initializing)
        self.assertEqual(env.read_text(), "SECURE_1PSID=old\nSECURE_1PSIDTS=old-ts\n")

    async def test_gateway_displays_failure_and_timeout_immediately(self):
        import httpx
        container = gateway.Container(9, 8009)
        for status, auth, kind, detail in [
            ("auth_failed", "UNAUTHENTICATED", "auth_failed", "HTTP 401 Unauthorized: 需要重新认证"),
            ("init_timeout", "unknown", "init_timeout", "初始化超时（总时限 60 秒）")]:
            data = {"status": status, "auth_status": auth, "last_error_type": kind, "last_error": detail,
                    "last_http_status": 401 if kind == "auth_failed" else None, "models": []}
            fake = SimpleNamespace(get=AsyncMock(return_value=httpx.Response(200, json=data)))
            await gateway.check_health(container, fake)
            self.assertFalse(container.initializing)
            self.assertFalse(container.healthy)
            self.assertEqual(container.last_error, detail)
            self.assertEqual(container.to_dict()["last_error_type"], kind)


if __name__ == "__main__":
    unittest.main()
