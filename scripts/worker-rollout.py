#!/usr/bin/env python3
"""Stage an isolated worker and switch validated accounts without restarting the old worker.

Run on the deployment host. Credentials remain on that host and are never printed.
"""
from __future__ import annotations
import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


def command(*args):
    return subprocess.check_output(args, text=True).strip()


def dotenv(path):
    result = {}
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip()
    return result


def private_write(path, content):
    with path.open("x", encoding="utf-8") as out:
        os.chmod(path, 0o600)
        out.write(content)


def set_setting(path, key, value):
    lines = path.read_text().splitlines()
    lines = [line for line in lines if line.split("=", 1)[0].strip() != key]
    lines.append(f"{key}={value}")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as out:
            temporary = out.name
            os.chmod(temporary, 0o600)
            out.write("\n".join(lines) + "\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def request(url, key="", body=None, timeout=15):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def ready(process, url, key):
    for _ in range(15):
        if process.poll() is not None:
            return False
        try:
            status, _ = request(url, key, timeout=2)
            if status == 200:
                return True
        except (OSError, ValueError):
            pass
        time.sleep(1)
    return False


def stop_child(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=15)


def replace_gateway(argv, original_cwd, original_environment, source, environment, log_path, url, key):
    """After old gateway exits, retain responsibility until replacement or rollback is ready."""
    new = None
    try:
        with Path(log_path).open("ab") as log:
            new = subprocess.Popen([argv[0], str(source / "gateway.py")], cwd=source, env=environment,
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        if not ready(new, url, key):
            raise RuntimeError("New gateway failed readiness")
        return new
    except BaseException as exc:
        if new is not None:
            stop_child(new)
        with Path(log_path).open("ab") as log:
            restored = subprocess.Popen(argv, cwd=original_cwd, env=original_environment,
                                        stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        if not ready(restored, url, key):
            raise RuntimeError(f"Gateway replacement and rollback readiness failed; inspect gateway PID {restored.pid}") from exc
        raise RuntimeError(f"New gateway failed; original gateway restored and verified, PID {restored.pid}") from exc


def start(args):
    source = args.source.resolve()
    serving = args.serving.resolve()
    runtime = args.runtime.resolve()
    if source == serving or serving in runtime.parents or runtime == serving or runtime in serving.parents:
        raise RuntimeError("Candidate source and runtime must be isolated from the serving checkout")
    if runtime.exists():
        raise RuntimeError("Candidate runtime already exists; refusing to overwrite credentials or state")
    if not args.slot or any(num <= 0 for num in args.slot):
        raise RuntimeError("Select explicit positive account numbers")
    if set(args.slot) & set(args.exclude):
        raise RuntimeError("An excluded account was selected")
    if command("git", "-C", str(source), "status", "--porcelain", "--untracked-files=no"):
        raise RuntimeError("Candidate source contains uncommitted changes")
    info = json.loads(command("docker", "inspect", args.serving_container))[0]
    old_environment = dict(value.split("=", 1) for value in info["Config"]["Env"] if "=" in value)
    config = dotenv(serving / ".env")
    key = config.get("WORKER_MANAGEMENT_KEY") or config.get("API_KEY")
    if not key:
        raise RuntimeError("Configure a management key before staging a worker")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", args.port))
    runtime.mkdir(mode=0o700, parents=True)
    (runtime / "envs").mkdir(mode=0o700)
    (runtime / "state" / "cookie-cache").mkdir(mode=0o700, parents=True)
    for num in sorted(set(args.slot)):
        private_write(runtime / "envs" / f"account{num}.env", (serving / "envs" / f"account{num}.env").read_text())
    env = {name: old_environment[name] for name in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "TZ", "IMAGE_DOWNLOAD_SIZE") if name in old_environment}
    env.update(API_KEY=config.get("API_KEY", ""), WORKER_MANAGEMENT_KEY=key,
               ENVS_DIR="/app/envs", ONECLICK_STATE_DIR="/app/state", GEMINI_COOKIE_PATH="/app/state/cookie-cache")
    if any("\n" in value or "\r" in value for value in env.values()):
        raise RuntimeError("Invalid multiline environment value")
    private_write(runtime / "runtime.env", "".join(f"{key}={value}\n" for key, value in env.items()))
    mounts = [(runtime / "envs", "/app/envs", "rw"), (runtime / "state", "/app/state", "rw"),
              (source / "lib/gemini_webapi", "/app/gemini_webapi", "ro"), (source / "app/parsers", "/app/parsers", "ro")]
    mounts += [(source / "app" / name, "/app/" + name, "ro") for name in
               ("worker.py", "slot.py", "worker_events.py", "raw_capture_tracer.py")]
    invocation = ["docker", "run", "-d", "--name", args.name, "--network", "host", "--restart", "unless-stopped",
                  "--env-file", str(runtime / "runtime.env")]
    for host, container, mode in mounts:
        invocation += ["-v", f"{host}:{container}:{mode}"]
    invocation += [info["Image"], "/app/.venv/bin/python", "-m", "uvicorn", "worker:app", "--host", "127.0.0.1", "--port", str(args.port)]
    container_id = command(*invocation)
    metadata = {"container": args.name, "container_id": container_id, "port": args.port,
                "slots": sorted(set(args.slot)), "commit": command("git", "-C", str(source), "rev-parse", "HEAD"),
                "old_worker_started_at": info["State"]["StartedAt"]}
    private_write(runtime / "rollout.json", json.dumps(metadata, indent=2))
    print(json.dumps(metadata))


def gateway(args):
    source, serving = args.source.resolve(), args.serving.resolve()
    pid = args.pid
    process = Path(f"/proc/{pid}")
    argv = [value.decode() for value in (process / "cmdline").read_bytes().split(b"\0") if value]
    if len(argv) != 2 or Path(argv[1]).name != "gateway.py":
        raise RuntimeError("PID is not the expected gateway process")
    environment = dict(value.split("=", 1) for value in (process / "environ").read_bytes().decode().split("\0") if "=" in value)
    original_environment = dict(environment)
    original_cwd = (process / "cwd").resolve()
    config = dotenv(serving / ".env")
    port = int(environment.get("GATEWAY_PORT") or config.get("GATEWAY_PORT", "9880"))
    for _ in range(2):
        status, data = request(f"http://127.0.0.1:{port}/gateway/status", config.get("COOKIE_MANAGER_PASSWORD", ""))
        if status != 200 or any(row.get("busy") for row in data.get("containers", [])):
            raise RuntimeError("Gateway has active requests or cannot confirm it is idle; no process was stopped")
        time.sleep(2)
    for name in (".env", "envs", "state"):
        destination, original = source / name, serving / name
        if destination.exists() or destination.is_symlink():
            if destination.resolve() != original.resolve():
                raise RuntimeError("Candidate gateway configuration is not the expected serving configuration")
        else:
            destination.symlink_to(original, target_is_directory=original.is_dir())
    known = environment.get("WORKER_CANDIDATE_URLS") or config.get("WORKER_CANDIDATE_URLS", "")
    environment["WORKER_CANDIDATE_URLS"] = ",".join(sorted(set(filter(None, known.split(","))) | {args.worker_url}))
    set_setting(serving / ".env", "WORKER_CANDIDATE_URLS", environment["WORKER_CANDIDATE_URLS"])
    # Validate imports before stopping the gateway. The worker is never signalled.
    subprocess.run([sys.executable, "-m", "py_compile", str(source / "gateway.py"), str(source / "app/worker_routing.py")], check=True)
    with Path(args.log).open("ab") as log:
        subprocess.run([argv[0], "-c", "import gateway"], cwd=source, env=environment,
                       stdout=log, stderr=log, check=True)
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 660  # allow the existing 600-second chat read window to drain
    while process.exists() and time.monotonic() < deadline:
        time.sleep(0.2)
    if process.exists():
        # Do not abandon a terminating gateway that could disappear after this script exits.
        os.kill(pid, signal.SIGKILL)
        while process.exists():
            time.sleep(0.2)
    new = replace_gateway(argv, original_cwd, original_environment, source, environment, args.log,
                          f"http://127.0.0.1:{port}/gateway/status", config.get("COOKIE_MANAGER_PASSWORD", ""))
    print(json.dumps({"gateway_pid": new.pid, "worker_url": args.worker_url, "old_worker_restarted": False}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--serving", type=Path, required=True)
    sub = parser.add_subparsers(dest="action", required=True)
    candidate = sub.add_parser("start")
    candidate.add_argument("--runtime", type=Path, required=True)
    candidate.add_argument("--slot", type=int, action="append", required=True)
    candidate.add_argument("--exclude", type=int, action="append", default=[])
    candidate.add_argument("--port", type=int, required=True)
    candidate.add_argument("--name", required=True)
    candidate.add_argument("--serving-container", default="gemini_worker")
    panel = sub.add_parser("gateway")
    panel.add_argument("--pid", type=int, required=True)
    panel.add_argument("--worker-url", required=True)
    panel.add_argument("--log", default="/tmp/gateway.log")
    switch = sub.add_parser("switch")
    switch.add_argument("--slot", type=int, required=True)
    switch.add_argument("--worker-url", required=True)
    args = parser.parse_args()
    if args.action == "start":
        start(args)
    elif args.action == "gateway":
        gateway(args)
    else:
        config = dotenv(args.serving / ".env")
        status, data = request(f"http://127.0.0.1:{config.get('GATEWAY_PORT', '9880')}/gateway/worker-route/{args.slot}",
                               config.get("COOKIE_MANAGER_PASSWORD", ""), {"worker_url": args.worker_url}, timeout=80)
        print(json.dumps({"http_status": status, "result": data}, ensure_ascii=False))
        if status != 200:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
