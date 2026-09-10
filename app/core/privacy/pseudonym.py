"""Keyed, process-independent pseudonymisation (master prompt §10.2, §22, §31).

Logs, audit events and rate-limit keys never carry a raw subject or conversation id;
they carry a pseudonym. That pseudonym must be *stable across every instance and
restart*, otherwise an auditor cannot join one customer's events and a shared rate
limiter keeps one budget per pod. Python's ``hash()`` is randomised per process and is
therefore unusable here; this module uses HMAC-SHA256 under a shared secret.

The secret is configured once at startup (``PSEUDONYM_SECRET``); non-hardened
environments fall back to a fixed development key so tests and local runs are
reproducible. Production and preprod refuse to start without a real secret.
"""

from __future__ import annotations

import hashlib
import hmac
import threading

_DEV_KEY = b"protec-dev-pseudonym-key-not-for-production"

_lock = threading.Lock()
_key: bytes = _DEV_KEY


def configure_pseudonymizer(secret: str | None) -> None:
    """Install the shared key. ``None`` keeps the development default."""
    global _key
    with _lock:
        _key = secret.encode("utf-8") if secret else _DEV_KEY


def pseudonymize(prefix: str, value: str, *, digits: int = 12) -> str:
    """Return ``<prefix>_<digits>`` derived deterministically from ``value``.

    The output shape matches the historical ``sub_000000000000`` form so dashboards,
    alert rules and log queries keep working.
    """
    tag = hmac.new(_key, value.encode("utf-8"), hashlib.sha256).hexdigest()
    numeric = int(tag[:16], 16) % 10**digits
    return f"{prefix}_{numeric:0{digits}d}"
