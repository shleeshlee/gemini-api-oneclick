"""Private proxy library and per-account selections for the gateway."""
import json
import os
from pathlib import Path
import tempfile


class ProxySettings:
    def __init__(self, path):
        self.path = Path(path)
        self.proxies = {}
        self.assignments = {}

    def load(self):
        if self.path.exists():
            data = json.loads(self.path.read_text())
            self.proxies = {row['id']: row for row in data.get('proxies', [])}
            self.assignments = {str(k): v for k, v in data.get('assignments', {}).items() if v in self.proxies}

    def commit(self, proxies=None, assignments=None):
        profiles = dict(self.proxies if proxies is None else proxies)
        choices = dict(self.assignments if assignments is None else assignments)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent, delete=False) as file:
                temporary = file.name
                os.chmod(temporary, 0o600)
                json.dump({'proxies': list(profiles.values()), 'assignments': choices}, file, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
            self.proxies, self.assignments = profiles, choices
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)

    def public(self):
        # Only expose through panel-authenticated endpoints; URLs may contain credentials.
        return {'proxies': list(self.proxies.values()), 'assignments': dict(self.assignments)}
