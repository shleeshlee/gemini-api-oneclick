"""Offline worker migration, origin pinning and streamed failure regressions."""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "app"), str(ROOT / "lib")]
import httpx
from app.worker_routing import WorkerRouting
original_read = Path.read_text
with patch.object(Path, "read_text", lambda p, *a, **kw: "" if p.name == ".env" else original_read(p, *a, **kw)):
    import gateway

HTTPClient = httpx.AsyncClient
OLD = "http://127.0.0.1:7860"
NEW = "http://127.0.0.1:17860"
HEALTH = {"status": "healthy", "auth_status": "AVAILABLE", "client_ready": True,
          "models": [{"id": "gemini-flash", "level": "flash"}]}


class WorkerRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.routing = WorkerRouting(self.root / "routes.json")
        for name, value in {
            "WORKER_MODE": True, "WORKER_URL": OLD, "WORKER_CANDIDATE_URLS": {NEW},
            "WORKER_MANAGEMENT_KEY": "synthetic-management", "API_KEY": "synthetic-api",
            "COOKIE_MANAGER_PASSWORD": "synthetic-panel", "worker_routing": self.routing,
            "_worker_route_locks": {}, "containers": {}, "container_groups": {},
            "group_defs": [], "group_round_robin": {}, "_session_routes": {},
            "_known_model_levels": {"flash"}, "fetch_base_models": AsyncMock(),
            "add_log": Mock(), "record_model_truth": Mock(), "ENVS_DIR": self.root,
            "_build_image_prompt": lambda body, headers: (body, json.dumps(body).encode(), headers),
        }.items():
            p = patch.object(gateway, name, value)
            p.start()
            self.addCleanup(p.stop)
        c = gateway.Container(2, 8002)
        c.healthy = True
        c.model_levels = ["flash"]
        gateway.containers[2] = c
        self.account = c
        for target in ("socket.socket.connect", "socket.create_connection"):
            p = patch(target, side_effect=AssertionError("Network forbidden"))
            p.start()
            self.addCleanup(p.stop)

    async def request(self, method, path, body=None, panel=True):
        async with HTTPClient(transport=httpx.ASGITransport(app=gateway.app), base_url="http://offline.test") as client:
            return await client.request(method, path, json=body,
                headers={"Authorization": "Bearer " + ("synthetic-panel" if panel else "synthetic-api")})

    def upstream(self, handler):
        return patch.object(gateway.httpx, "AsyncClient", side_effect=lambda **kw: HTTPClient(transport=httpx.MockTransport(handler), **kw))

    async def test_failed_candidate_never_switches_or_touches_old(self):
        calls = []
        def upstream(req):
            calls.append(str(req.url))
            return httpx.Response(200, json={"status": "auth_failed", "auth_status": "UNAUTHENTICATED"})
        with self.upstream(upstream):
            result = await self.request("POST", "/gateway/worker-route/2", {"worker_url": NEW})
        self.assertEqual(result.status_code, 409)
        self.assertEqual(self.account.url, OLD + "/slot/2")
        self.assertEqual(self.routing.accounts, {})
        self.assertEqual(calls, [NEW + "/slot/2/health"])

    async def test_successful_switch_requires_chat_and_persists(self):
        calls = []
        def upstream(req):
            calls.append(str(req.url))
            return httpx.Response(200, json=HEALTH if req.method == "GET" else {"choices": [{"message": {"content": "OK"}}]})
        with self.upstream(upstream):
            result = await self.request("POST", "/gateway/worker-route/2", {"worker_url": NEW})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(calls, [NEW + "/slot/2/health", NEW + "/slot/2/v1/chat/completions"])
        self.assertEqual(self.account.url, NEW + "/slot/2")
        restored = WorkerRouting(self.routing.path)
        restored.load()
        self.assertEqual(restored.account(2, OLD), NEW)

    async def test_probe_failure_and_busy_account_keep_old(self):
        def upstream(req):
            return httpx.Response(200, json=HEALTH) if req.method == "GET" else httpx.Response(502, json={"error": "synthetic"})
        with self.upstream(upstream):
            result = await self.request("POST", "/gateway/worker-route/2", {"worker_url": NEW})
        self.assertEqual(result.status_code, 502)
        self.assertEqual(self.account.url, OLD + "/slot/2")
        self.account.busy = True
        result = await self.request("POST", "/gateway/worker-route/2", {"worker_url": NEW})
        self.assertEqual(result.status_code, 409)

    async def test_unlisted_worker_is_rejected_without_network(self):
        result = await self.request("POST", "/gateway/worker-route/2", {"worker_url": "http://example.invalid"})
        self.assertEqual(result.status_code, 400)

    async def test_management_follows_migrated_account_and_sends_key(self):
        self.account.url = NEW + "/slot/2"
        calls = []
        def upstream(req):
            calls.append((str(req.url), req.headers.get("authorization")))
            return httpx.Response(202, json={"ok": True})
        with self.upstream(upstream):
            result = await self.request("POST", "/gateway/restart/2")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(calls, [(NEW + "/worker/reload-slot/2", "Bearer synthetic-management")])

    async def test_image_session_stays_on_original_worker(self):
        gateway.remember_worker_session("existing", 2, OLD)
        self.account.url = NEW + "/slot/2"
        calls = []
        def upstream(req):
            calls.append(str(req.url))
            return httpx.Response(200, json={"data": [{"b64_json": "synthetic"}], "session_id": "existing"})
        with self.upstream(upstream):
            result = await self.request("POST", "/v1/images/generations", {"model": "gemini-flash", "prompt": "edit", "session_id": "existing"}, panel=False)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(calls, [OLD + "/slot/2/v1/images/generations"])
        self.assertEqual(self.routing.sessions["existing"]["worker"], OLD)

    async def test_unknown_image_session_is_not_silently_recreated(self):
        result = await self.request("POST", "/v1/images/generations", {"model": "gemini-flash", "prompt": "edit", "session_id": "missing"}, panel=False)
        self.assertEqual(result.status_code, 409)

    async def test_background_edit_stays_on_origin_without_retry(self):
        gateway.remember_worker_session("existing", 2, OLD)
        self.account.url = NEW + "/slot/2"
        calls = []
        def upstream(req):
            calls.append(str(req.url))
            return httpx.Response(200, json={"data": [{"b64_json": "synthetic"}], "session_id": "existing"})
        task = gateway.GenerationTask("synthetic-task", "image")
        with self.upstream(upstream):
            await gateway._run_generation_task(task, {"model": "gemini-flash", "prompt": "edit", "session_id": "existing"}, {})
        self.assertEqual(task.status, "success")
        self.assertEqual(calls, [OLD + "/slot/2/v1/images/generations"])
        self.assertEqual(self.routing.sessions["existing"]["worker"], OLD)
        unknown = gateway.GenerationTask("synthetic-missing", "image")
        await gateway._run_generation_task(unknown, {"model": "gemini-flash", "session_id": "missing"}, {})
        self.assertEqual(unknown.status, "failed")

    async def test_research_poll_keeps_origin_after_migration_and_restart(self):
        self.routing.remember("research", "job", 2, OLD)
        self.routing.switch(2, NEW)
        restored = WorkerRouting(self.routing.path)
        restored.load()
        calls = []
        def upstream(req):
            calls.append(str(req.url))
            return httpx.Response(200, json={"task_id": "job", "slot": 2, "finished": False})
        with patch.object(gateway, "worker_routing", restored), self.upstream(upstream):
            result = await self.request("GET", "/v1/research/job", panel=False)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(calls, [OLD + "/v1/research/job"])

    async def test_old_health_cannot_overwrite_new_destination(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def get(*args, **kw):
            entered.set()
            await release.wait()
            return httpx.Response(200, json={"status": "auth_failed", "auth_status": "UNAUTHENTICATED"})
        task = asyncio.create_task(gateway.check_health(self.account, Mock(get=get)))
        await entered.wait()
        self.account.url = NEW + "/slot/2"
        release.set()
        await task
        self.assertTrue(self.account.healthy)
        self.assertFalse(self.account.needs_cookie)

    async def test_stream_error_is_forwarded_and_counted_as_failure(self):
        wire = b'data: {"error":{"message":"synthetic failure","type":"upstream_error"}}\n\ndata: [DONE]\n\n'
        def upstream(req):
            return httpx.Response(200, content=wire, headers={"content-type": "text/event-stream", "x-oneclick-success": "true"})
        with self.upstream(upstream):
            result = await self.request("POST", "/v1/chat/completions", {"model": "gemini-flash", "messages": [], "stream": True}, panel=False)
        self.assertEqual(result.status_code, 200)
        self.assertIn("synthetic failure", result.text)
        self.assertFalse(gateway.record_model_truth.call_args.kwargs["success"])
        self.assertEqual(self.account.total_errors, 1)

    def test_persist_failure_does_not_change_route(self):
        with patch.object(self.routing, "save", side_effect=OSError("synthetic disk full")):
            with self.assertRaises(OSError):
                self.routing.switch(2, NEW)
        self.assertEqual(self.routing.account(2, OLD), OLD)


if __name__ == "__main__":
    unittest.main()
