"""The private key must never appear in any log output, including tracebacks."""

import io
import logging

from weatherbot import logutil

FAKE_KEY = "a" * 63 + "b"  # 64 hex chars


def _logger_with_buffer():
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logutil.RedactingFormatter("%(message)s"))
    logger = logging.getLogger("redaction-test")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    return logger, buf


def test_registered_secret_redacted():
    logutil.register_secret("supersecrettoken123")
    logger, buf = _logger_with_buffer()
    logger.info("token is supersecrettoken123 ok")
    assert "supersecrettoken123" not in buf.getvalue()
    assert logutil.REDACTED in buf.getvalue()


def test_hex_key_redacted_even_unregistered():
    logger, buf = _logger_with_buffer()
    logger.error("leaked 0x%s in message", FAKE_KEY)
    out = buf.getvalue()
    assert FAKE_KEY not in out
    assert logutil.REDACTED in out


def test_key_redacted_in_traceback():
    logger, buf = _logger_with_buffer()
    try:
        raise ValueError(f"bad key: {FAKE_KEY}")
    except ValueError:
        logger.exception("boom")
    out = buf.getvalue()
    assert FAKE_KEY not in out
    assert logutil.REDACTED in out


def test_env_style_assignment_redacted():
    logger, buf = _logger_with_buffer()
    logger.info("POLYMARKET_PRIVATE_KEY=shortvalue123")
    out = buf.getvalue()
    assert "shortvalue123" not in out
