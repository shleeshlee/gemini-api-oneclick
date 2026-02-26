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

        psid_val = env.get("SECURE_1PSID", "").strip()
        accounts.append({
            "num": account_id,
            "has_cookie": bool(psid_val),
            "psid_preview": (psid_val[:20] + "...") if len(psid_val) > 20 else psid_val,
            "status": status,
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


def _restart_container(account_id: int) -> tuple[bool, str]:
    """Recreate a single container via docker compose (picks up new env)."""
    compose_file = ROOT_DIR / "docker-compose.accounts.yml"
    service_name = f"gemini-api-{account_id}"
    try:
        result = subprocess.run(
            ["docker", "compose", "-f", str(compose_file),
             "up", "-d", "--force-recreate", service_name],
            capture_output=True, text=True, timeout=30,
            cwd=str(ROOT_DIR),
        )
        if result.returncode == 0:
            return True, f"Container account {account_id} recreated"
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

        if path == "/api/deploy":
            # Require password on every deploy (matches VPS behavior)
            if AUTH_PASSWORD and body.get("password") != AUTH_PASSWORD:
                self._send_json({"error": "unauthorized"}, 401)
                return
            account_id = body.get("account")
            psid = body.get("psid", "").strip()
            psidts = body.get("psidts", "").strip()
            if not account_id or not psid or not psidts:
                self._send_json({"error": "missing account/psid/psidts"}, 400)
                return
            aid = int(account_id)
            if not _save_cookie(aid, psid, psidts):
                self._send_json({"error": f"account{aid}.env not found"}, 404)
                return
            ok, msg = _restart_container(aid)
            self._send_json({
                "success": ok,
                "message": f"account {aid}: env updated + container restarted" if ok else msg,
                "env_written": True,
            }, 200 if ok else 500)
            return

        if path == "/api/status":
            account_id = body.get("account")
            if not account_id:
                self._send_json({"error": "missing account"}, 400)
                return
            container = f"{CONTAINER_PREFIX}{account_id}"
            try:
                result = subprocess.run(
                    ["docker", "inspect", container, "--format",
                     "{{.State.Status}} {{.State.Health.Status}}"],
                    capture_output=True, text=True, timeout=10,
                )
                status = result.stdout.strip() if result.returncode == 0 else "not found"
            except Exception:
                status = "unknown"
            self._send_json({"container": container, "status": status})
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
