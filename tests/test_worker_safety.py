"""Offline counterexamples against a checkout supplied as argv[1]. No services."""
import asyncio
import base64
import json
import logging
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "app"), str(ROOT / "lib")]
import httpx
import worker
import slot as slot_module
from slot import Slot, parse_env_file
from gemini_webapi import GeminiClient
from gemini_webapi.exceptions import AuthError
from gemini_webapi.constants import AccountStatus

class WorkerSafety(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.client = SimpleNamespace(account_status=AccountStatus.AVAILABLE,
                                      _model_registry={}, cookies={})
        self.slot = Slot(num=7, client=self.client)
        self.slot.state.update(initializing=False, auth_status="AVAILABLE")
        self.slot.writeback_cookies = Mock(return_value=False)
        self.trace = {"requested_model": "gemini-flash", "model_level": "flash",
                      "resolved_model": "gemini-flash", "resolution": "test"}
        for item in [
            patch.object(worker, "slots", {7: self.slot}),
            patch.object(worker, "API_KEY", "synthetic-test-key"),
            patch.object(worker, "WORKER_MANAGEMENT_KEY", "synthetic-management-key"),
            patch.object(worker, "ENVS_DIR", self.directory),
            patch.object(worker, "_research_tasks", {}),
            patch.object(worker, "_slot_tasks", {}),
            patch.object(worker, "_management_locks", {}),
            patch.object(worker, "resolve_model_for_chat", return_value=(worker.Model.BASIC_FLASH, self.trace)),
            patch.object(worker, "log_worker_event", side_effect=lambda event, **kw: event),
            patch.object(worker, "slot_log"),
            patch("socket.socket.connect", side_effect=AssertionError("No network permitted")),
            patch("socket.create_connection", side_effect=AssertionError("No network permitted")),
        ]:
            item.start()
            self.addCleanup(item.stop)

    async def post(self, path, body, authorized=True):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=worker.app),
                                     base_url="http://offline.test") as client:
            return await client.post(path, json=body,
                headers={"Authorization": "Bearer synthetic-test-key"} if authorized else {})

    async def test_stream_failure_is_reported_as_structured_error(self):
        async def broken(*args, **kwargs):
            raise RuntimeError("synthetic upstream failure")
            yield
        self.client.generate_content_stream = broken
        result = await self.post("/slot/7/v1/chat/completions", {
            "model": "gemini-flash", "messages": [{"role": "user", "content": "hi"}], "stream": True})
        self.assertEqual(result.status_code, 200)
        self.assertIn('"error":', result.text)
        self.assertNotIn('"finish_reason": "stop"', result.text)
        self.slot.writeback_cookies.assert_not_called()

    async def test_empty_output_returns_bad_gateway(self):
        async def empty(*args, **kwargs):
            if False:
                yield
        self.client.generate_content_stream = empty
        result = await self.post("/slot/7/v1/chat/completions", {
            "model": "gemini-flash", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(result.status_code, 502)

    async def test_nonstream_failure_removes_uploaded_file(self):
        async def broken(*args, **kwargs):
            raise RuntimeError("synthetic upstream failure")
            yield
        self.client.generate_content_stream = broken
        original = tempfile.NamedTemporaryFile
        with patch.object(worker.tempfile, "NamedTemporaryFile",
                          side_effect=lambda **kwargs: original(dir=self.directory, **kwargs)):
            result = await self.post("/slot/7/v1/chat/completions", {
                "model": "gemini-flash", "messages": [{"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(b"synthetic image").decode()}}
                ]}]})
        self.assertEqual(result.status_code, 500)
        remaining = list(self.directory.iterdir())
        self.assertEqual(remaining, [])

    async def test_unicode_media_model_preserves_successful_result(self):
        output = SimpleNamespace(images=[SimpleNamespace(url="https://example.invalid/image")], text="image", candidates=[])
        chat = SimpleNamespace(send_message=AsyncMock(return_value=output), cid="synthetic")
        self.client.start_chat = Mock(return_value=chat)
        with patch.object(worker, "parse_image_generation_result", AsyncMock(return_value=([{"b64_json": "synthetic"}], []))):
            ascii_result = await self.post("/slot/7/v1/images/generations", {"model": "gemini-flash", "prompt": "test"})
            unicode_result = await self.post("/slot/7/v1/images/generations", {"model": "gemini-flash-测试", "prompt": "test"})
        self.assertEqual(ascii_result.status_code, 200)
        self.assertEqual(unicode_result.status_code, 200)
        self.assertIn("%", unicode_result.headers["x-oneclick-requested-model"])
        self.assertEqual(chat.send_message.await_count, 2)

    async def test_management_rejects_missing_api_key(self):
        with patch.object(worker, "schedule_slot_init", return_value=True) as schedule:
            guarded = await self.post("/slot/7/v1/chat/completions", {
                "model": "gemini-flash", "messages": []}, authorized=False)
            unguarded = await self.post("/worker/reload-slot/7", {}, authorized=False)
        self.assertEqual(guarded.status_code, 401)
        self.assertEqual(unguarded.status_code, 401)
        schedule.assert_not_called()

    async def test_unicode_video_and_music_headers(self):
        self.client.generate_content = AsyncMock(return_value=SimpleNamespace(videos=[SimpleNamespace(url="https://example.invalid/v")], text="video", candidates=[]))
        with patch.object(worker, "parse_video_generation_result", AsyncMock(return_value=([{"url": "synthetic"}], []))):
            result = await self.post("/slot/7/v1/videos/generations", {"model": "gemini-flash-测试", "prompt": "test"})
        self.assertEqual(result.status_code, 200)
        self.assertIn("%", result.headers["x-oneclick-requested-model"])
        media = SimpleNamespace(mp3_url="https://example.invalid/audio", url="", cookies={}, account_index=0, mp3_thumbnail="", thumbnail_url="")
        self.client.generate_content.return_value = SimpleNamespace(media=[media], text="music", candidates=[])
        self.client.watchdog_timeout = 180
        with patch.object(worker, "download_audio_as_base64", AsyncMock(return_value="synthetic")):
            result = await self.post("/slot/7/v1/music/generations", {"model": "gemini-flash-测试", "prompt": "test"})
        self.assertEqual(result.status_code, 200)
        self.assertIn("%", result.headers["x-oneclick-requested-model"])

    async def test_unicode_media_fallback_headers(self):
        failure = RuntimeError("synthetic parse error")
        self.client.start_chat = Mock(return_value=SimpleNamespace(send_message=AsyncMock(side_effect=failure), cid="synthetic"))
        with patch.object(worker, "collect_generated_image_urls", return_value=["https://example.invalid/image"]):
            result = await self.post("/slot/7/v1/images/generations", {"model": "gemini-flash-测试", "prompt": "test"})
        self.assertEqual(result.status_code, 200)
        self.assertIn("%", result.headers["x-oneclick-requested-model"])
        self.client.generate_content = AsyncMock(side_effect=failure)
        with patch.object(worker, "collect_generated_video_urls", return_value=["https://example.invalid/video"]):
            result = await self.post("/slot/7/v1/videos/generations", {"model": "gemini-flash-测试", "prompt": "test"})
        self.assertEqual(result.status_code, 200)
        self.assertIn("%", result.headers["x-oneclick-requested-model"])

    async def test_worker_stream_retry_never_replays_partial_output(self):
        from gemini_webapi.utils.decorators import running
        from gemini_webapi.exceptions import APIError
        calls = []
        @running(retry=2)
        async def stream(client):
            calls.append(True)
            yield "first"
            raise APIError("partial failure")
        client = SimpleNamespace(_running=True, allow_implicit_init=False)
        output = []
        with self.assertRaises(APIError):
            async for item in stream(client):
                output.append(item)
        self.assertEqual(output, ["first"])
        self.assertEqual(len(calls), 1)

    async def test_empty_stream_has_error_and_no_normal_stop(self):
        async def empty(*args, **kwargs):
            if False:
                yield
        self.client.generate_content_stream = empty
        result = await self.post("/slot/7/v1/chat/completions", {
            "model": "gemini-flash", "messages": [], "stream": True})
        self.assertIn('"error":', result.text)
        self.assertNotIn('"finish_reason": "stop"', result.text)
        self.slot.writeback_cookies.assert_not_called()

    async def test_all_management_routes_require_management_key(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=worker.app), base_url="http://offline.test") as client:
            for method, path in [("GET", "/worker/status"), ("GET", "/worker/slot-logs/7"),
                                 ("POST", "/worker/deploy-slot/7"), ("DELETE", "/worker/slot/7"),
                                 ("POST", "/worker/test-slot/7"), ("POST", "/worker/retire-slot/7")]:
                with self.subTest(path=path):
                    result = await client.request(method, path, headers={"Authorization": "Bearer synthetic-test-key"}, json={})
                    self.assertEqual(result.status_code, 401)
            result = await client.get("/worker/status", headers={"Authorization": "Bearer synthetic-management-key"})
            self.assertEqual(result.status_code, 200)
            with patch.object(worker, "WORKER_MANAGEMENT_KEY", ""), patch.object(worker, "API_KEY", ""):
                result = await client.get("/worker/status")
                self.assertEqual(result.status_code, 503)

    async def test_model_resolution_failure_cleans_upload(self):
        original = tempfile.NamedTemporaryFile
        with patch.object(worker.tempfile, "NamedTemporaryFile", side_effect=lambda **kw: original(dir=self.directory, **kw)), \
             patch.object(worker, "resolve_model_for_chat", side_effect=worker.ModelNotFoundError("missing", [])):
            result = await self.post("/slot/7/v1/chat/completions", {
                "model": "missing", "messages": [{"role": "user", "content": [{"type": "image_url",
                "image_url": {"url": "data:image/png;base64,eA=="}}]}], "stream": True})
        self.assertEqual(result.status_code, 404)
        self.assertEqual(list(self.directory.iterdir()), [])

    async def test_cancelled_nonstream_request_cleans_upload(self):
        started = asyncio.Event()
        async def generate(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()
            yield
        self.client.generate_content_stream = generate
        original = tempfile.NamedTemporaryFile
        with patch.object(worker.tempfile, "NamedTemporaryFile", side_effect=lambda **kw: original(dir=self.directory, **kw)):
            job = asyncio.create_task(self.post("/slot/7/v1/chat/completions", {
                "model": "gemini-flash", "messages": [{"role": "user", "content": [{"type": "image_url",
                "image_url": {"url": "data:image/png;base64,eA=="}}]}]}))
            await started.wait()
            self.assertEqual(len(list(self.directory.iterdir())), 1)
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertEqual(self.slot.active_requests, 0)

    async def test_retire_does_not_cancel_running_stream(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def generate(*args, **kwargs):
            started.set()
            await release.wait()
            yield SimpleNamespace(text_delta="still completed", thoughts_delta="", images=[])
        self.client.generate_content_stream = generate
        job = asyncio.create_task(self.post("/slot/7/v1/chat/completions", {
            "model": "gemini-flash", "messages": [], "stream": True}))
        await started.wait()
        self.assertEqual(self.slot.active_requests, 1)
        await worker.worker_retire_slot(7)
        rejected = await self.post("/slot/7/v1/chat/completions", {"model": "gemini-flash", "messages": []})
        self.assertEqual(rejected.status_code, 503)
        release.set()
        result = await job
        self.assertIn("still completed", result.text)
        self.assertEqual(self.slot.active_requests, 0)

    async def test_research_cancellation_never_marks_finished(self):
        started = asyncio.Event()
        async def research(**kwargs):
            started.set()
            await asyncio.Event().wait()
        self.client.deep_research = research
        tasks_before = asyncio.all_tasks()
        result = await worker.slot_deep_research(7, worker.DeepResearchRequest(prompt="synthetic"), worker.Response(), None)
        await started.wait()
        jobs = asyncio.all_tasks() - tasks_before
        self.assertEqual(len(jobs), 1)
        job = jobs.pop()
        job.cancel()
        await asyncio.gather(job, return_exceptions=True)
        record = await worker.get_research_status(result["task_id"], None)
        self.assertTrue(record["finished"])
        self.assertEqual(record["error"], "cancelled")


def fake_client(value):
    return SimpleNamespace(account_status=AccountStatus.AVAILABLE, _running=True, _model_registry={},
                           cookies={"__Secure-1PSID": value, "__Secure-1PSIDTS": value + "-ts"},
                           close=AsyncMock(), cookie_audit=None)


class LifecycleSafety(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "account7.env"
        self.path.write_text("SECURE_1PSID=old-disk\nSECURE_1PSIDTS=old-disk-ts\n")
        self.old = fake_client("old-memory")
        self.slot = Slot(7, "old-disk", "old-disk-ts", client=self.old, env_path=self.path)
        self.slot.state.update(initializing=False, auth_status="AVAILABLE")
        for item in [patch.object(worker, "slots", {7: self.slot}),
                     patch.object(worker, "_slot_tasks", {}),
                     patch.object(worker, "_management_locks", {}),
                     patch.object(worker, "_research_tasks", {}),
                     patch.object(worker, "ENVS_DIR", self.path.parent),
                     patch.object(worker, "get_runtime_models", AsyncMock(return_value=[])),
                     patch("gemini_webapi.cookie_audit.CookieAudit.create", return_value=None),
                     patch("socket.socket.connect", side_effect=AssertionError("No network"))]:
            item.start()
            self.addCleanup(item.stop)

    async def asyncTearDown(self):
        for task in worker._slot_tasks.values():
            task.cancel()
        await asyncio.gather(*worker._slot_tasks.values(), return_exceptions=True)

    async def fake_init(self, slot, proxy=None):
        slot.client = fake_client(slot.psid)
        slot.state.update(initializing=False, auth_status="AVAILABLE")

    def deployment(self):
        return worker.worker_deploy_slot(7, SimpleNamespace(json=AsyncMock(return_value={"psid": "new", "psidts": "new-ts"})))

    async def test_deployment_serializes_after_old_writeback(self):
        entered, release = threading.Event(), threading.Event()
        replace = slot_module.os.replace
        def pause(source, target):
            entered.set()
            if not release.wait(3):
                raise AssertionError("barrier timeout")
            replace(source, target)
        with patch.object(slot_module.os, "replace", side_effect=pause), patch.object(Slot, "init_client", lambda slot, proxy=None: self.fake_init(slot, proxy)):
            write = asyncio.create_task(asyncio.to_thread(self.slot.writeback_cookies))
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            deploy = asyncio.create_task(self.deployment())
            await asyncio.sleep(0)
            self.assertFalse(deploy.done())
            release.set()
            self.assertTrue(await write)
            self.assertEqual((await deploy).status_code, 202)
            await worker._slot_tasks[7]
        self.assertEqual(parse_env_file(self.path)["SECURE_1PSID"], "new")
        self.assertEqual(self.slot.psid, "new")

    async def test_cancelled_deploy_finishes_recovery_after_atomic_save(self):
        entered, release = threading.Event(), threading.Event()
        replace = slot_module.os.replace
        def pause(source, target):
            entered.set()
            if not release.wait(3):
                raise AssertionError("barrier timeout")
            replace(source, target)
        with patch.object(slot_module.os, "replace", side_effect=pause), patch.object(Slot, "init_client", lambda slot, proxy=None: self.fake_init(slot, proxy)):
            deploy = asyncio.create_task(self.deployment())
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            deploy.cancel()
            await asyncio.sleep(0)
            self.assertFalse(deploy.done())
            self.assertTrue(self.slot.state["initializing"])
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await deploy
            await worker._slot_tasks[7]
        self.assertEqual(parse_env_file(self.path)["SECURE_1PSID"], "new")
        self.assertEqual(self.slot.client.cookies["__Secure-1PSID"], "new")
        self.old.close.assert_awaited_once()

    async def test_reload_reads_credentials_under_writeback_lock(self):
        entered, release = threading.Event(), threading.Event()
        replace = slot_module.os.replace
        def pause(source, target):
            entered.set()
            if not release.wait(3):
                raise AssertionError("barrier timeout")
            replace(source, target)
        with patch.object(slot_module.os, "replace", side_effect=pause), patch.object(Slot, "init_client", lambda slot, proxy=None: self.fake_init(slot, proxy)):
            write = asyncio.create_task(asyncio.to_thread(self.slot.writeback_cookies))
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            await worker.worker_reload_slot(7)
            await asyncio.sleep(0)
            release.set()
            await write
            await worker._slot_tasks[7]
        self.assertEqual(parse_env_file(self.path)["SECURE_1PSID"], "old-memory")
        self.assertEqual(self.slot.client.cookies["__Secure-1PSID"], "old-memory")

    async def test_delete_blocks_admission_before_waiting_for_writer(self):
        entered, release = threading.Event(), threading.Event()
        def hold():
            with slot_module.credential_file_lock(self.path):
                entered.set()
                if not release.wait(3):
                    raise AssertionError("barrier timeout")
        holder = asyncio.create_task(asyncio.to_thread(hold))
        self.assertTrue(await asyncio.to_thread(entered.wait, 2))
        deletion = asyncio.create_task(worker.worker_delete_slot(7))
        await asyncio.sleep(0)
        try:
            self.assertTrue(self.slot.retiring)
            with self.assertRaises(worker.HTTPException) as error:
                await worker._get_client(self.slot)
            self.assertEqual(error.exception.status_code, 503)
            self.old.close.assert_not_awaited()
        finally:
            release.set()
        await holder
        self.assertTrue((await deletion)["ok"])
        self.old.close.assert_awaited_once()

    async def test_old_refresh_cannot_change_new_client_status(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def refresh():
            entered.set()
            await release.wait()
            self.old.account_status = AccountStatus.UNAUTHENTICATED
            raise RuntimeError("old request closed")
        self.old.refresh_models = refresh
        self.slot.writeback_cookies = Mock(return_value=False)
        task = asyncio.create_task(worker._refresh_slot_models(self.slot))
        await entered.wait()
        with patch.object(Slot, "init_client", lambda slot, proxy=None: self.fake_init(slot, proxy)):
            await worker.worker_reload_slot(7)
            await worker._slot_tasks[7]
        release.set()
        await task
        self.assertEqual(self.slot.state["auth_status"], "AVAILABLE")
        self.slot.writeback_cookies.assert_not_called()

    async def test_delete_finishes_before_same_number_deployment(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def close():
            entered.set()
            await release.wait()
        self.old.close = close
        deletion = asyncio.create_task(worker.worker_delete_slot(7))
        await entered.wait()
        with patch.object(Slot, "init_client", lambda slot, proxy=None: self.fake_init(slot, proxy)):
            deploy = asyncio.create_task(self.deployment())
            await asyncio.sleep(0)
            self.assertFalse(deploy.done())
            self.assertIn(7, worker.slots)
            release.set()
            self.assertTrue((await deletion)["ok"])
            self.assertEqual((await deploy).status_code, 202)
            await worker._slot_tasks[7]
        self.assertTrue(self.path.exists())
        self.assertEqual(parse_env_file(self.path)["SECURE_1PSID"], "new")

    async def test_closed_worker_client_cannot_implicitly_login(self):
        client = GeminiClient("synthetic", "synthetic-ts", allow_implicit_init=False)
        client.account_status = AccountStatus.AVAILABLE
        self.slot.client = client
        await client.close()
        self.slot.report_error(AuthError("synthetic failure", http_status=401))
        self.assertEqual(self.slot.health_response()["status"], "auth_failed")
        with self.assertRaises(worker.HTTPException) as error:
            await worker._get_client(self.slot)
        self.assertEqual(error.exception.status_code, 401)
        with patch.object(GeminiClient, "init", AsyncMock()) as init:
            with self.assertRaises(AuthError):
                await anext(client._generate("test"))
        init.assert_not_awaited()

    async def test_retire_keeps_client_and_stops_new_work_and_writeback(self):
        result = await worker.worker_retire_slot(7)
        self.assertTrue(result["health"]["retiring"])
        self.assertIs(self.slot.client, self.old)
        self.old.close.assert_not_awaited()
        self.assertFalse(await asyncio.to_thread(self.slot.writeback_cookies))
        with self.assertRaises(worker.HTTPException) as error:
            await worker._get_client(self.slot)
        self.assertEqual(error.exception.status_code, 503)
        self.old.refresh_models = AsyncMock()
        await worker._refresh_slot_models(self.slot)
        self.old.refresh_models.assert_not_awaited()

    async def test_external_file_change_is_not_overwritten(self):
        self.path.write_text("SECURE_1PSID=external\nSECURE_1PSIDTS=external-ts\n")
        self.assertFalse(await asyncio.to_thread(self.slot.writeback_cookies))
        self.assertEqual(parse_env_file(self.path)["SECURE_1PSID"], "external")


if __name__ == "__main__":
    unittest.main(verbosity=2)
