"""Offline account-pool counterexamples; no lifespan, services or real env files."""
import ast
import asyncio
import hashlib
import inspect
import json
import logging
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace, MethodType
from unittest.mock import AsyncMock, Mock, patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "app"), str(ROOT / "lib")]
import worker
with patch.dict(os.environ, {"SECURE_1PSID": "", "SECURE_1PSIDTS": "", "API_KEY": ""}):
    import main
import slot as slot_module
from slot import Slot, label_level, request_level, best_flash_model
from gemini_webapi import GeminiClient
from gemini_webapi.client import _StreamFlags
from gemini_webapi.constants import AccountStatus, Model
from gemini_webapi.types import AvailableModel, Candidate

# Gateway reads .env at import; suppress that read rather than inspect credentials.
_read_text = Path.read_text
with patch.object(Path, "read_text", lambda p, *a, **kw: "" if p.name == ".env" else _read_text(p, *a, **kw)):
    import gateway
HTTPClient = httpx.AsyncClient


class LiveASGITransport(httpx.AsyncBaseTransport):
    """Expose ASGI body messages immediately; httpx.ASGITransport buffers them."""
    def __init__(self, app):
        self.app = app

    async def handle_async_request(self, request):
        body = await request.aread()
        started = asyncio.get_running_loop().create_future()
        chunks = asyncio.Queue()
        received = False

        async def receive():
            nonlocal received
            if not received:
                received = True
                return {"type": "http.request", "body": body, "more_body": False}
            await asyncio.Event().wait()

        async def send(message):
            if message["type"] == "http.response.start":
                started.set_result(message)
            elif message["type"] == "http.response.body":
                await chunks.put(message.get("body", b""))

        async def run():
            try:
                await self.app({
                    "type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
                    "http_version": "1.1", "method": request.method, "scheme": "http",
                    "path": request.url.path, "raw_path": request.url.raw_path,
                    "query_string": request.url.query, "root_path": "",
                    "headers": [(k.lower(), v) for k, v in request.headers.raw], "server": ("offline.test", 80),
                    "client": ("offline.test", 1234),
                }, receive, send)
            except BaseException as exc:
                if not started.done():
                    started.set_exception(exc)
                raise
            finally:
                await chunks.put(None)

        task = asyncio.create_task(run())

        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                while (chunk := await chunks.get()) is not None:
                    yield chunk
                await task

            async def aclose(self):
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        try:
            start = await started
        except BaseException:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        return httpx.Response(start["status"], headers=start["headers"], stream=Body())


def model(label, token="a" * 16, capacity=2, available=True):
    return AvailableModel(model_id=token, model_name="stale-alias", display_name=label,
                          description=label, capacity=capacity, is_available=available)


def make_slot(num, *models, status=AccountStatus.AVAILABLE):
    client = SimpleNamespace(_model_registry={m.model_id: m for m in models},
                             account_status=status, cookies={},
                             generate_content=AsyncMock(), refresh_models=AsyncMock())
    slot = Slot(num=num, client=client)
    slot.state.update(initializing=False, auth_status=status.name)
    return slot


