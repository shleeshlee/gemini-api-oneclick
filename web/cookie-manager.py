#!/usr/bin/env python3
"""Cookie Manager - Web UI backend for managing Gemini API account cookies.

Runs on the host machine (not in Docker) so it can execute docker commands
to restart individual account containers.
"""

import json
import os
import re
import subprocess
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# ── Config ──────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent

def _read_env(path: Path) -> dict:
    result = {}
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        result[k.strip()] = v.strip()
    return result

_dotenv = _read_env(ROOT_DIR / ".env")

ENVS_DIR = Path(os.environ.get("ENVS_DIR", str(ROOT_DIR / "envs")))
CONTAINER_PREFIX = os.environ.get("CONTAINER_PREFIX", _dotenv.get("CONTAINER_PREFIX", "gemini_api_account_"))
AUTH_PASSWORD = os.environ.get("COOKIE_MANAGER_PASSWORD", _dotenv.get("COOKIE_MANAGER_PASSWORD", ""))
LISTEN_PORT = int(os.environ.get("COOKIE_MANAGER_PORT", _dotenv.get("COOKIE_MANAGER_PORT", "9880")))
API_KEY = os.environ.get("API_KEY", _dotenv.get("API_KEY", ""))

INDEX_HTML = SCRIPT_DIR / "index.html"


# ── Helpers ─────────────────────────────────────────────────────────────

def _list_accounts() -> list[dict]:
    """Scan envs/ for account*.env files, return sorted list of account info."""
    accounts = []
    pattern = re.compile(r"account(\d+)\.env$")
    for p in ENVS_DIR.glob("account*.env"):
        m = pattern.search(p.name)
        if not m:
            continue
        account_id = int(m.group(1))
        env = _read_env(p)
        container = f"{CONTAINER_PREFIX}{account_id}"

        # Check container status
        status = "unknown"
        try:
            result = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.Status}}", container],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                status = result.stdout.strip()
        except Exception:
            pass

        accounts.append({
            "id": account_id,
            "container": container,
            "status": status,
            "has_cookie": bool(env.get("SECURE_1PSID", "").strip()),
            "cookie_preview": _mask(env.get("SECURE_1PSID", "")),
            "cookie_ts_preview": _mask(env.get("SECURE_1PSIDTS", "")),
        })
    accounts.sort(key=lambda x: x["id"])
    return accounts


def _mask(value: str) -> str:
    """Show first 8 and last 4 chars, mask the rest."""
    v = value.strip()
    if len(v) <= 16:
        return v[:4] + "***" if v else ""
    return v[:8] + "..." + v[-4:]


def _save_cookie(account_id: int, psid: str, psidts: str) -> bool:
    """Write cookie values to the account's env file."""
    env_file = ENVS_DIR / f"account{account_id}.env"
    if not env_file.exists():
        return False

    lines = env_file.read_text(encoding="utf-8").splitlines()
    new_lines = []
    found_psid = False
    found_psidts = False
    for line in lines:
        if line.strip().startswith("SECURE_1PSID="):
            new_lines.append(f"SECURE_1PSID={psid}")
            found_psid = True
        elif line.strip().startswith("SECURE_1PSIDTS="):
            new_lines.append(f"SECURE_1PSIDTS={psidts}")
            found_psidts = True
        else:
            new_lines.append(line)
    if not found_psid:
        new_lines.append(f"SECURE_1PSID={psid}")
    if not found_psidts:
        new_lines.append(f"SECURE_1PSIDTS={psidts}")

    env_file.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    return True


def _restart_container(container: str) -> tuple[bool, str]:
    """Restart a single docker container."""
    try:
        result = subprocess.run(
            ["docker", "restart", container],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            return True, f"Container {container} restarted"
        return False, result.stderr.strip()
    except subprocess.TimeoutExpired:
        return False, "Restart timed out"
    except Exception as e:
        return False, str(e)


# ── HTTP Handler ────────────────────────────────────────────────────────

class CookieManagerHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[cookie-manager] {args[0]} {args[1]} {args[2]}")

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str, status=200):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/" or path == "/index.html":
            if INDEX_HTML.exists():
                self._send_html(INDEX_HTML.read_text(encoding="utf-8"))
            else:
                self._send_html("<h1>index.html not found</h1>", 404)
            return

        if path == "/api/accounts":
            self._send_json({"accounts": _list_accounts()})
            return

        self._send_json({"error": "Not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        try:
            body = json.loads(self._read_body() or b"{}")
        except json.JSONDecodeError:
            self._send_json({"error": "Invalid JSON"}, 400)
            return

        if path == "/api/login":
            if not AUTH_PASSWORD:
                self._send_json({"success": True, "message": "No password configured"})
                return
            if body.get("password") == AUTH_PASSWORD:
                self._send_json({"success": True})
            else:
                self._send_json({"success": False, "error": "Wrong password"}, 401)
            return

        if path == "/api/save-cookie":
            account_id = body.get("id")
            psid = body.get("psid", "").strip()
            psidts = body.get("psidts", "").strip()
            if not account_id or not psid or not psidts:
                self._send_json({"error": "Missing id, psid, or psidts"}, 400)
                return
            if _save_cookie(int(account_id), psid, psidts):
                self._send_json({"success": True})
            else:
                self._send_json({"error": f"account{account_id}.env not found"}, 404)
            return

        if path == "/api/restart":
            container = body.get("container", "")
            if not container or not container.startswith(CONTAINER_PREFIX):
                self._send_json({"error": "Invalid container name"}, 400)
                return
            ok, msg = _restart_container(container)
            self._send_json({"success": ok, "message": msg}, 200 if ok else 500)
            return

        if path == "/api/deploy":
            account_id = body.get("id")
            psid = body.get("psid", "").strip()
            psidts = body.get("psidts", "").strip()
            if not account_id or not psid or not psidts:
                self._send_json({"error": "Missing id, psid, or psidts"}, 400)
                return
            if not _save_cookie(int(account_id), psid, psidts):
                self._send_json({"error": f"account{account_id}.env not found"}, 404)
                return
            container = f"{CONTAINER_PREFIX}{account_id}"
            ok, msg = _restart_container(container)
            self._send_json({"success": ok, "message": f"Cookie saved. {msg}"}, 200 if ok else 500)
            return

        self._send_json({"error": "Not found"}, 404)


# ── Main ────────────────────────────────────────────────────────────────

def main():
    if not AUTH_PASSWORD:
        print("[cookie-manager] WARNING: No COOKIE_MANAGER_PASSWORD set, login is disabled!")

    server = HTTPServer(("0.0.0.0", LISTEN_PORT), CookieManagerHandler)
    print(f"[cookie-manager] Listening on 0.0.0.0:{LISTEN_PORT}")
    print(f"[cookie-manager] Serving UI from {INDEX_HTML}")
    print(f"[cookie-manager] Managing envs in {ENVS_DIR}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[cookie-manager] Shutting down")
        server.server_close()


if __name__ == "__main__":
    main()
