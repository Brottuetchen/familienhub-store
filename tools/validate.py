"""Checks index.json with the hub's own parser and checks that every screenshot exists.

Run from the store repository with the Familienhub backend on PYTHONPATH:
    PYTHONPATH=<familienhub>/backend python tools/validate.py
"""

import pathlib
import sys

from app.store.index import parse_index

ROOT = pathlib.Path(__file__).resolve().parent.parent
BASE = "https://raw.githubusercontent.com/Brottuetchen/familienhub-store/main/index.json"
PREFIX = BASE.rsplit("/", 1)[0] + "/"

parsed = parse_index((ROOT / "index.json").read_bytes(), BASE, frozenset({"raw.githubusercontent.com"}))
problems = [f"skipped entry: {s}" for s in parsed.skipped]
ids = [e.id for e in parsed.entries]
problems += [f"duplicate id: {i}" for i in sorted({i for i in ids if ids.count(i) > 1})]
for entry in parsed.entries:
    for url in entry.screenshots:
        if not url.startswith(PREFIX):
            continue  # absolute URL on an allowed host, checked by the parser
        path = ROOT / url[len(PREFIX):]
        if not path.is_file():
            problems.append(f"{entry.id}: missing screenshot {path.relative_to(ROOT)}")
        elif path.stat().st_size > 2 * 1024 * 1024:
            problems.append(f"{entry.id}: screenshot larger than 2 MB: {path.relative_to(ROOT)}")

for p in problems:
    print("ERROR", p)
print(f"{len(parsed.entries)} entries checked, {len(problems)} problems")
sys.exit(1 if problems else 0)
