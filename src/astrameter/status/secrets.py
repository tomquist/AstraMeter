"""Keep credentials off the wire, in both directions.

The dashboard has to show a config that contains passwords, tokens and
broker URIs.  Rather than trust the transport, every secret is replaced with
a sentinel on the way out; a value that comes back still equal to the
sentinel means "keep what is stored".  Plaintext credentials therefore never
cross the wire in either direction.

The key patterns mirror what the add-on already redacts from the
add-on log, so the two agree on what counts as a secret.
"""

from __future__ import annotations

import re

# Eight bullets.  Deliberately not a plausible password, and identical in
# every field so its length leaks nothing about the real value.
SENTINEL = "•" * 8

_SECRET_KEY = re.compile(
    r"(password|passwd|secret|token|api[_-]?key|accesstoken|mailbox)",
    re.IGNORECASE,
)
_URI_USERINFO = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)([^/@\s:]+:[^/@\s]+)@")
#: The shape :func:`redact_value` leaves behind, so a round trip can find the
#: hole it made and fill only that.
_REDACTED_USERINFO = re.compile(
    r"([a-zA-Z][a-zA-Z0-9+.-]*://)" + re.escape(f"{SENTINEL}:{SENTINEL}") + "@"
)


def is_secret_key(key: str) -> bool:
    """True when *key* names a value that must not leave the process."""
    return bool(_SECRET_KEY.search(key))


def redact_value(key: str, value):
    """Replace a secret value, or the userinfo inside a URI, with the sentinel."""
    if not isinstance(value, str):
        return value
    if is_secret_key(key):
        return SENTINEL if value else value
    return _URI_USERINFO.sub(rf"\1{SENTINEL}:{SENTINEL}@", value)


def redact_sections(sections: dict) -> dict:
    """Redact every secret in a ``{section: {key: value}}`` mapping."""
    return {
        name: {key: redact_value(key, value) for key, value in pairs.items()}
        for name, pairs in sections.items()
    }


def _restore_value(key: str, value: str, stored):
    """Put the stored secret back into an echoed value.

    For a plain secret the whole value is the secret, so the stored one
    replaces it. For a URI only the userinfo was redacted, and the rest of it
    is the user's to edit — splicing just the credential back keeps a host or
    port change they made in the same round trip.
    """
    if is_secret_key(key):
        return stored
    match = _URI_USERINFO.search(stored) if isinstance(stored, str) else None
    if match is None:
        return stored
    spliced, count = _REDACTED_USERINFO.subn(
        lambda hole: f"{hole.group(1)}{match.group(2)}@", value, count=1
    )
    # No hole in the shape we punched: the sentinel is somewhere we do not
    # understand, so fall back to the stored value rather than write bullets.
    return spliced if count else stored


def restore_sections(sections: dict, current: dict) -> dict:
    """Put stored secrets back where the client echoed the sentinel.

    A client that never saw the real value cannot send it back, so this is
    what makes a round-trip edit of an unrelated field non-destructive.
    """
    out: dict = {}
    for name, pairs in sections.items():
        stored = current.get(name, {})
        restored = {}
        for key, value in pairs.items():
            if isinstance(value, str) and SENTINEL in value and key in stored:
                restored[key] = _restore_value(key, value, stored[key])
            else:
                restored[key] = value
        out[name] = restored
    return out
