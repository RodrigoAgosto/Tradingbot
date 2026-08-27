"""Logging setup with mandatory secret redaction.

Every handler installed through setup_logging() formats records through
RedactingFormatter, which scrubs:
  * any value registered via register_secret() (e.g. the private key,
    telegram token, SMTP password),
  * anything that even looks like an EVM private key (64 hex chars),
  * KEY/TOKEN/PASSWORD env-style assignments.

This applies to messages, args and formatted tracebacks, because redaction
runs on the final formatted string.
"""

from __future__ import annotations

import logging
import os
import re
import sys

_SECRETS: set[str] = set()

# Exactly 64 hex chars (optionally 0x-prefixed) — an EVM private key. The
# lookarounds keep longer digit runs (e.g. 77-digit Polymarket token ids,
# which are public) from being clipped.
_HEX_KEY_RE = re.compile(r"(?<![0-9a-fA-F])(?:0x)?[0-9a-fA-F]{64}(?![0-9a-fA-F])")
_ASSIGNMENT_RE = re.compile(
    r"((?:PRIVATE_KEY|API_KEY|TOKEN|PASSWORD|SECRET)\s*[=:]\s*)(\S+)",
    re.IGNORECASE,
)

REDACTED = "[REDACTED]"


def register_secret(value: str | None) -> None:
    if value and len(value) >= 6:
        _SECRETS.add(value)


def register_env_secrets() -> None:
    """Register well-known secret env vars so they can never be logged."""
    for var in (
        "POLYMARKET_PRIVATE_KEY",
        "TELEGRAM_BOT_TOKEN",
        "SMTP_PASSWORD",
    ):
        register_secret(os.environ.get(var))


def redact(text: str) -> str:
    for secret in _SECRETS:
        if secret in text:
            text = text.replace(secret, REDACTED)
    text = _HEX_KEY_RE.sub(REDACTED, text)
    text = _ASSIGNMENT_RE.sub(rf"\1{REDACTED}", text)
    return text


class RedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))

    def formatException(self, ei) -> str:  # noqa: ANN001 - logging signature
        return redact(super().formatException(ei))


def setup_logging(level: int = logging.INFO) -> None:
    register_env_secrets()
    root = logging.getLogger()
    root.setLevel(level)
    if not root.handlers:
        root.addHandler(logging.StreamHandler(sys.stderr))
    fmt = RedactingFormatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s", "%Y-%m-%dT%H:%M:%S%z"
    )
    for handler in root.handlers:
        handler.setFormatter(fmt)
