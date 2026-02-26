#!/usr/bin/env python3
"""Cookie Manager — API + static page server.

Based on VPS cookie-deployer.py, with hardcoded values replaced by config.
Runs on host machine (not Docker) so it can execute docker commands.
"""

import json
import os
import re
import subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

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
COMPOSE_FILE = ROOT_DIR / "docker-compose.accounts.yml"
CONTAINER_PREFIX = os.environ.get("CONTAINER_PREFIX", _dotenv.get("CONTAINER_PREFIX", "gemini_api_account_"))
API_KEY = os.environ.get("API_KEY", _dotenv.get("API_KEY", ""))
AUTH_PASSWORD = os.environ.get("COOKIE_MANAGER_PASSWORD", _dotenv.get("COOKIE_MANAGER_PASSWORD", ""))
LISTEN_PORT = int(os.environ.get("COOKIE_MANAGER_PORT", _dotenv.get("COOKIE_MANAGER_PORT", "9880")))
INDEX_HTML = SCRIPT_DIR / "index.html"


# ── HTTP Handler (mirrors VPS cookie-deployer) ────────────────────────

class DeployHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self._serve_html()
        elif self.path == "/api/accounts":
            self._handle_list_accounts()
        elif self.path == "/api/health":
            self._respond(200, {"status": "ok"})
        else:
            self._respond(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/api/login":
            self._handle_login()
        elif self.path == "/api/deploy":
            self._handle_deploy()
        elif self.path == "/api/status":
            self._handle_status()
        else:
            self._respond(404, {"error": "not found"})

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _serve_html(self):
        try:
            content = INDEX_HTML.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(f"Error: {e}".encode())

    def _handle_login(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        except Exception:
            self._respond(400, {"error": "invalid json"})
            return

        if not AUTH_PASSWORD:
            self._respond(200, {"ok": True})
            return

        password = body.get("password", "")
        if password != AUTH_PASSWORD:
            self._respond(401, {"error": "unauthorized"})
            return

        self._respond(200, {"ok": True})

    def _handle_deploy(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        except Exception:
            self._respond(400, {"error": "invalid json"})
            return

        if AUTH_PASSWORD:
            password = body.get("password", "")
            if password != AUTH_PASSWORD:
                self._respond(401, {"error": "unauthorized"})
                return

        account_num = body.get("account")
        psid = body.get("psid", "").strip()
        psidts = body.get("psidts", "").strip()

        if not account_num or not psid or not psidts:
            self._respond(400, {"error": "missing account/psid/psidts"})
            return

        try:
            num = int(account_num)
            if num < 1 or num > 50:
                raise ValueError
        except ValueError:
            self._respond(400, {"error": "invalid account number (1-50)"})
            return

        env_path = ENVS_DIR / f"account{num}.env"
        env_content = f"API_KEY={API_KEY}\nSECURE_1PSID={psid}\nSECURE_1PSIDTS={psidts}\n"

        try:
            env_path.write_text(env_content)
        except Exception as e:
            self._respond(500, {"error": f"write env failed: {e}"})
            return

        container_name = f"{CONTAINER_PREFIX}{num}"
        try:
            result = subprocess.run(
                ["docker", "compose", "-f", str(COMPOSE_FILE),
                 "up", "-d", "--force-recreate", f"gemini-api-{num}"],
                capture_output=True, text=True, timeout=30,
                cwd=str(ROOT_DIR))
            if result.returncode != 0:
                self._respond(500, {
                    "error": f"restart failed: {result.stderr[:200]}",
                    "env_written": True})
                return
        except subprocess.TimeoutExpired:
            self._respond(500, {"error": "restart timeout", "env_written": True})
            return

        self._respond(200, {
            "success": True,
            "message": f"account {num}: env updated + container restarted",
            "container": container_name
        })

    def _handle_status(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        except Exception:
            self._respond(400, {"error": "invalid json"})
            return

        account_num = body.get("account")
        if not account_num:
            self._respond(400, {"error": "missing account"})
            return

        container_name = f"{CONTAINER_PREFIX}{account_num}"
        try:
            result = subprocess.run(
                ["docker", "inspect", container_name, "--format",
                 "{{.State.Status}} {{.State.Health.Status}}"],
                capture_output=True, text=True, timeout=10)
            status = result.stdout.strip() if result.returncode == 0 else "not found"
        except Exception:
            status = "unknown"

        self._respond(200, {"container": container_name, "status": status})

    def _handle_list_accounts(self):
        accounts = []
        numbered = []

        for env_file in ENVS_DIR.glob("account*.env"):
            m = re.match(r"account(\d+)$", env_file.stem)
            if not m:
                continue
            numbered.append((int(m.group(1)), env_file))

        for num, env_file in sorted(numbered, key=lambda x: x[0]):
            try:
                content = env_file.read_text()
                has_cookie = "SECURE_1PSID=" in content
                psid_preview = ""
                for line in content.split("\n"):
                    if line.startswith("SECURE_1PSID=") and not line.startswith("SECURE_1PSIDTS"):
                        val = line.split("=", 1)[1]
                        if not val.strip():
                            has_cookie = False
                        psid_preview = val[:20] + "..." if len(val) > 20 else val
            except Exception:
                has_cookie = False
                psid_preview = ""
            accounts.append({"num": num, "has_cookie": has_cookie, "psid_preview": psid_preview})
        self._respond(200, {"accounts": accounts})

    def _respond(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def log_message(self, fmt, *args):
        print(f"[cookie-manager] {args[0]} {args[1]} {args[2]}")


# ── Main ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not AUTH_PASSWORD:
        print("[cookie-manager] WARNING: No COOKIE_MANAGER_PASSWORD set, login is disabled!")

    server = HTTPServer(("0.0.0.0", LISTEN_PORT), DeployHandler)
    print(f"[cookie-manager] Listening on 0.0.0.0:{LISTEN_PORT}")
    print(f"[cookie-manager] Serving UI from {INDEX_HTML}")
    print(f"[cookie-manager] Managing envs in {ENVS_DIR}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[cookie-manager] Shutting down")
        server.server_close()
