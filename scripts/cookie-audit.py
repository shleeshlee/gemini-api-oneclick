#!/usr/bin/env python3
"""Read bounded cookie-audit history without opening any credentials or keys."""
import argparse
from collections import deque
from datetime import datetime, timezone
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=Path("state"))
    parser.add_argument("--slot", type=int, required=True)
    parser.add_argument("--limit", type=int, default=80)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be positive")
    root = args.state_dir / "cookie-audit"
    records = deque(maxlen=args.limit)
    files = [root / f"events.jsonl.{i}" for i in range(5, 0, -1)] + [root / "events.jsonl"]
    for path in files:
        if not path.exists():
            continue
        with path.open() as source:
            for line in source:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue  # An active writer may not have completed its last line.
                if row.get("slot") == args.slot:
                    records.append(row)
    if not records:
        print("No cookie audit records for this slot in retained history.")
        return
    for row in records:
        if args.json:
            print(json.dumps(row, ensure_ascii=False))
            continue
        timestamp = datetime.fromtimestamp(row["time"], tz=timezone.utc).isoformat(timespec="seconds")
        details = {k: row[k] for k in ("status", "reason", "error_type", "route", "matches_file",
                                       "matches_previous", "previous_comparable", "overlapped",
                                       "transport_success", "client_changed_during_write") if k in row}
        if row.get("changes"):
            details["changes"] = [f'{c["name"]}:{c["change"]}' for c in row["changes"]]
        print(timestamp, "boot=" + row["boot_id"][:8], "client=" + row["client_id"][:8],
              "op=" + row.get("operation_id", "-")[:8], row["event"], json.dumps(details, ensure_ascii=False))


if __name__ == "__main__":
    main()
