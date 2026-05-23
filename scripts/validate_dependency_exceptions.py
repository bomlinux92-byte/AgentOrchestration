"""Dependency review exception validation script.

Validates that all active dependency review exceptions have:
- owner, reason, expires, package, ecosystem
- A link to the review (url/issue/pr)
- Non-expired date
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

MANIFEST = Path("dependency-review-exceptions.json")
REQUIRED = ("package", "ecosystem", "owner", "reason", "expires")


def main() -> int:
    if not MANIFEST.exists():
        print(f"{MANIFEST}: missing tracked dependency review exception manifest")
        return 1

    try:
        data = json.loads(MANIFEST.read_text())
    except json.JSONDecodeError as exc:
        print(f"{MANIFEST}: invalid JSON: {exc}")
        return 1

    exceptions = data.get("exceptions")
    if not isinstance(exceptions, list):
        print(f'{MANIFEST}: top-level "exceptions" must be a list')
        return 1

    today = date.today()
    ok = True

    for idx, item in enumerate(exceptions):
        prefix = f"exception[{idx}]"
        if not isinstance(item, dict):
            print(f"{prefix}: must be an object")
            ok = False
            continue

        missing = [k for k in REQUIRED if not str(item.get(k, "")).strip()]
        if missing:
            print(f"{prefix}: missing required field(s): {', '.join(missing)}")
            ok = False

        expires = item.get("expires")
        if expires:
            try:
                expires_on = date.fromisoformat(str(expires))
            except ValueError:
                print(f"{prefix}: expires must be YYYY-MM-DD")
                ok = False
            else:
                if expires_on < today:
                    print(f"{prefix}: expired on {expires_on.isoformat()}")
                    ok = False

        link = item.get("url") or item.get("issue") or item.get("pr")
        if not link:
            print(f"{prefix}: missing review summary link (url/issue/pr)")
            ok = False

    if ok:
        print(f"validated {len(exceptions)} dependency review exception(s)")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())