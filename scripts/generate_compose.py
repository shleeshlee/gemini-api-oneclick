#!/usr/bin/env python3
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_DIR = ROOT / "envs"
OUT_FILE = ROOT / "docker-compose.accounts.yml"


def read_env(path: Path) -> dict:
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


def main() -> int:
    base = read_env(ROOT / ".env")
    start_port = int(base.get("START_PORT", "8001"))
    image = base.get("IMAGE_NAME", "gemini-api-oneclick:local")
    prefix = base.get("CONTAINER_PREFIX", "gemini_api_account_")

    env_files = []
    pattern = re.compile(r"account(\d+)\.env$")
    for p in ENV_DIR.glob("account*.env"):
        m = pattern.search(p.name)
        if m:
            env_files.append((int(m.group(1)), p))
    env_files.sort(key=lambda x: x[0])

    if not env_files:
        raise SystemExit("No env files found under envs/account*.env")

    lines = [
        "services:",
    ]

    for account_id, p in env_files:
        svc = f"gemini-api-{account_id}"
        port = start_port + account_id - 1
        lines.extend(
            [
                f"  {svc}:",
                "    build:",
                "      context: .",
                "      dockerfile: Dockerfile",
                "      args:",
                "        MODE: accounts",
                f"    image: {image}",
                f"    container_name: {prefix}{account_id}",
                "    env_file:",
                f"      - {p.as_posix().replace(str(ROOT.as_posix()) + '/', './')}",
                "    environment:",
                "      - API_KEY=${API_KEY}",
                "      - TZ=${TZ}",
                "      - HTTP_PROXY=${HTTP_PROXY:-}",
                "      - HTTPS_PROXY=${HTTPS_PROXY:-}",
                "      - NO_PROXY=${NO_PROXY:-localhost,127.0.0.1}",
                "      - GEMINI_COOKIE_PATH=/app/cookie-cache",
                "      - ONECLICK_STATE_DIR=/app/state",
                "    volumes:",
                f"      - ./cookie-cache/account{account_id}:/app/cookie-cache",
                "      - ./app/main.py:/app/main.py:ro",
                "      - ./app/raw_capture_tracer.py:/app/raw_capture_tracer.py:ro",
                "      - ./app/parsers:/app/parsers:ro",
                "      - ./app/worker_events.py:/app/worker_events.py:ro",
                "      - ./lib/gemini_webapi:/app/gemini_webapi:ro",
                "      - ./state:/app/state",
                "    ports:",
                f"      - \"{port}:8000\"",
                "    restart: no",
                "    healthcheck:",
                "      test: [\"CMD\", \"curl\", \"-fsS\", \"http://localhost:8000/health\"]",
                "      interval: 30s",
                "      timeout: 10s",
                "      retries: 3",
                "",
            ]
        )

    OUT_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(f"Generated: {OUT_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
