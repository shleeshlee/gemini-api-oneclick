"""Persist account destinations and pin existing tasks to their original worker."""
import json
import os
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit


def normalized_worker_url(value):
    value = str(value).rstrip("/")
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname or
            parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment):
        raise ValueError("Worker 地址必须是无路径、凭证或查询参数的 HTTP(S) 地址")
    _ = parsed.port
    return value


class WorkerRouting:
    def __init__(self, path):
        self.path = Path(path)
        self.accounts = {}
        self.sessions = {}
        self.research = {}

    def load(self):
        if not self.path.exists():
            return
        data = json.loads(self.path.read_text(encoding="utf-8"))
        accounts = {int(num): normalized_worker_url(url) for num, url in data.get("accounts", {}).items()}
        sessions = data.get("sessions", {})
        research = data.get("research", {})
        for collection in (sessions, research):
            for row in collection.values():
                row["worker"] = normalized_worker_url(row["worker"])
                row["slot"] = int(row["slot"])
                row["updated_at"] = float(row["updated_at"])
        self.accounts, self.sessions, self.research = accounts, sessions, research
        self.prune()

    def prune(self):
        now = time.time()
        self.sessions = {key: row for key, row in self.sessions.items() if now - row["updated_at"] < 600}
        self.research = {key: row for key, row in self.research.items() if now - row["updated_at"] < 7 * 86400}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, delete=False) as out:
                temporary = out.name
                os.chmod(temporary, 0o600)
                json.dump({"accounts": self.accounts, "sessions": self.sessions, "research": self.research}, out)
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)

    def account(self, num, default):
        return self.accounts.get(num, default)

    def switch(self, num, url):
        previous = dict(self.accounts)
        self.accounts[num] = normalized_worker_url(url)
        try:
            self.save()
        except BaseException:
            self.accounts = previous
            raise

    def remember(self, kind, identity, num, base):
        self.prune()
        collection = self.sessions if kind == "session" else self.research
        collection[identity] = {"slot": num, "worker": base, "updated_at": time.time()}
        self.save()

    def workers(self, default):
        return {default, *self.accounts.values(),
                *(row["worker"] for row in self.sessions.values()),
                *(row["worker"] for row in self.research.values())}
