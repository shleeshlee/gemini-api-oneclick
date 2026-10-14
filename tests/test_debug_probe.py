"""Offline probe counterexamples; ASGI transport never starts a server."""

import copy
import json
import sys
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "app"), str(ROOT / "lib")]
import worker
from gemini_webapi.constants import MODEL_HEADER_KEY


class DebugProbeCounterexamples(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.frames = []
        self.failure = None
        self.output = SimpleNamespace(
            text_delta="Hello", thoughts_delta="", text="Hello", thoughts=None, images=[],
        )
        self.client = SimpleNamespace(
            _model_registry={"slot-model": SimpleNamespace(capacity=4)},
            generate_content_stream=Mock(side_effect=self.generate),
        )
        self.slot = SimpleNamespace(num=1, client=self.client, report_error=Mock())
        self.model = worker.Model.BASIC_FLASH
        self.trace = {"requested_model": "probe-model", "resolution": "exact"}
        self.trace.update(worker.describe_model(self.model))
        patches = {
            "slots": {1: self.slot},
            "API_KEY": "offline-test-key",
            "_get_client": AsyncMock(return_value=self.client),
            "resolve_model_for_chat": Mock(return_value=(self.model, self.trace)),
            "persist_worker_event": Mock(side_effect=lambda event, **kwargs: event),
            "logger": Mock(),
            "slot_log": Mock(),
        }
        for name, value in patches.items():
            patcher = patch.object(worker, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for patcher in (
            patch.object(worker.time, "time", return_value=1234567890),
            patch.object(worker.uuid, "uuid4", return_value=uuid.UUID(int=1)),
            patch("socket.create_connection", side_effect=AssertionError("Network forbidden")),
            patch("socket.socket.connect", side_effect=AssertionError("Network forbidden")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def generate(self, conversation, *, files, model, tracer):
        self.tracer = tracer
        tracer.on_request_start(
            prompt=conversation, model_name="probe", params={}, request_data_preview="",
            chat_metadata=[], use_pro=False, file_count=0,
        )
        for frame in self.frames:
            tracer.on_stream_frame(**frame)
        if self.failure:
            raise RuntimeError(self.failure)
        yield self.output
        tracer.on_request_end(
            status="ok", error=None, final_flags={}, chat_metadata_after=[], poll_iterations=0,
        )

    async def post(self, *, authorization="Bearer offline-test-key", **flags):
        headers = {"Authorization": authorization} if authorization else {}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=worker.app), base_url="http://offline.test",
        ) as client:
            return await client.post(
                "/slot/1/v1/chat/completions", headers=headers,
                json={"model": "probe-model", "messages": [{"role": "user", "content": "Hi"}], **flags},
            )

    async def test_counterexample_no_flag_unchanged(self):
        response = await self.post()
        self.assertEqual(response.status_code, 200)
        worker.resolve_model_for_chat.assert_called_once_with("probe-model", self.client)
        self.assertIs(self.client.generate_content_stream.call_args.kwargs["model"], self.model)
        self.assertIs(type(self.tracer), worker.RawCaptureTracer)
        expected = {
            "id": f"chatcmpl-{uuid.UUID(int=1)}", "object": "chat.completion",
            "created": 1234567890, "model": "probe-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hello"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
        }
        self.assertEqual(response.content, json.dumps(expected, separators=(",", ":")).encode())
        self.assertNotIn("oneclick_raw_capture", response.json())
        self.assertNotIn("oneclick_model_hints", response.json())
        worker.persist_worker_event.assert_called_once()
        self.assertEqual(worker.persist_worker_event.call_args.kwargs["payload"]["raw_capture"], self.tracer.get_snapshot())
        worker.slot_log.assert_called_once_with(1, "Chat: Human: Hi\n\nAssistant: ")

    async def test_counterexample_forced_token_used(self):
        for token, capacity, registry, expected in (
            ("0123456789abcdef", None, True, 4),
            ("ABCDEF0123456789", 2, True, 2),
            ("0123456789abcdef", None, False, 1),
            ("0123456789abcdef", 0, True, 0),
        ):
            with self.subTest(token=token, capacity=capacity, registry=registry):
                self.client._model_registry = {"m": SimpleNamespace(capacity=4)} if registry else {}
                worker.logger.reset_mock()
                worker.slot_log.reset_mock()
                response = await self.post(oneclick_force_model_token=token, oneclick_force_model_capacity=capacity)
                self.assertEqual(response.status_code, 200)
                model = self.client.generate_content_stream.call_args.kwargs["model"]
                self.assertEqual(model["model_header"], worker.build_model_header(token, expected))
                self.assertEqual(json.loads(model["model_header"][MODEL_HEADER_KEY])[4], token)
                self.assertEqual(response.headers["X-OneClick-Header-Token"], token)
                self.assertEqual(response.headers["X-OneClick-Model-Resolution"], "debug-forced")
                worker.resolve_model_for_chat.assert_not_called()
                worker.persist_worker_event.assert_not_called()
                worker.logger.info.assert_called_once_with("debug-forced token=%s", token)
                worker.slot_log.assert_called_once_with(1, f"debug-forced token={token}")
                self.assertNotIn("oneclick_raw_capture", response.json())

    async def test_counterexample_bad_token_400(self):
        for token in ("", "0" * 15, "0" * 17, "g" * 16, "0" * 16 + "\n", " 0123456789abcde", 123, [], {}):
            with self.subTest(token=token):
                response = await self.post(oneclick_force_model_token=token)
                self.assertEqual(response.status_code, 400)
        worker._get_client.assert_not_called()
        worker.resolve_model_for_chat.assert_not_called()
        self.client.generate_content_stream.assert_not_called()
        worker.persist_worker_event.assert_not_called()
        self.assertEqual(worker.logger.mock_calls, [])
        worker.slot_log.assert_not_called()

    async def test_counterexample_raw_truncated(self):
        self.frames = [{"part": ["x" * 24000], "part_json": ["y" * 24000], "flags": {}}]
        response = await self.post(oneclick_debug_raw=True)
        self.assertEqual(response.status_code, 200)
        capture = response.json()["oneclick_raw_capture"]
        self.assertTrue(capture["truncated"])
        self.assertTrue(capture["frames"][0]["truncated"])
        for field in ("part", "part_json"):
            self.assertEqual(capture["frames"][0][field], json.dumps(self.frames[0][field])[:20000])
        self.assertLessEqual(len(json.dumps(capture).encode()), 1_000_000)
        self.assertEqual(self.tracer.get_snapshot()["frames"], self.frames)
        worker.persist_worker_event.assert_not_called()
        self.assertEqual(worker.logger.mock_calls, [])
        worker.slot_log.assert_not_called()

    async def test_counterexample_hints_paths(self):
        nested = [None] * 5
        nested[4] = [[None] * 24]
        nested[4][0][23] = ["ordinary", "GeMiNi Flash"]
        self.frames = [
            {"part": ["Pro ignored outside part_json"], "part_json": nested, "flags": {}},
            {"part": [], "part_json": nested, "flags": {}},
            {"part": [], "part_json": {"model": ["Pro", "Lite", "Auto", "Argon", "Gemini", "Flash" + "x" * 76]}, "flags": {}},
        ]
        response = await self.post(oneclick_debug_raw=True)
        self.assertEqual(response.json()["oneclick_model_hints"], [
            {"value": "GeMiNi Flash", "path": [4, 0, 23, 1]},
            *({"value": value, "path": ["model", i]} for i, value in enumerate(("Pro", "Lite", "Auto", "Argon", "Gemini"))),
        ])

    async def test_counterexample_all_frames_hints_before_truncation(self):
        self.frames = [{"part": [], "part_json": ["plain"], "flags": {}} for _ in range(25)]
        self.frames[-1]["part_json"] = ["x" * 21000, "Argon late"]
        response = await self.post(oneclick_debug_raw=True)
        capture = response.json()["oneclick_raw_capture"]
        self.assertEqual(len(capture["frames"]), 25)
        self.assertEqual(response.json()["oneclick_model_hints"], [{"value": "Argon late", "path": [1]}])
        self.assertNotIn("Argon late", capture["frames"][-1]["part_json"])

    def test_counterexample_raw_total_bytes_and_metadata(self):
        for metadata in ([], ["界" * 400000]):
            with self.subTest(large_metadata=bool(metadata)):
                snapshot = {
                    "chat_metadata_after": metadata,
                    "frames": [{"part": ["界" * 21000], "part_json": ["\\" * 21000], "flags": {}} for _ in range(20)],
                }
                before = copy.deepcopy(snapshot)
                capture = worker._debug_raw_snapshot(snapshot)
                self.assertLessEqual(len(json.dumps(capture).encode()), 1_000_000)
                self.assertLessEqual(len(json.dumps(capture, ensure_ascii=False).encode()), 1_000_000)
                self.assertTrue(capture["truncated"])
                self.assertEqual(snapshot, before)

    def test_counterexample_hints_limit_and_dedup(self):
        snapshot = {"frames": [{"part_json": ["Pro " + str(i) for i in range(60)]}] * 2}
        hints = worker._debug_model_hints(snapshot)
        self.assertEqual(len(hints), 50)
        self.assertEqual(hints[-1], {"value": "Pro 49", "path": [49]})
        self.assertEqual(worker._debug_model_hints(None), [])
        self.assertIsNone(worker._debug_raw_snapshot(None))

    async def test_counterexample_auth_before_debug(self):
        for auth in (None, "Bearer wrong", "Basic wrong"):
            response = await self.post(authorization=auth, oneclick_force_model_token="bad", oneclick_debug_raw=True)
            self.assertEqual(response.status_code, 401)
        worker._get_client.assert_not_called()
        worker.resolve_model_for_chat.assert_not_called()
        self.client.generate_content_stream.assert_not_called()
        worker.persist_worker_event.assert_not_called()
        self.assertEqual(worker.logger.mock_calls, [])
        worker.slot_log.assert_not_called()

    async def test_counterexample_raw_ignored_for_stream(self):
        normal = await self.post(stream=True)
        debug = await self.post(stream=True, oneclick_debug_raw=True)
        self.assertEqual(normal.content, debug.content)
        self.assertEqual(normal.headers, debug.headers)
        self.assertIs(type(self.tracer), worker.RawCaptureTracer)
        self.assertNotIn(b"oneclick_raw_capture", debug.content)

    async def test_counterexample_debug_errors_never_persist(self):
        self.failure = "private raw payload"
        for stream, flags in (
            (False, {"oneclick_debug_raw": True}),
            (False, {"oneclick_force_model_token": "0123456789abcdef"}),
            (True, {"oneclick_force_model_token": "0123456789abcdef"}),
        ):
            with self.subTest(stream=stream, flags=flags):
                worker.logger.reset_mock()
                worker.slot_log.reset_mock()
                response = await self.post(stream=stream, **flags)
                self.assertEqual(response.status_code, 200 if stream else 500)
                worker.persist_worker_event.assert_not_called()
                self.assertNotIn(self.failure, str(worker.logger.mock_calls))
                self.assertNotIn(self.failure, str(worker.slot_log.mock_calls))

    async def test_counterexample_both_flags_and_stream_forcing(self):
        token = "0123456789abcdef"
        response = await self.post(oneclick_force_model_token=token, oneclick_debug_raw=True)
        self.assertIn("oneclick_raw_capture", response.json())
        self.assertEqual(response.headers["X-OneClick-Header-Token"], token)
        response = await self.post(stream=True, oneclick_force_model_token=token)
        self.assertEqual(response.status_code, 200)
        model = self.client.generate_content_stream.call_args.kwargs["model"]
        self.assertEqual(model["model_header"], worker.build_model_header(token, 4))
        self.assertIn(b"data: [DONE]", response.content)
        worker.resolve_model_for_chat.assert_not_called()
        worker.persist_worker_event.assert_not_called()


if __name__ == "__main__":
    unittest.main()
