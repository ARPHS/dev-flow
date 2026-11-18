import json
import os
from pathlib import Path


def path():
    return Path(os.environ.get("LEDGER_FILE", "ledger.json"))


def load():
    file = path()
    if not file.exists():
        return []
    return json.loads(file.read_text(encoding="utf-8"))


def append(entry):
    entries = load()
    entries.append(entry)
    path().write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    return entry
