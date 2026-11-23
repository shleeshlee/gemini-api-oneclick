"""OSID must survive panel deployment, restart, login and cookie writeback."""
import asyncio
import importlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests.test_login_recovery import gateway, worker, Slot, GeminiClient, token_module, response
from slot import parse_env_file
from curl_cffi.requests import Cookies
import httpx


class OsidTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "account9.env"
        self.path.write_text("API_KEY=synthetic-key\nSECURE_1PSID=synthetic\nSECURE_1PSIDTS=synthetic-ts\nSLOT_PROXY_ID=keep\n")
        for p in [patch.object(worker, "slots", {}), patch.object(worker, "_slot_tasks", {}),
                  patch.object(worker, "ENVS_DIR", self.root),
                  patch("gemini_webapi.cookie_audit.CookieAudit.create", return_value=None)]:
            p.start()
            self.addCleanup(p.stop)

    async def asyncTearDown(self):
        for task in worker._slot_tasks.values():
            task.cancel()
        await asyncio.gather(*worker._slot_tasks.values(), return_exceptions=True)

    async def test_osid_only_preserves_pair_and_proxy_and_survives_restart(self):
        slot = Slot.from_env_file(9, self.path)
        await slot.save_credentials(self.path, "", "", "synthetic-osid")
        saved = parse_env_file(self.path)
        self.assertEqual(saved["SECURE_1PSID"], "synthetic")
        self.assertEqual(saved["SLOT_PROXY_ID"], "keep")
        self.assertEqual(saved["API_KEY"], "synthetic-key")
        self.assertEqual(Slot.from_env_file(9, self.path).osid, "synthetic-osid")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    async def test_new_pair_without_osid_cannot_reuse_old_session_osid(self):
        slot = Slot.from_env_file(9, self.path)
        await slot.save_credentials(self.path, "", "", "old-osid")
        await slot.save_credentials(self.path, "new-psid", "new-ts")
        self.assertEqual(slot.osid, "")
        self.assertEqual(parse_env_file(self.path)["OSID"], "")

    async def test_direct_reload_clears_old_session_osid(self):
        slot = Slot(9, psid="old", psidts="old-ts", osid="old-osid")
        with patch.object(Slot, "init_client", AsyncMock()):
            await slot.reload("new", "new-ts")
        self.assertEqual(slot.osid, "")
        with patch.object(Slot, "init_client", AsyncMock()):
            await slot.reload("next", "next-ts", osid="next-osid")
        self.assertEqual(slot.osid, "next-osid")

    async def test_osid_only_requires_existing_pair(self):
        slot = Slot(10)
        with self.assertRaises(ValueError):
            await slot.save_credentials(self.root / "account10.env", "", "", "synthetic-osid")
        self.assertFalse((self.root / "account10.env").exists())

    async def test_worker_deploy_osid_only_reloads_single_slot(self):
        other = Slot(8)
        worker.slots[8] = other
        async def init(slot, proxy=None):
            slot.state["initializing"] = False
        with patch.object(Slot, "init_client", init), patch.object(worker, "get_runtime_models", AsyncMock(return_value=[])):
            result = await worker.worker_deploy_slot(9, SimpleNamespace(json=AsyncMock(return_value={"osid":"synthetic-osid"})))
            self.assertEqual(result.status_code, 202)
            await worker._slot_tasks[9]
        self.assertEqual(worker.slots[9].osid, "synthetic-osid")
        self.assertIs(worker.slots[8], other)
        self.assertNotIn("synthetic-osid", result.body.decode())

    async def test_gateway_passes_osid_to_worker(self):
        context = AsyncMock()
        context.__aenter__.return_value.post.return_value = httpx.Response(202, json={"ok":True})
        with patch.object(gateway, "WORKER_MODE", True), patch.object(gateway, "ENVS_DIR", self.root), \
             patch.dict(gateway.containers, {9:gateway.Container(9, 8009)}, clear=True), \
             patch.object(gateway.httpx, "AsyncClient", return_value=context), \
             patch.object(gateway, "check_health", AsyncMock()), patch.object(gateway, "add_log"):
            result = await gateway.deploy_cookie(9, SimpleNamespace(json=AsyncMock(return_value={"osid":"synthetic-osid"})))
        self.assertTrue(result["ok"])
        self.assertEqual(context.__aenter__.return_value.post.call_args.kwargs["json"],
                         {"psid":"", "psidts":"", "osid":"synthetic-osid"})

    async def test_bad_osid_rejected_before_mutation(self):
        original = self.path.read_bytes()
        for value in ["bad\nAPI_KEY=x", "bad\rvalue", "bad; SID=x", "bad\x00", 123, None]:
            for handler in (worker.worker_deploy_slot, gateway.deploy_cookie):
                with self.subTest(value=value, handler=handler.__name__), self.assertRaises(worker.HTTPException) as exc:
                    await handler(9, SimpleNamespace(json=AsyncMock(return_value={"osid":value})))
                self.assertEqual(exc.exception.status_code, 400)
        self.assertEqual(self.path.read_bytes(), original)

    async def test_client_cookie_domain_and_base_cache_candidates(self):
        client = GeminiClient("synthetic", "synthetic-ts", osid="synthetic-osid")
        self.assertNotIn("osid", client.kwargs)
        osid = next(c for c in client.cookies.jar if c.name == "OSID")
        self.assertEqual((osid.domain, osid.path), ("gemini.google.com", "/"))
        seen = []
        async def send(jar, **kwargs):
            seen.append(jar)
            return response(), jar
        (self.root / ".cached_1psidts_synthetic.txt").write_text("cached-ts")
        session = SimpleNamespace(get=AsyncMock(return_value=response(url="https://www.google.com/")))
        with patch.object(token_module, "send_request", side_effect=send), \
             patch.object(token_module, "_get_cache_dir", return_value=self.root):
            await token_module.get_access_token(client.cookies, session=session)
        self.assertEqual(len(seen), 2)
        for jar in seen:
            self.assertEqual(jar.get("OSID", domain="gemini.google.com"), "synthetic-osid")

    async def test_slot_passes_osid_to_client(self):
        slot = Slot(9, psid="synthetic", psidts="synthetic-ts", osid="synthetic-osid")
        fake = SimpleNamespace(init=AsyncMock(side_effect=RuntimeError("stop before network")))
        with patch("gemini_webapi.GeminiClient", return_value=fake) as factory:
            with self.assertRaises(RuntimeError):
                await slot.init_client()
        self.assertEqual(factory.call_args.kwargs["osid"], "synthetic-osid")

    def test_cookie_headers_keep_osid_on_gemini_only(self):
        import main
        jar = GeminiClient("synthetic", "synthetic-ts", osid="synthetic-osid").cookies
        scoped = httpx.Cookies(jar.jar)
        with httpx.Client(cookies=scoped) as client:
            self.assertIn("OSID=synthetic-osid", client.build_request("GET", "https://gemini.google.com/app").headers["cookie"])
            for host in ["accounts.google.com", "lh3.googleusercontent.com", "example.invalid"]:
                self.assertNotIn("OSID=", client.build_request("GET", "https://"+host+"/").headers.get("cookie", ""))
        for module in [worker, main]:
            for source in [jar, {"OSID":"synthetic-osid", "SID":"synthetic-sid"}]:
                with httpx.Client(cookies=module._flatten_cookies(source)) as client:
                    self.assertNotIn("OSID=", client.build_request("GET", "https://lh3.googleusercontent.com/").headers.get("cookie", ""))

    async def test_library_media_downloads_never_expand_osid_domain(self):
        from gemini_webapi.types.image import Image
        from gemini_webapi.types.video import GeneratedVideo
        jar = GeminiClient("synthetic", "synthetic-ts", osid="synthetic-osid").cookies
        for source in [jar, {"OSID":"synthetic-osid", "SID":"synthetic-sid"}]:
            for kind in ["image", "video", "thumbnail"]:
                session = SimpleNamespace(headers={}, cookies=Cookies())
                async def intercept(*args, **kwargs):
                    self.assertNotIn("OSID", session.cookies)
                    raise RuntimeError("offline download stopped")
                session.get = AsyncMock(side_effect=intercept)
                context = AsyncMock()
                context.__aenter__.return_value = session
                module = importlib.import_module("gemini_webapi.types."+("image" if kind == "image" else "video"))
                with self.subTest(kind=kind), patch.object(module, "AsyncSession", return_value=context):
                    video = GeneratedVideo(url="https://contribution.usercontent.google.com/video", thumbnail_url="https://lh3.googleusercontent.com/thumb", cookies=source)
                    try:
                        if kind == "image":
                            await Image(url="https://lh3.googleusercontent.com/image").save(path=str(self.root), filename="test.png", cookies=source)
                        elif kind == "video":
                            await video.save(path=str(self.root), filename="test.mp4")
                        else:
                            await video.save_thumbnail(path=str(self.root), filename="test.png")
                    except RuntimeError:
                        pass
                    session.get.assert_awaited_once()

    async def test_writeback_preserves_scoped_osid_and_rejects_stale_writer(self):
        slot = Slot.from_env_file(9, self.path)
        await slot.save_credentials(self.path, "", "", "old-osid")
        jar = Cookies({"__Secure-1PSID":"synthetic", "__Secure-1PSIDTS":"new-ts"})
        jar.set("OSID", "new-osid", domain="gemini.google.com")
        jar.set("OSID", "wrong-domain", domain="accounts.google.com")
        slot.client = SimpleNamespace(cookies=jar)
        slot.state["initializing"] = False
        self.assertTrue(slot.writeback_cookies())
        self.assertEqual(parse_env_file(self.path)["OSID"], "new-osid")
        self.assertEqual(slot.osid, "new-osid")
        self.path.write_text(self.path.read_text().replace("OSID=new-osid", "OSID=user-updated"))
        slot._last_cookie_write = None
        jar.set("__Secure-1PSIDTS", "stale-ts")
        self.assertFalse(slot.writeback_cookies())
        self.assertEqual(parse_env_file(self.path)["OSID"], "user-updated")


if __name__ == "__main__":
    unittest.main()