class PoolCounterexamples(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tmp = Path(self.temp.name)
        for target, value in (
            ("socket.create_connection", Mock(side_effect=AssertionError("Network forbidden"))),
            ("socket.socket.connect", Mock(side_effect=AssertionError("Network forbidden"))),
            ("curl_cffi.requests.AsyncSession.request", AsyncMock(side_effect=AssertionError("Network forbidden"))),
        ):
            p = patch(target, value); p.start(); self.addCleanup(p.stop)
        for obj, name, value in (
            (worker, "slots", {}), (worker, "_models_cache", {}), (worker, "_pool_best_labels", {}),
            (worker, "ENVS_DIR", self.tmp), (worker, "API_KEY", ""),
            (worker, "persist_worker_event", Mock(side_effect=lambda event, **kw: event)),
            (gateway, "containers", {}), (gateway, "container_groups", {}),
            (gateway, "group_round_robin", {}), (gateway, "group_defs", []), (gateway, "API_KEY", ""),
            (gateway, "_models_cache_time", float("inf")), (gateway, "_known_model_levels", set()),
            (gateway, "MODEL_TRUTH_FILE", self.tmp / "truth.json"),
            (gateway, "add_log", Mock()), (gateway, "ENVS_DIR", self.tmp),
            (main, "_model_slot", Slot(num=0)), (main, "_main_best_labels", {}),
        ):
            p = patch.object(obj, name, value); p.start(); self.addCleanup(p.stop)
        gateway.app.dependency_overrides[gateway.verify_auth] = lambda: None
        self.addCleanup(gateway.app.dependency_overrides.clear)
        logging.getLogger("httpx").setLevel(logging.WARNING)

    def install(self, *slots):
        worker.slots.update({s.num: s for s in slots})

    async def post(self, name="gemini-6-flash", num=1, stream=False, app=None, **extra):
        async with HTTPClient(transport=httpx.ASGITransport(app=app or worker.app), base_url="http://offline.test") as client:
            path = "/v1/chat/completions" if app else f"/slot/{num}/v1/chat/completions"
            return await client.post(path, json={"model": name, "messages": [{"role": "user", "content": "Hi"}], "stream": stream, **extra})

    async def parsed_output(self, label="3.8 Flash"):
        payload = [None] * 43
        payload[1] = []
        payload[4] = [["candidate"]]
        payload[42] = label
        client = SimpleNamespace(_parse_candidates=Mock(return_value=[Candidate(rcid="r", text="Hello", text_delta="Hello")]))
        return await GeminiClient._process_stream_part(
            client, [None, None, json.dumps(payload)], Model.BASIC_FLASH, None, {}, {}, _StreamFlags())

    async def output_stream(self, label="3.8 Flash", failure=False):
        output = await self.parsed_output(label)
        async def generate(*args, **kwargs):
            if failure:
                raise RuntimeError("mock failure")
            yield output
        return Mock(side_effect=generate)

    def container(self, slot, group=None):
        c = gateway.Container(slot.num, 8000 + slot.num)
        c.healthy = True
        c.record_error = Mock()
        gateway.update_container_models(c, worker._slot_models(slot))
        gateway.containers[c.num] = c
        if group:
            gateway.container_groups[c.num] = group
            if group not in gateway.group_defs:
                gateway.group_defs.append(group)
        return c

    async def test_counterexample_level_ignores_version(self):
        s = make_slot(1, model("3.8 Flash"), model("3.1 Pro", "b" * 16))
        self.install(s)
        for name, level in [("gemini-6-flash", "flash"), ("gemini-3.6-flash", "flash"),
                            ("gemini-3.8-flash", "flash"), ("gemini-5-pro", "pro"), ("anything", "flash"), ("pro-gemini-6-flash", "flash")]:
            with self.subTest(name=name):
                self.assertEqual(worker.resolve_model_for_chat(name, s.client)[1]["model_level"], level)
                self.assertEqual(request_level(name, {"argon", "pro", "flash"}), level)
        self.assertEqual(request_level("gemini-4-argon-pro-flash", {"argon"}), "argon")
        self.assertEqual(request_level("gemini-4-flash-lite", {"flash"}), "flash-lite")

    async def test_counterexample_free_uses_best_flash_token(self):
        paid = make_slot(1, model("3.8 Flash", "a" * 16))
        older = make_slot(2, model("3.6 Flash", "b" * 16, capacity=4))
        free = make_slot(3, model("Auto", "c" * 16, capacity=1))
        self.install(paid, older, free)
        free.client.generate_content_stream = await self.output_stream("3.6 Flash")
        response = await self.post(num=3)
        self.assertEqual(response.status_code, 200)
        sent = free.client.generate_content_stream.call_args.kwargs["model"]
        self.assertEqual(sent.model_id, "a" * 16)
        self.assertNotEqual(sent.model_id, "c" * 16)
        self.assertEqual(worker._slot_models(free)[0]["id"], "gemini-3.6-flash")
        worker.slots.clear(); self.install(free)
        fallback = best_flash_model([])
        self.assertEqual(worker.resolve_model_for_chat("flash", free.client)[0], fallback)
        self.assertTrue(fallback.advanced_only)
        free.actual_labels.clear()
        self.assertEqual(worker._slot_models(free)[0]["id"], "gemini-flash")

    async def test_counterexample_free_pro_probe(self):
        free = make_slot(1, model("3.1 Pro", "b" * 16, capacity=1))
        self.install(free)
        for label, expected in [("3.6 Flash", False), ("3.1 Pro", True)]:
            free._pro_probe_done = False
            free.client.generate_content.return_value = await self.parsed_output(label)
            await free.probe_free_pro()
            self.assertEqual("pro" in worker._slot_level_models(free), expected)
            self.assertEqual(free.client.generate_content.call_args.kwargs["model"].model_id, "b" * 16)
        self.assertIn("该号 Pro 不可用，实际给 3.6 Flash", str(free.model_events))
        calls = free.client.generate_content.await_count
        await free.probe_free_pro()
        self.assertEqual(free.client.generate_content.await_count, calls)
        free.observe_actual("pro", "3.6 Flash")
        self.assertNotIn("pro", worker._slot_level_models(free))

    async def test_counterexample_actual_model_from_42(self):
        slot = make_slot(1, model("3.8 Flash")); self.install(slot)
        for label in ("3.6 Flash", None, 42, ""):
            for stream in (False, True):
                with self.subTest(label=label, stream=stream):
                    slot.client.generate_content_stream = await self.output_stream(label)
                    response = await self.post(stream=stream)
                    expected = label if isinstance(label, str) and label else "unknown"
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.headers["X-OneClick-Model-Level"], "flash")
                    if stream:
                        self.assertNotIn("X-OneClick-Actual-Model", response.headers)
                        chunks = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: {")]
                        self.assertTrue(chunks)
                        self.assertTrue(all(chunk["model"] == (expected if expected != "unknown" else "flash") for chunk in chunks))
                    else:
                        self.assertEqual(response.headers["X-OneClick-Actual-Model"], expected)
                        self.assertEqual(response.json()["model"], expected)

    async def test_counterexample_new_model_auto_pool(self):
        s = make_slot(1, model("3.8 Flash")); other = make_slot(2, model("3.8 Flash"))
        self.install(s, other)
        await worker.get_runtime_models(s.client, 1)
        argon = model("4 Argon", "c" * 16)
        async def refresh():
            s.client._model_registry = {**s.client._model_registry, argon.model_id: argon}
        s.client.refresh_models.side_effect = refresh
        await worker._refresh_slot_models(s)
        await worker._refresh_slot_models(s)
        entries = [e for e in s.model_events if e["message"] == "出现新模型 4 Argon"]
        self.assertEqual(len(entries), 1)
        data = await worker.pool_models()
        self.assertIn("gemini-4-argon", [m["id"] for m in data["data"]])
        one, two = self.container(s), self.container(other)
        self.assertIs(gateway.get_next_available(level="argon"), one)
        self.assertIsNone(gateway.get_next_available(group="other", level="argon"))
        chosen, trace = worker.resolve_model_for_chat("gemini-4-argon", s.client)
        self.assertIs(chosen, argon)
        with self.assertRaises(worker.ModelNotFoundError):
            worker.resolve_model_for_chat("gemini-4-argon", other.client)

    def test_counterexample_no_hardcoded_tokens(self):
        functions = [slot_module.label_level, slot_module.request_level, slot_module.registry_levels,
                     slot_module.best_flash_model, slot_module.model_label, slot_module.client_capacity,
                     Slot.level_models, worker.resolve_model_for_chat, main.resolve_model_for_chat]
        import re
        for function in functions:
            with self.subTest(function=function.__qualname__):
                self.assertIsNone(re.search(r'[\'\"][0-9a-fA-F]{16}[\'\"]', inspect.getsource(function)))
        for label, level in [("3.1 Pro", "pro"), ("3.5 Flash-Lite", "flash-lite"),
                             ("3.8 Flash", "flash"), ("Auto", None), ("4 Argon", "argon")]:
            self.assertEqual(label_level(label), level)

    def test_counterexample_cookie_writeback(self):
        path = self.tmp / "account7.env"
        original = b"# keep\r\nSECURE_1PSID=old\r\nOTHER=a=b\r\nSECURE_1PSIDTS=old-ts\r\nLAST=yes"
        path.write_bytes(original)
        s = make_slot(7, model("Auto", capacity=1)); s.env_path = path
        s.client.cookies = {"__Secure-1PSID": "new-secret", "__Secure-1PSIDTS": "new-ts-secret"}
        with patch.object(slot_module.time, "monotonic", return_value=1000), self.assertLogs(slot_module.logger, "INFO") as logs:
            self.assertTrue(s.writeback_cookies())
        expected = original.replace(b"SECURE_1PSID=old", b"SECURE_1PSID=new-secret").replace(b"SECURE_1PSIDTS=old-ts", b"SECURE_1PSIDTS=new-ts-secret")
        self.assertEqual(path.read_bytes(), expected)
        self.assertNotIn("new-secret", str(logs.output))
        self.assertNotIn("new-ts-secret", str(logs.output))
        self.assertIn("slot 7 cookie 已写回", str(logs.output))
        s.client.cookies["__Secure-1PSID"] = "next-secret"
        with patch.object(slot_module.time, "monotonic", return_value=1599):
            self.assertFalse(s.writeback_cookies())
        self.assertEqual(path.read_bytes(), expected)
        with patch.object(slot_module.time, "monotonic", return_value=1600):
            self.assertTrue(s.writeback_cookies())
        self.assertFalse(list(self.tmp.glob("tmp*")))

    async def test_counterexample_unauth_excluded(self):
        s = make_slot(1, model("Auto", capacity=1, available=False), status=AccountStatus.UNAUTHENTICATED)
        self.install(s)
        self.assertEqual(s.level_models(), {})
        self.assertEqual(await worker.get_runtime_models(s.client, 1), [])
        self.assertEqual((await worker.pool_models())["data"], [])
        with self.assertRaises(worker.ModelNotFoundError):
            worker.resolve_model_for_chat("flash", s.client)

    async def test_counterexample_busy_vs_missing(self):
        s = make_slot(1, model("3.8 Flash")); self.install(s)
        c = self.container(s); c.busy = True
        response = await self.post(app=gateway.app)
        self.assertEqual(response.status_code, 503)
        response = await self.post(name="gemini-5-pro", app=gateway.app)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "model_not_found")
        self.assertIn("flash", response.json()["error"]["message"])
        s.client.generate_content_stream = await self.output_stream()
        async with s.lock:
            self.assertEqual((await self.post()).status_code, 200)
            self.assertEqual((await self.post(name="pro")).status_code, 404)

    def test_counterexample_media_unchanged(self):
        baseline = json.loads((ROOT / "tests/media_probe_baseline.json").read_text())
        for filename, expected in baseline.items():
            tree = ast.parse((ROOT / filename).read_text())
            nodes = {n.name: n for n in tree.body if hasattr(n, "name")}
            for name, digest in expected.items():
                with self.subTest(file=filename, function=name):
                    self.assertEqual(hashlib.sha256(ast.dump(nodes[name], include_attributes=False).encode()).hexdigest(), digest)
        for name in ("pro", "argon", "anything"):
            self.assertIs(worker.resolve_model_for_media(name)[0], Model.BASIC_FLASH)
            self.assertIs(main.resolve_model_for_media(name)[0], Model.BASIC_FLASH)

    async def test_counterexample_refresh_no_reinit_and_failure_retains_registry(self):
        old = {"old": model("3.8 Flash")}
        fake = SimpleNamespace(_running=True, client=object(), _model_registry=old,
                               _fetch_user_status=AsyncMock(side_effect=RuntimeError("mock failure")))
        raw = AsyncMock(return_value=SimpleNamespace(text="unused"))
        with patch.object(GeminiClient._batch_execute, "__wrapped__", raw), patch.object(GeminiClient, "init", AsyncMock()) as init:
            with self.assertRaises(RuntimeError):
                await GeminiClient.refresh_models(fake)
            self.assertIs(fake._model_registry, old)
            init.assert_not_awaited()
            self.assertFalse(raw.call_args.kwargs["close_on_error"])
            self.assertFalse(raw.call_args.kwargs["update_cookies"])
        self.assertFalse(hasattr(GeminiClient.refresh_models, "__wrapped__"))

    async def test_counterexample_group_isolation_and_gateway_union(self):
        a = make_slot(1, model("3.8 Flash")); b = make_slot(2, model("4 Argon", "b" * 16))
        self.install(a, b)
        self.container(a, "alpha"); self.container(b, "beta")
        result = await gateway.list_models()
        self.assertEqual({m["id"] for m in result["data"]}, {"alpha-gemini-3.8-flash", "beta-gemini-4-argon"})
        self.assertEqual((await self.post(name="alpha-gemini-4-argon", app=gateway.app)).status_code, 404)
        self.assertEqual((await self.post(app=gateway.app)).status_code, 404)
        self.assertIsNone(gateway.get_next_available(level="flash"))

    async def test_counterexample_gateway_truth_success_and_failure(self):
        s = make_slot(1, model("3.8 Flash")); self.install(s); self.container(s)
        headers = {"X-OneClick-Actual-Model": "3.6 Flash", "X-OneClick-Model-Level": "flash"}
        gateway.record_model_truth(1, "/v1/chat/completions", None, headers, requested_model="gemini-9-flash", level="flash")
        gateway.record_model_truth(1, "/v1/chat/completions", None, {}, requested_model="flash", level="flash", success=False)
        result = await gateway.get_model_truth()
        level = result["levels"][0]
        self.assertEqual(level["count"], 2)
        self.assertEqual(level["success_rate"], 0.5)
        self.assertEqual(level["actual_models"], {"3.6 Flash": 1, "unknown": 1})
        self.assertEqual(result["accounts"][0]["levels"], ["flash"])

    async def test_counterexample_main_sync(self):
        s = make_slot(1, model("3.8 Flash"), model("4 Argon", "b" * 16)); self.install(s)
        s.client.generate_content_stream = await self.output_stream("3.8 Flash")
        with patch.object(main, "gemini_client", s.client), patch.object(main, "get_or_create_client", AsyncMock(return_value=s.client)):
            self.assertEqual(main.resolve_model_for_chat("gemini-6-flash")[1]["model_level"], "flash")
            self.assertEqual(main.resolve_model_for_chat("gemini-4-argon")[1]["model_level"], "argon")
            for stream in (False, True):
                response = await self.post(app=main.app, stream=stream)
                self.assertEqual(response.status_code, 200)
                if stream:
                    self.assertNotIn("X-OneClick-Actual-Model", response.headers)
                    chunks = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: {")]
                    self.assertTrue(chunks)
                    self.assertTrue(all(chunk["model"] == "3.8 Flash" for chunk in chunks))
                else:
                    self.assertEqual(response.headers["X-OneClick-Actual-Model"], "3.8 Flash")
            self.assertEqual(len(await main.get_runtime_models()), 2)

    async def test_counterexample_per_slot_cache_and_versions(self):
        a = make_slot(1, model("3.8 Flash")); b = make_slot(2, model("3.6 Pro"))
        self.install(a, b)
        self.assertEqual((await worker.get_runtime_models(a.client, 1))[0]["level"], "flash")
        self.assertEqual((await worker.get_runtime_models(b.client, 2))[0]["level"], "pro")
        a.client._model_registry = {"x": model("3.10 Flash")}
        self.assertEqual((await worker.get_runtime_models(a.client, 1))[0]["id"], "gemini-3.10-flash")
        self.assertEqual(len([e for e in a.model_events if "3.10 Flash" in e["message"]]), 1)
        await worker.get_runtime_models(a.client, 1)
        self.assertEqual(len([e for e in a.model_events if "3.10 Flash" in e["message"]]), 1)

    async def test_counterexample_gateway_routes_by_level_end_to_end(self):
        a = make_slot(1, model("3.8 Flash")); b = make_slot(2, model("4 Argon", "b" * 16))
        self.install(a, b); self.container(a); self.container(b)
        calls = []
        def upstream(request):
            calls.append(request.url.path)
            return httpx.Response(200, json={"model": "4 Argon"}, headers={
                "X-OneClick-Actual-Model": "4 Argon", "X-OneClick-Model-Level": "argon", "X-OneClick-Success": "true"})
        with patch.object(gateway.httpx, "AsyncClient", side_effect=lambda **kw: HTTPClient(transport=httpx.MockTransport(upstream), **kw)):
            response = await self.post(name="gemini-99-argon", app=gateway.app)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-OneClick-Actual-Model"], "4 Argon")
        self.assertEqual(len(calls), 1)
        self.assertEqual(gateway.list_model_truth_entries()[0]["container"], 2)

    async def test_counterexample_probe_does_not_change_membership(self):
        s = make_slot(1, model("3.1 Pro")); self.install(s)
        s.client.generate_content_stream = await self.output_stream("3.6 Flash")
        response = await self.post(name="pro", oneclick_force_model_token="b" * 16)
        self.assertEqual(response.status_code, 200)
        self.assertIn("pro", s.level_models())
        self.assertFalse(s.model_events)
        c = self.container(s)
        gateway.record_model_truth(1, "/v1/chat/completions", None, {
            "X-OneClick-Requested-Model": "pro", "X-OneClick-Model-Level": "pro",
            "X-OneClick-Actual-Model": "3.6 Flash", "X-OneClick-Model-Resolution": "debug-forced"})
        self.assertIn("pro", c.model_levels)

    async def test_counterexample_tail_frame_keeps_observed_label(self):
        fake = SimpleNamespace(_parse_candidates=Mock(return_value=[Candidate(rcid="r", text="Hi")]))
        flags = _StreamFlags()
        async def parse(label, candidates=True):
            payload = [None] * 43
            payload[1], payload[4], payload[42] = [], [["candidate"]] if candidates else [], label
            return await GeminiClient._process_stream_part(fake, [None, None, json.dumps(payload)], Model.BASIC_FLASH, None, {}, {}, flags)
        first = await parse("3.1 Pro")
        last = await parse(None)
        self.assertEqual(last.actual_model, "3.1 Pro")
        await parse("3.6 Pro", candidates=False)
        self.assertEqual(last.actual_model, "3.6 Pro")

    async def test_counterexample_login_clears_previous_actual_labels(self):
        s = make_slot(1, model("Auto", capacity=1))
        s.psid, s.psidts = "mock", "mock-ts"
        s.actual_labels = {"flash": "9 Flash"}
        s.last_actual_model = "9 Flash"
        fresh = make_slot(2, model("Auto", capacity=1)).client
        fresh.init = AsyncMock()
        with patch("gemini_webapi.GeminiClient", return_value=fresh):
            await s.init_client()
        self.assertEqual(s.actual_labels, {})
        self.assertEqual(s.last_actual_model, "unknown")

    def test_counterexample_refresh_cookie_uses_transport_jar(self):
        s = make_slot(1, model("3.8 Flash")); s.env_path = self.tmp / "account1.env"
        s.env_path.write_text("SECURE_1PSID=old\nSECURE_1PSIDTS=old-ts\nOTHER=keep\n")
        s.client.cookies = {"__Secure-1PSID": "old", "__Secure-1PSIDTS": "old-ts"}
        s.client.client = SimpleNamespace(cookies={"__Secure-1PSID": "new", "__Secure-1PSIDTS": "new-ts"})
        self.assertTrue(s.writeback_cookies())
        self.assertIn("SECURE_1PSID=new\n", s.env_path.read_text())


    async def test_counterexample_gateway_transport_failure_releases_account(self):
        s = make_slot(1, model("3.1 Pro")); self.install(s); c = self.container(s)
        def upstream(request):
            raise httpx.ConnectError("mock offline", request=request)
        with patch.object(gateway.httpx, "AsyncClient", side_effect=lambda **kw: HTTPClient(transport=httpx.MockTransport(upstream), **kw)):
            response = await self.post(name="pro", app=gateway.app)
        self.assertEqual(response.status_code, 502)
        self.assertFalse(c.busy)
        self.assertIn("pro", c.model_levels)
        entry = gateway.list_model_truth_entries()[0]
        self.assertFalse(entry["success"])
        self.assertEqual(entry["actual_model"], "unknown")

    async def test_counterexample_media_background_dispatch_unchanged(self):
        s = make_slot(1, model("Auto", capacity=1)); self.install(s); c = self.container(s)
        gateway.update_container_models(c, [])  # Media must not depend on chat capabilities.
        for kind in ("image", "video", "music"):
            task = gateway.GenerationTask(task_id=kind, task_type=kind)
            def upstream(request):
                return httpx.Response(200, json={"data": [{"url": "mock-media"}]})
            with patch.object(gateway.httpx, "AsyncClient", side_effect=lambda **kw: HTTPClient(transport=httpx.MockTransport(upstream), **kw)):
                await gateway._run_generation_task(task, {"model": "gemini-99-argon", "prompt": "mock"}, {})
            self.assertEqual(task.status, "success")
            self.assertFalse(c.busy)


    async def test_counterexample_refresh_rpc_offline_and_cookie_check(self):
        data = [None] * 18
        data[14], data[16], data[17] = int(AccountStatus.AVAILABLE), [8], []
        raw_model = [None] * 20
        raw_model[0], raw_model[1], raw_model[2], raw_model[11] = "a" * 16, "Argon", "tagline", "4 Argon"
        data[15] = [raw_model]
        response = httpx.Response(200, text=json.dumps([["rpc", None, json.dumps(data)]]))
        transport = SimpleNamespace(post=AsyncMock(return_value=response), cookies={
            "__Secure-1PSID": "new", "__Secure-1PSIDTS": "new-ts"})
        fake = SimpleNamespace(_running=True, client=transport, _model_registry={},
                               account_status=AccountStatus.AVAILABLE, _reqid=10000,
                               build_label="", session_id="", account_index=0, access_token="mock", cookies={},
                               close=AsyncMock())
        fake._fetch_user_status = MethodType(GeminiClient._fetch_user_status, fake)
        fake.refresh_models = MethodType(GeminiClient.refresh_models, fake)
        s = Slot(num=1, client=fake, env_path=self.tmp / "account1.env")
        s.state["initializing"] = False
        s.env_path.write_text("SECURE_1PSID=old\nSECURE_1PSIDTS=old-ts\nKEEP=yes\n")
        self.install(s)
        with patch("gemini_webapi.client.rotate_1psidts", AsyncMock(side_effect=AssertionError("No rotate"))) as rotate, patch.object(GeminiClient, "init", AsyncMock(side_effect=AssertionError("No init"))) as init:
            await worker._refresh_slot_models(s)
            await worker._refresh_slot_models(s)
            rotate.assert_not_awaited(); init.assert_not_awaited(); fake.close.assert_not_awaited()
        self.assertEqual(len(s.model_events), 1)
        self.assertEqual(s.model_events[0]["message"], "出现新模型 4 Argon")
        self.assertIn("argon", s.level_models())
        self.assertIn("SECURE_1PSID=new\n", s.env_path.read_text())
        self.assertIn("KEEP=yes\n", s.env_path.read_text())

    async def test_counterexample_login_reload_pro_probe_once(self):
        s = Slot(num=1, psid="mock", psidts="mock-ts")
        first = make_slot(1, model("3.1 Pro", capacity=1)).client
        second = make_slot(1, model("3.1 Pro", capacity=1)).client
        for client, label in ((first, "3.6 Flash"), (second, "3.1 Pro")):
            client.init = AsyncMock()
            client.generate_content.return_value = await self.parsed_output(label)
        with patch("gemini_webapi.GeminiClient", side_effect=[first, second]):
            await s.init_client()
            self.assertNotIn("pro", s.level_models())
            await s.probe_free_pro()
            first.generate_content.assert_awaited_once()
            await s.reload(psid="new-mock", psidts="new-ts")
            self.assertIn("pro", s.level_models())
            second.generate_content.assert_awaited_once()
            self.assertFalse(second.init.call_args.kwargs["auto_refresh"])

    async def test_counterexample_health_events_once_and_unauth_clears_pool(self):
        s = make_slot(1, model("4 Argon")); self.install(s)
        c = self.container(s)
        data = await worker.slot_health(1)
        data = await worker.slot_health(1)
        fake = SimpleNamespace(get=AsyncMock(return_value=httpx.Response(200, json=data)))
        await gateway.check_health(c, fake)
        await gateway.check_health(c, fake)
        messages = [call.args[2] for call in gateway.add_log.call_args_list]
        self.assertEqual(messages.count("slot 1 出现新模型 4 Argon"), 1)
        data["auth_status"] = "UNAUTHENTICATED"
        fake.get.return_value = httpx.Response(200, json=data)
        await gateway.check_health(c, fake)
        self.assertEqual(c.model_levels, [])
        self.assertTrue(c.needs_cookie)
        self.assertEqual((await self.post(name="gemini-4-argon", app=gateway.app)).status_code, 404)

    async def test_counterexample_success_request_cookie_writeback(self):
        s = make_slot(1, model("3.8 Flash")); self.install(s)
        s.env_path = self.tmp / "account1.env"
        s.env_path.write_text("SECURE_1PSID=old\nSECURE_1PSIDTS=old-ts\nKEEP=yes\n")
        s.client.cookies = {"__Secure-1PSID": "new", "__Secure-1PSIDTS": "new-ts"}
        s.client.generate_content_stream = await self.output_stream()
        response = await self.post()
        self.assertEqual(response.status_code, 200)
        self.assertIn("SECURE_1PSID=new\n", s.env_path.read_text())
        self.assertIn("KEEP=yes\n", s.env_path.read_text())

    async def test_counterexample_failed_response_preserves_known_actual(self):
        s = make_slot(1, model("3.8 Flash")); self.install(s); c = self.container(s)
        def upstream(request):
            return httpx.Response(500, json={"error": "mock"}, headers={"X-OneClick-Actual-Model": "3.6 Flash"})
        with patch.object(gateway.httpx, "AsyncClient", side_effect=lambda **kw: HTTPClient(transport=httpx.MockTransport(upstream), **kw)):
            response = await self.post(app=gateway.app)
        self.assertEqual(response.status_code, 502)
        self.assertFalse(c.busy)
        entry = gateway.list_model_truth_entries()[0]
        self.assertEqual(entry["actual_model"], "3.6 Flash")
        self.assertFalse(entry["success"])


    async def test_counterexample_actual_label_survives_empty_poll(self):
        def response(label, candidates):
            payload = [None] * 43
            payload[1], payload[4], payload[42] = [], [["candidate"]] if candidates else [], label
            wire = json.dumps([[None, None, json.dumps(payload)]]).encode()
            async def content():
                yield str(len(wire) + 2).encode() + b"\n" + wire + b"\n"
            return SimpleNamespace(status_code=200, aiter_content=content)
        transport = SimpleNamespace(request=AsyncMock(side_effect=[response("3.1 Pro", False), response(None, True)]), cookies={})
        def parse_candidates(candidates, chat, texts, thoughts, flags):
            flags.has_candidates = True
            return [Candidate(rcid="r", text="Hello")]
        fake = SimpleNamespace(_reqid=10000, build_label="", session_id="", access_token="mock", account_index=0,
                               client=transport, cookies={}, timeout=300, watchdog_timeout=180,
                               _parse_candidates=parse_candidates)
        fake._process_stream_part = MethodType(GeminiClient._process_stream_part, fake)
        with patch("gemini_webapi.client.asyncio.sleep", AsyncMock()):
            outputs = [output async for output in GeminiClient._generate.__wrapped__(fake, "Hi", model=model("3.1 Pro"))]
        self.assertEqual(transport.request.await_count, 2)
        self.assertEqual(outputs[-1].actual_model, "3.1 Pro")

    async def test_counterexample_stream_not_buffered(self):
        for target in (worker.app, main.app, gateway.app):
            with self.subTest(target=target.title):
                s = make_slot(1, model("3.8 Flash"))
                self.install(s)
                self.container(s).url = "http://offline.test/slot/1"
                gates = [asyncio.Event(), asyncio.Event()]
                produced = []

                async def generate(*args, **kwargs):
                    for index, label in enumerate((None, "3.6 Flash", "3.6 Flash")):
                        if index:
                            await gates[index - 1].wait()
                        output = await self.parsed_output(label)
                        produced.append(index)
                        yield output

                s.client.generate_content_stream = Mock(side_effect=generate)
                path = "/slot/1/v1/chat/completions" if target is worker.app else "/v1/chat/completions"
                with patch.object(main, "gemini_client", s.client), patch.object(main, "get_or_create_client", AsyncMock(return_value=s.client)), patch.object(
                    gateway.httpx, "AsyncClient", side_effect=lambda **kw: HTTPClient(transport=LiveASGITransport(worker.app), **kw)
                ):
                    async with HTTPClient(transport=LiveASGITransport(target), base_url="http://offline.test") as client:
                        async with asyncio.timeout(2):
                            async with client.stream("POST", path, json={"model": "flash", "messages": [{"role": "user", "content": "Hi"}], "stream": True}) as response:
                                self.assertEqual(response.status_code, 200)
                                self.assertNotIn("X-OneClick-Actual-Model", response.headers)
                                self.assertEqual(response.headers["X-OneClick-Model-Level"], "flash")
                                content_models = []
                                all_models = []
                                async for line in response.aiter_lines():
                                    if not line.startswith("data: {"):
                                        continue
                                    chunk = json.loads(line[6:])
                                    all_models.append(chunk["model"])
                                    if chunk["choices"][0]["delta"].get("content"):
                                        content_models.append(chunk["model"])
                                        if len(content_models) == 1:
                                            self.assertEqual(produced, [0])
                                            gates[0].set()
                                        elif len(content_models) == 2:
                                            self.assertEqual(produced, [0, 1])
                                            gates[1].set()
                                self.assertEqual(content_models, ["flash", "3.6 Flash", "3.6 Flash"])
                                self.assertEqual(all_models, ["flash", "flash", "3.6 Flash", "3.6 Flash", "3.6 Flash"])
                if target is gateway.app:
                    self.assertEqual(gateway.list_model_truth_entries()[0]["actual_model"], "3.6 Flash")

        # Fragmented UTF-8/JSON, coalesced events and trailing level-only chunks
        # must not replace the last real label; stale HTTP labels are ignored.
        for labels, expected in ((["flash", "argon", "unknown"], "unknown"),
                                 (["flash", "3.6 Flash", "4 Flash 预览", "flash"], "4 Flash 预览")):
            with self.subTest(labels=labels):
                gateway.MODEL_TRUTH_FILE.unlink(missing_ok=True)
                c = self.container(s)
                gateway._known_model_levels.add("argon")
                wire = (": keepalive\r\n\r\ndata: malformed\n\n" + "".join(
                    "data: " + json.dumps({"model": label}, ensure_ascii=False) + "\r\n\r\n"
                    for label in labels
                ) + "data: [DONE]\n\n").encode()

                class FragmentedBody(httpx.AsyncByteStream):
                    async def __aiter__(self):
                        for byte in wire:
                            yield bytes([byte])

                def upstream(request):
                    return httpx.Response(200, stream=FragmentedBody(), headers={
                        "Content-Type": "text/event-stream", "X-OneClick-Actual-Model": "stale",
                        "X-OneClick-Model-Level": "flash"})

                with patch.object(gateway.httpx, "AsyncClient", side_effect=lambda **kw: HTTPClient(transport=httpx.MockTransport(upstream), **kw)):
                    response = await self.post(app=gateway.app, stream=True)
                self.assertEqual(response.content, wire)
                self.assertNotIn("X-OneClick-Actual-Model", response.headers)
                self.assertEqual(gateway.list_model_truth_entries()[0]["actual_model"], expected)
                self.assertFalse(c.busy)

    async def test_counterexample_concurrent_chat_same_slot(self):
        s = make_slot(1, model("3.8 Flash")); self.install(s)
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def generate(*args, **kwargs):
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            yield await self.parsed_output()

        s.client.generate_content_stream = Mock(side_effect=generate)
        first = asyncio.create_task(self.post())
        second = None
        try:
            await asyncio.wait_for(entered.wait(), 2)
            entered.clear()
            second = asyncio.create_task(self.post())
            await asyncio.wait_for(entered.wait(), 2)
            self.assertEqual(calls, 2)
            release.set()
            responses = await asyncio.gather(first, second)
            self.assertEqual([r.status_code for r in responses], [200, 200])
        finally:
            release.set()
            await asyncio.gather(*(t for t in (first, second) if t), return_exceptions=True)

    async def test_counterexample_refresh_does_not_block_chat(self):
        s = make_slot(1, model("3.8 Flash")); self.install(s)
        entered, release = asyncio.Event(), asyncio.Event()

        async def refresh():
            entered.set()
            await release.wait()

        s.client.refresh_models.side_effect = refresh
        s.client.generate_content_stream = await self.output_stream()
        task = asyncio.create_task(worker._refresh_slot_models(s))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            response = await asyncio.wait_for(self.post(), 2)
            self.assertEqual(response.status_code, 200)
            self.assertFalse(task.done())
            self.assertEqual(response.json()["model"], "3.8 Flash")
        finally:
            release.set()
            await task

        # The single-account entry point must also keep refresh off its init lock.
        entered.clear(); release.clear()
        with patch.object(main, "gemini_client", s.client), patch.object(main, "MODEL_REFRESH_INTERVAL_SECONDS", 0):
            task = asyncio.create_task(main._refresh_models_loop())
            try:
                await asyncio.wait_for(entered.wait(), 2)
                response = await asyncio.wait_for(self.post(app=main.app), 2)
                self.assertEqual(response.status_code, 200)
                self.assertFalse(task.done())
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        # Slow file writes run off-loop and use a mutex separate from chat.
        file_entered, file_release = threading.Event(), threading.Event()

        def slow_write(*args):
            file_entered.set()
            if not file_release.wait(2):
                raise AssertionError("Chat did not finish while the file write was held")
            return True

        with patch.object(s, "_writeback_cookies", side_effect=slow_write):
            task = asyncio.create_task(asyncio.to_thread(s.writeback_cookies, self.tmp))
            try:
                self.assertTrue(await asyncio.to_thread(file_entered.wait, 2))
                response = await asyncio.wait_for(self.post(), 1)
                self.assertEqual(response.status_code, 200)
                self.assertFalse(task.done())
            finally:
                file_release.set()
                await task



if __name__ == "__main__":
    unittest.main()
