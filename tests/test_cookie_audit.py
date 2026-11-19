"""Cookie audit contracts with synthetic credentials and no network access."""
import asyncio
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "app"), str(ROOT / "lib")]
from curl_cffi.requests import AsyncSession, Cookies
from gemini_webapi.cookie_audit import AuditStore, CookieAudit, AuditedSession, changes, cookie_operation
from slot import Slot


def pair(psid="synthetic-first", ts="synthetic-second"):
    return {"__Secure-1PSID": psid, "__Secure-1PSIDTS": ts}


class AuditContracts(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = AuditStore(self.root)
        self.addCleanup(self.store.handler.close)
        self.audit = CookieAudit(9, self.store)

    def events(self):
        return [json.loads(row) for row in (self.store.root / "events.jsonl").read_text().splitlines()]

    def slot(self, content="SECURE_1PSID=old-first\nSECURE_1PSIDTS=old-second\n"):
        env = self.root / "account9.env"
        env.write_text(content)
        return Slot(num=9, env_path=env, cookie_audit=self.audit,
                    client=SimpleNamespace(cookies=pair(), client=SimpleNamespace(cookies=pair())))

    def test_scoped_add_change_delete_and_no_values(self):
        jar = Cookies()
        jar.set("same", "private-one", domain=".google.com", path="/")
        jar.set("same", "private-two", domain="gemini.google.com", path="/")
        before = self.audit.snapshot(jar)
        jar.set("same", "private-three", domain=".google.com", path="/")
        jar.delete("same", domain="gemini.google.com", path="/")
        jar.set("added", "private-four", domain=".google.com", path="/")
        delta = changes(before, self.audit.snapshot(jar))
        self.assertEqual({r["change"] for r in delta}, {"added", "changed", "deleted"})
        serialized = json.dumps(delta)
        self.assertNotIn("private-", serialized)
        self.assertEqual(len(before["cookies"]), 2)

    def test_restart_comparison_and_external_change(self):
        self.audit.loaded(pair(), pair())
        self.assertIsNone(self.events()[-1]["matches_previous"])
        self.audit.writeback("written", memory=pair(ts="new-second"), file_values=pair(ts="new-second"))
        other_store = AuditStore(self.root)
        self.addCleanup(other_store.handler.close)
        restarted = CookieAudit(9, other_store)
        restarted.loaded(pair(ts="new-second"), pair(ts="new-second"))
        self.assertTrue(self.events()[-1]["matches_previous"])
        restarted.loaded(pair(ts="external-change"), pair(ts="external-change"))
        self.assertFalse(self.events()[-1]["matches_previous"])
        self.assertNotIn("new-second", (self.store.root / "slot-9.json").read_text())

    async def test_response_cookies_fingerprint_and_request_link(self):
        async def request(session, method, url, **kwargs):
            session.cookies.set("__Secure-1PSIDTS", "changed-secret", domain=".google.com", path="/")
            return SimpleNamespace(status_code=401, url="https://accounts.google.com/v3/signin/identifier?token=secret-query",
                                   cookies=Cookies({"__Secure-1PSIDTS": "changed-secret"}))
        session = AuditedSession(cookie_audit=self.audit)
        session.cookies.set("__Secure-1PSIDTS", "old-secret", domain=".google.com", path="/")
        try:
            with patch.object(AsyncSession, "request", request), cookie_operation("initialization"):
                response = await session.get("https://gemini.google.com/app?token=secret-query")
            self.assertEqual(response.status_code, 401)
            start, end = self.events()
            self.assertEqual(start["request_id"], end["request_id"])
            self.assertEqual(start["operation_id"], end["operation_id"])
            self.assertEqual(end["changes"][0]["change"], "changed")
            self.assertEqual(end["status"], 401)
            serialized = json.dumps(self.events())
            for secret in ["old-secret", "changed-secret", "secret-query"]:
                self.assertNotIn(secret, serialized)
        finally:
            await session.close()

    async def test_network_exception_and_cancellation_rethrow_without_message(self):
        session = AuditedSession(cookie_audit=self.audit)
        try:
            for exc in [RuntimeError("cookie=must-not-leak"), asyncio.CancelledError()]:
                with patch.object(AsyncSession, "request", AsyncMock(side_effect=exc)):
                    with self.assertRaises(type(exc)):
                        await session.get("https://gemini.google.com/app")
                self.assertEqual(self.events()[-1]["error_type"], type(exc).__name__)
            self.assertNotIn("must-not-leak", json.dumps(self.events()))
        finally:
            await session.close()

    async def test_audit_storage_failure_does_not_fail_request(self):
        session = AuditedSession(cookie_audit=self.audit)
        response = SimpleNamespace(status_code=200, url="https://gemini.google.com/app", cookies=Cookies())
        try:
            with patch.object(self.store, "emit", side_effect=OSError("secret")), \
                 patch.object(AsyncSession, "request", AsyncMock(return_value=response)):
                self.assertIs(await session.get("https://gemini.google.com/app"), response)
        finally:
            await session.close()

    async def test_overlap_marks_shared_jar_not_response_ownership(self):
        both_started = asyncio.Event()
        finish_first = asyncio.Event()
        started = 0
        session = AuditedSession(cookie_audit=self.audit)

        async def request(current, method, url, **kwargs):
            nonlocal started
            started += 1
            index = started
            if started == 2:
                both_started.set()
            await both_started.wait()
            if index == 1:
                await finish_first.wait()
                cookies = Cookies()
            else:
                current.cookies.set("extra", "synthetic-overlap", domain=".google.com")
                cookies = Cookies({"extra": "synthetic-overlap"})
                finish_first.set()
            return SimpleNamespace(status_code=200, url=url, cookies=cookies)

        try:
            with patch.object(AsyncSession, "request", request):
                await asyncio.gather(session.get("https://gemini.google.com/app"), session.get("https://gemini.google.com/app"))
            responses = [e for e in self.events() if e["event"] == "http_response"]
            self.assertTrue(all(e["overlapped"] for e in responses))
            self.assertTrue(all(e["change_scope"] == "shared_session_jar" for e in responses))
            self.assertEqual(sorted(len(e["response_cookies"]["cookies"]) for e in responses), [0, 1])
            self.assertFalse(session._audit_active)
        finally:
            await session.close()

    async def test_cookie_update_before_transport_error_is_recorded(self):
        session = AuditedSession(cookie_audit=self.audit)

        async def request(current, *args, **kwargs):
            current.cookies.set("redirect-cookie", "synthetic-partial", domain=".google.com")
            raise RuntimeError("transport failed after redirect")

        try:
            with patch.object(AsyncSession, "request", request):
                with self.assertRaises(RuntimeError):
                    await session.get("https://gemini.google.com/app")
            self.assertEqual(self.events()[-1]["event"], "http_error")
            self.assertEqual(self.events()[-1]["changes"][0]["name"], "redirect-cookie")
            self.assertFalse(session._audit_active)
        finally:
            await session.close()

    async def test_reinitialization_does_not_relabel_old_session(self):
        from gemini_webapi import GeminiClient
        slot = self.slot()
        slot.psid, slot.psidts = "synthetic-first", "synthetic-second"
        old_audit = slot.cookie_audit
        old_id = old_audit.client_id
        init = AsyncMock()
        with patch.object(GeminiClient, "init", init), patch.object(Slot, "probe_free_pro", AsyncMock()):
            await slot.init_client()
        old_audit.emit("late_old_response")
        self.assertIsNot(slot.cookie_audit, old_audit)
        self.assertNotEqual(slot.cookie_audit.client_id, old_id)
        self.assertEqual(self.events()[-1]["client_id"], old_id)
        init.assert_awaited_once_with(timeout=300, watchdog_timeout=180, auto_refresh=False)

    async def test_http_failure_is_not_reported_as_success(self):
        from worker import CookieWritebackMiddleware, slots
        slot = self.slot()

        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 500, "headers": []})

        with patch.dict(slots, {9: slot}, clear=True):
            await CookieWritebackMiddleware(app)({"path": "/slot/9/v1/chat/completions", "method": "POST"}, AsyncMock(), AsyncMock())
        self.assertFalse(self.events()[-2]["transport_success"])
        self.assertEqual(self.events()[-1]["reason"], "request_failed")

    def test_writeback_success_skip_and_atomic_failure(self):
        slot = self.slot()
        self.assertTrue(slot.writeback_cookies())
        event = self.events()[-1]
        self.assertEqual(event["reason"], "written")
        self.assertNotEqual(event["previous_file"]["version"], event["file"]["version"])
        self.assertEqual(event["file"], event["memory"])
        self.assertFalse(slot.writeback_cookies())
        self.assertEqual(self.events()[-1]["reason"], "throttled")
        slot._last_cookie_write = None
        self.assertFalse(slot.writeback_cookies())
        self.assertEqual(self.events()[-1]["reason"], "unchanged")
        slot.client.client.cookies = pair(ts="another-secret")
        old = slot.env_path.read_text()
        with patch("slot.os.replace", side_effect=OSError("secret-error")):
            self.assertFalse(slot.writeback_cookies())
        self.assertEqual(slot.env_path.read_text(), old)
        self.assertEqual(self.events()[-1]["reason"], "failed")
        self.assertNotIn("another-secret", json.dumps(self.events()))
        self.assertNotIn("secret-error", json.dumps(self.events()))

    def test_writeback_other_skip_reasons(self):
        slot = self.slot()
        slot._cookie_lock.acquire()
        try:
            self.assertFalse(slot.writeback_cookies())
        finally:
            slot._cookie_lock.release()
        self.assertEqual(self.events()[-1]["reason"], "lock_busy")
        slot.client = None
        self.assertFalse(slot.writeback_cookies())
        self.assertEqual(self.events()[-1]["reason"], "no_client")
        slot = self.slot("SECURE_1PSID=only-one\n")
        self.assertFalse(slot.writeback_cookies())
        self.assertEqual(self.events()[-1]["reason"], "incomplete_env")
        slot.client.cookies = {}
        slot.client.client.cookies = {}
        self.assertFalse(slot.writeback_cookies())
        self.assertEqual(self.events()[-1]["reason"], "invalid_cookie_pair")

    def test_writeback_survives_fingerprint_failure(self):
        slot = self.slot()
        with patch.object(self.store, "snapshot", side_effect=OSError("audit failure")):
            self.assertTrue(slot.writeback_cookies())
        self.assertIn("synthetic-second", slot.env_path.read_text())

    def test_writeback_keeps_old_identity_during_reload(self):
        import slot as slot_module
        slot = self.slot()
        original_parse = slot_module.parse_env_file
        old_id = self.audit.client_id

        def reload_during_read(path):
            data = original_parse(path)
            slot.cookie_audit = CookieAudit(9, self.store)
            slot.client = SimpleNamespace(cookies=pair(ts="new-client-secret"))
            return data

        with patch.object(slot_module, "parse_env_file", reload_during_read):
            self.assertTrue(slot.writeback_cookies())
        event = self.events()[-1]
        self.assertEqual(event["client_id"], old_id)
        self.assertTrue(event["client_changed_during_write"])

    def test_replaced_fingerprint_key_makes_comparison_unknown(self):
        self.audit.loaded(pair(), pair())
        self.store.key = b"x" * 32
        self.store.key_id = hashlib.sha256(self.store.key).hexdigest()[:16]
        self.audit.loaded(pair(), pair())
        self.assertIsNone(self.events()[-1]["matches_previous"])
        self.assertFalse(self.events()[-1]["previous_comparable"])

    def test_unavailable_audit_directory_is_fail_open(self):
        with patch.object(Path, "resolve", side_effect=PermissionError("private-path")):
            self.assertIsNone(CookieAudit.create(9, self.root))

    def test_log_rotation_and_permissions(self):
        self.store.handler.maxBytes = 1000
        self.store.handler.backupCount = 2
        for n in range(40):
            self.audit.emit("synthetic", number=n)
        self.assertEqual(len(list(self.store.root.glob("events.jsonl*"))), 3)
        for p in self.store.root.iterdir():
            self.assertEqual(p.stat().st_mode & 0o077, 0)

    async def test_successful_stream_operation_and_writeback_share_id(self):
        from worker import CookieWritebackMiddleware, slots
        slot = self.slot()

        async def app(scope, receive, send):
            self.audit.emit("http_response", status=200)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"data: test\n\n", "more_body": True})
            await send({"type": "http.response.body", "body": b"", "more_body": False})

        with patch.dict(slots, {9: slot}, clear=True):
            await CookieWritebackMiddleware(app)({"path": "/slot/9/v1/chat/completions", "method": "POST"}, AsyncMock(), AsyncMock())
        events = self.events()
        self.assertEqual({e["event"] for e in events}, {"http_response", "api_complete", "cookie_writeback"})
        self.assertEqual(len({e["operation_id"] for e in events}), 1)


if __name__ == "__main__":
    unittest.main()
