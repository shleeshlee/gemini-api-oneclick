"""Bounded, fingerprint-only evidence for worker cookie lifecycles.

Observation only: never rotates, restores, or modifies session cookies.
"""
from __future__ import annotations

import contextvars
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlsplit

from curl_cffi.requests import AsyncSession

BOOT_ID = uuid.uuid4().hex
_operation = contextvars.ContextVar("cookie_audit_operation", default=None)
_stores = {}
_stores_lock = threading.Lock()
_logger = logging.getLogger(__name__)


class PrivateRotatingHandler(RotatingFileHandler):
    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        os.fchmod(fd, 0o600)
        return os.fdopen(fd, self.mode, encoding=self.encoding, errors=self.errors)


@contextmanager
def cookie_operation(kind):
    token = _operation.set({"operation_id": uuid.uuid4().hex, "operation": kind})
    try:
        yield
    finally:
        _operation.reset(token)


def safe_route(url):
    """Retain only known Google routes, never query strings or user URLs."""
    try:
        parsed = urlsplit(str(url))
        host = parsed.hostname or ""
        if host != "google.com" and not host.endswith(".google.com"):
            return "<other>"
        path = parsed.path
        if not re.fullmatch(
            r"/|/(?:u/\d+/)?app|/_/BardChatUi/data/(?:batchexecute|assistant\.lamda\.BardFrontendService/StreamGenerate)"
            r"|/RotateCookies|/v3/signin/identifier|/restrictions/\d+", path
        ):
            path = "/<other>"
        return host + path
    except Exception:
        return "<unknown>"


def _cookie_items(cookies):
    jar = getattr(cookies, "jar", None)
    if jar is not None:
        return [(c.name, c.domain or "", c.path or "/", c.value or "") for c in jar]
    if isinstance(cookies, dict):
        return [(k, ".google.com", "/", v) for k, v in cookies.items() if isinstance(v, str)]
    return []


class AuditStore:
    def __init__(self, root):
        self.root = Path(root) / "cookie-audit"
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lock = threading.RLock()
        key_path = self.root / "fingerprint.key"
        try:
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as output:
                output.write(secrets.token_bytes(32))
                output.flush()
                os.fsync(output.fileno())
        self.key = key_path.read_bytes()
        if len(self.key) != 32:
            raise ValueError("Invalid audit fingerprint key")
        self.key_id = hashlib.sha256(self.key).hexdigest()[:16]
        os.chmod(key_path, 0o600)
        log_path = self.root / "events.jsonl"
        fd = os.open(log_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        os.close(fd)
        self.handler = PrivateRotatingHandler(log_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
        self.handler.setFormatter(logging.Formatter("%(message)s"))
        self.sequence = 0

    def fingerprint(self, value):
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        return hmac.new(self.key, encoded, hashlib.sha256).hexdigest()[:32]

    def snapshot(self, cookies):
        entries = []
        for name, domain, path, value in _cookie_items(cookies):
            entries.append({"name": name, "scope": self.fingerprint([domain, path]),
                            "value_fp": self.fingerprint([name, domain, path, value])})
        entries.sort(key=lambda row: (row["name"], row["scope"]))
        return {"version": self.fingerprint(entries), "cookies": entries}

    def emit(self, num, event, **fields):
        with self.lock:
            self.sequence += 1
            record = {"schema": 1, "time": time.time(), "boot_id": BOOT_ID,
                      "fingerprint_key_id": self.key_id,
                      "sequence": self.sequence, "slot": num, "event": event,
                      **(_operation.get() or {}), **fields}
            message = json.dumps(record, ensure_ascii=True, separators=(",", ":"))
            # Handler owns rotation and flushing; no cookie values reach logging.
            self.handler.handle(logging.LogRecord(__name__, logging.INFO, "", 0, message, (), None))

    def read_checkpoint(self, num):
        path = self.root / f"slot-{num}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text())

    def checkpoint(self, num, snapshot, reason):
        payload = {"boot_id": BOOT_ID, "fingerprint_key_id": self.key_id, "time": time.time(), "reason": reason, "file": snapshot}
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=self.root, delete=False) as output:
                temporary = output.name
                os.chmod(temporary, 0o600)
                json.dump(payload, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.root / f"slot-{num}.json")
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)


def changes(before, after):
    old = {(c["name"], c["scope"]): c["value_fp"] for c in before["cookies"]}
    new = {(c["name"], c["scope"]): c["value_fp"] for c in after["cookies"]}
    return [{"name": k[0], "scope": k[1], "change": "added" if k not in old else "deleted" if k not in new else "changed",
             "before_fp": old.get(k), "after_fp": new.get(k)}
            for k in sorted(old.keys() | new.keys()) if old.get(k) != new.get(k)]


