#!/usr/bin/env python3
"""Pre-commit guard: refuse to commit anything that looks like key material.

Checks staged file contents for:
  * 64-hex private keys (with or without 0x),
  * assignments of real values to *_PRIVATE_KEY / *_TOKEN / *_PASSWORD /
    *_SECRET style names,
  * staging of .env files (other than .env.example).
"""

from __future__ import annotations

import re
import subprocess
import sys

HEX_KEY = re.compile(r"(?:0x)?[0-9a-fA-F]{64}")
ASSIGNMENT = re.compile(
    r"(PRIVATE_KEY|API_KEY|BOT_TOKEN|PASSWORD|SECRET)\s*[=:]\s*['\"]?[A-Za-z0-9+/_-]{8,}",
)


def staged_files() -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        capture_output=True, text=True, check=True,
    ).stdout
    return [f for f in out.splitlines() if f]


def staged_content(path: str) -> str:
    proc = subprocess.run(["git", "show", f":{path}"], capture_output=True, text=True)
    return proc.stdout if proc.returncode == 0 else ""


def main() -> int:
    failures: list[str] = []
    for path in staged_files():
        if path.startswith(".env") and path != ".env.example":
            failures.append(f"{path}: env files must never be committed")
            continue
        content = staged_content(path)
        if HEX_KEY.search(content):
            failures.append(f"{path}: contains a 64-hex string that looks like a private key")
        if ASSIGNMENT.search(content):
            failures.append(f"{path}: contains what looks like a secret assignment")
    if failures:
        print("COMMIT BLOCKED — possible secrets staged:", file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
