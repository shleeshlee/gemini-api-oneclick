#!/usr/bin/env python3
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = ROOT / "state"
STATE_DIR.mkdir(parents=True, exist_ok=True)
STATE_FILE = STATE_DIR / "channel_guard_state.json"
LOG_FILE = STATE_DIR / "channel_guard.log"


def env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


ERROR_THRESHOLD = int(env("GUARD_ERROR_THRESHOLD", "3"))
LOG_WINDOW_SECONDS = int(env("GUARD_LOG_WINDOW_SECONDS", "300"))
NEWAPI_CONTAINER = env("GUARD_NEWAPI_CONTAINER", "newapi-new-api-1")
NEWAPI_MYSQL_CONTAINER = env("GUARD_NEWAPI_MYSQL_CONTAINER", "newapi-mysql-1")
DB_NAME = env("NEWAPI_DB_NAME", "new-api")
DB_USER = env("NEWAPI_DB_USER", "root")
DB_PASS = env("NEWAPI_DB_PASS", "")
CONTAINER_PREFIX = env("CONTAINER_PREFIX", "gemini_api_account_")
MAX_SEEN_IDS = 5000


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(cmd: list[str]):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return p.returncode, p.stdout, p.stderr


def append_log(msg: str) -> None:
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"[{now_iso()}] {msg}\n")


def mysql_cmd(sql: str):
    if not DB_PASS:
        raise RuntimeError("NEWAPI_DB_PASS is empty")
    cmd = [
        "docker",
        "exec",
        NEWAPI_MYSQL_CONTAINER,
        "mysql",
        "-N",
        "-B",
        f"-u{DB_USER}",
        f"-p{DB_PASS}",
        "-D",
        DB_NAME,
        "-e",
        sql,
    ]
    code, out, err = run(cmd)
    if code != 0:
        raise RuntimeError(err.strip() or "mysql command failed")
    return out


def sql_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "''")


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {
            "last_scan_epoch": 0,
            "error_counts": {},
            "disabled_by_guard": {},
            "container_started_at": {},
            "seen_request_ids": [],
        }
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {
            "last_scan_epoch": 0,
            "error_counts": {},
            "disabled_by_guard": {},
            "container_started_at": {},
            "seen_request_ids": [],
        }


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=True, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(STATE_FILE)


def parse_port(base_url: str):
    try:
        return urlparse(base_url).port
    except Exception:
        return None


def channel_container(base_url: str):
    port = parse_port(base_url)
    if port is None:
        return None
    if 8001 <= port <= 65535:
        return f"{CONTAINER_PREFIX}{port - 8000}"
    return None


def fetch_channels() -> tuple[dict, dict]:
    out = mysql_cmd("SELECT id,IFNULL(base_url,''),status FROM channels ORDER BY id;")
    info: dict[int, dict] = {}
    by_container: dict[str, list[int]] = {}
    for line in out.strip().splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        cid = int(parts[0])
        base_url = parts[1]
        status = int(parts[2] or 0)
        ctn = channel_container(base_url)
        info[cid] = {"base_url": base_url, "status": status, "container": ctn}
        if ctn:
            by_container.setdefault(ctn, []).append(cid)
    return info, by_container


def container_started_at(container: str):
    code, out, _ = run(["docker", "inspect", "--format", "{{.State.StartedAt}}", container])
    if code != 0:
        return None
    return out.strip() or None


def should_count(status_code: int, msg: str) -> bool:
    m = msg.lower()
    if status_code >= 500:
        return True
    return "credentials not configured" in m or "failed to initialize gemini client" in m


def disable_channel(cid: int, count: int, reason: str):
    msg = f"AUTO-DISABLED by channel_guard {now_iso()} count={count} reason={reason[:120]}"
    mysql_cmd(
        "UPDATE channels SET status=2, remark=CONCAT(IFNULL(remark,''),"
        "CASE WHEN IFNULL(remark,'')='' THEN '' ELSE ' | ' END,"
        f"'{sql_escape(msg)}') WHERE id={cid};"
    )
    append_log(f"disable channel #{cid}: {msg}")


def recover_channel(cid: int, container: str):
    msg = f"AUTO-RECOVERED by channel_guard {now_iso()} container_restart={container}"
    mysql_cmd(
        "UPDATE channels SET status=1, remark=CONCAT(IFNULL(remark,''),"
        "CASE WHEN IFNULL(remark,'')='' THEN '' ELSE ' | ' END,"
        f"'{sql_escape(msg)}') WHERE id={cid};"
    )
    append_log(f"recover channel #{cid}: {msg}")


def parse_errors(since_epoch: int):
    code, _, err = run(["docker", "logs", "--since", str(since_epoch), NEWAPI_CONTAINER])
    if code != 0:
        raise RuntimeError(err.strip() or "docker logs failed")
    pat = re.compile(r"\|\s*([A-Za-z0-9]+)\s*\|\s*channel error \(channel #(\d+), status code: (\d+)\):\s*(.*)$")
    events = []
    for line in err.splitlines():
        m = pat.search(line)
        if not m:
            continue
        req_id = m.group(1)
        cid = int(m.group(2))
        status_code = int(m.group(3))
        msg = m.group(4).strip()
        events.append((req_id, cid, status_code, msg))
    return events


def main() -> int:
    state = load_state()
    now_ts = int(time.time())
    since = int(state.get("last_scan_epoch", 0))
    if since <= 0:
        since = now_ts - LOG_WINDOW_SECONDS
    else:
        since = max(0, since - 2)

    channels, by_container = fetch_channels()

    prev = state.get("container_started_at", {})
    curr = {}
    for ctn in by_container.keys():
        cur_started = container_started_at(ctn)
        curr[ctn] = cur_started
        old_started = prev.get(ctn)
        if old_started and cur_started and old_started != cur_started:
            for key in list(state.get("disabled_by_guard", {}).keys()):
                if not str(key).isdigit():
                    continue
                cid = int(key)
                if channels.get(cid, {}).get("container") == ctn:
                    recover_channel(cid, ctn)
                    state["disabled_by_guard"].pop(str(cid), None)
                    state["error_counts"][str(cid)] = 0

    seen_ids = list(state.get("seen_request_ids", []))
    seen_set = set(seen_ids)
    events = parse_errors(since)

    for req_id, cid, status_code, msg in events:
        if req_id in seen_set:
            continue
        seen_set.add(req_id)
        seen_ids.append(req_id)

        if cid not in channels or not should_count(status_code, msg):
            continue

        k = str(cid)
        cnt = int(state.get("error_counts", {}).get(k, 0)) + 1
        state.setdefault("error_counts", {})[k] = cnt

        if cnt >= ERROR_THRESHOLD:
            if k not in state.setdefault("disabled_by_guard", {}) and int(channels[cid].get("status", 0)) == 1:
                disable_channel(cid, cnt, f"status={status_code} {msg}")
                state["disabled_by_guard"][k] = {
                    "disabled_at": now_iso(),
                    "container": channels[cid].get("container"),
                    "reason": f"status={status_code} {msg[:200]}",
                }

    if len(seen_ids) > MAX_SEEN_IDS:
        seen_ids = seen_ids[-MAX_SEEN_IDS:]

    state["seen_request_ids"] = seen_ids
    state["container_started_at"] = curr
    state["last_scan_epoch"] = now_ts
    save_state(state)
    append_log(f"loop done: events={len(events)} disabled_by_guard={len(state.get('disabled_by_guard', {}))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