class CookieAudit:
    def __init__(self, num, store):
        self.num, self.store = num, store
        self.client_id = uuid.uuid4().hex

    @classmethod
    def create(cls, num, root=None):
        try:
            root = str(Path(root or os.getenv("ONECLICK_STATE_DIR", "/app/state")).resolve())
            with _stores_lock:
                if root not in _stores:
                    _stores[root] = AuditStore(root)
                return cls(num, _stores[root])
        except Exception as exc:
            _logger.warning("Cookie audit unavailable for slot %d (%s)", num, type(exc).__name__)
            return None

    def snapshot(self, cookies):
        return self.store.snapshot(cookies)

    def emit(self, event, **fields):
        try:
            self.store.emit(self.num, event, client_id=self.client_id, **fields)
        except Exception as exc:
            _logger.warning("Cookie audit event failed for slot %d (%s)", self.num, type(exc).__name__)

    def loaded(self, values, file_values):
        try:
            supplied = self.snapshot(values)
            disk = self.snapshot(file_values) if file_values is not None else None
            previous = self.store.read_checkpoint(self.num)
            expected = previous["file"] if previous else None
            comparable = previous is not None and previous.get("fingerprint_key_id") == self.store.key_id
            self.emit("credentials_loaded", supplied=supplied, file=disk,
                      matches_file=supplied == disk if disk is not None else None,
                      previous_file=expected,
                      previous_comparable=comparable,
                      matches_previous=disk == expected if disk is not None and comparable else None)
            if disk is not None:
                self.store.checkpoint(self.num, disk, "loaded")
        except Exception as exc:
            self.emit("audit_error", stage="loaded", error_type=type(exc).__name__)

    def writeback(self, reason, *, memory=None, file_values=None, previous_file_values=None, **fields):
        try:
            disk = self.snapshot(file_values) if file_values is not None else None
            self.emit("cookie_writeback", reason=reason,
                      memory=self.snapshot(memory) if memory is not None else None, file=disk,
                      previous_file=self.snapshot(previous_file_values) if previous_file_values is not None else None, **fields)
            if reason in {"written", "unchanged"} and disk is not None:
                self.store.checkpoint(self.num, disk, reason)
        except Exception as exc:
            self.emit("audit_error", stage="writeback", error_type=type(exc).__name__)


class AuditedSession(AsyncSession):
    def __init__(self, *args, cookie_audit, **kwargs):
        super().__init__(*args, **kwargs)
        self.cookie_audit = cookie_audit
        self._audit_active = {}

    async def request(self, method, url, *args, **kwargs):
        audit = self.cookie_audit
        request_id = uuid.uuid4().hex
        for active in self._audit_active.values():
            active["overlapped"] = True
        tracking = {"overlapped": bool(self._audit_active)}
        self._audit_active[request_id] = tracking
        started = time.monotonic()
        before = None
        try:
            before = audit.snapshot(self.cookies)
            audit.emit("http_start", request_id=request_id, method=method,
                       route=safe_route(url), jar=before)
        except Exception as exc:
            audit.emit("audit_error", stage="http_start", error_type=type(exc).__name__)
        try:
            response = await super().request(method, url, *args, **kwargs)
        except BaseException as exc:
            try:
                after = audit.snapshot(self.cookies)
                response = getattr(exc, "response", None)
                audit.emit("http_error", request_id=request_id, error_type=type(exc).__name__,
                           elapsed_ms=round((time.monotonic() - started) * 1000),
                           status=getattr(response, "status_code", None), jar=after,
                           changes=changes(before, after) if before is not None else None,
                           change_scope="shared_session_jar", overlapped=tracking["overlapped"],
                           response_cookies=audit.snapshot(getattr(response, "cookies", None)))
            except Exception as audit_exc:
                audit.emit("audit_error", stage="http_error", error_type=type(audit_exc).__name__)
            finally:
                self._audit_active.pop(request_id, None)
            raise
        try:
            after = audit.snapshot(self.cookies)
            audit.emit("http_response", request_id=request_id, status=response.status_code,
                       route=safe_route(response.url), elapsed_ms=round((time.monotonic() - started) * 1000),
                       before_version=before["version"] if before is not None else None,
                       jar=after, changes=changes(before, after) if before is not None else None,
                       change_scope="shared_session_jar", overlapped=tracking["overlapped"],
                       response_cookies=audit.snapshot(response.cookies))
        except Exception as exc:
            audit.emit("audit_error", stage="http_response", error_type=type(exc).__name__)
        finally:
            self._audit_active.pop(request_id, None)
        return response
