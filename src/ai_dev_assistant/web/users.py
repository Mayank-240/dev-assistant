"""Named multi-user support layered on the single ADA_API_TOKEN owner auth.

Users live in ``<data_dir>/users.json`` as a list of
``{name, token_sha256, created_at}``. Only the sha256 of a user's token is ever
stored — the raw token is generated server-side and shown exactly once, at
creation. Two identities exist outside the file:

- ``owner`` — whoever presents the raw ADA_API_TOKEN (the admin identity);
- ``local`` — the pseudo-user every request resolves to when auth is off
  entirely (loopback bind with no token configured).

Both are reserved and can never be created or deleted as named users.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from pathlib import Path
from typing import Any

OWNER = "owner"
LOCAL = "local"
RESERVED_NAMES = frozenset({OWNER, LOCAL})

# Slug names: lowercase alphanumerics and inner hyphens, 1-32 chars.
_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$")


def users_path(data_dir: Path) -> Path:
    return Path(data_dir) / "users.json"


def load_users(data_dir: Path) -> list[dict[str, Any]]:
    """Stored users, [] when the file is missing/corrupt (tolerant read)."""
    try:
        data = json.loads(users_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    out: list[dict[str, Any]] = []
    for u in data:
        if isinstance(u, dict) and u.get("name") and u.get("token_sha256"):
            out.append({"name": str(u["name"]),
                        "token_sha256": str(u["token_sha256"]),
                        "created_at": u.get("created_at")})
    return out


def save_users(data_dir: Path, users: list[dict[str, Any]]) -> None:
    path = users_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(users, indent=2) + "\n", encoding="utf-8")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def validate_name(name: str, existing: list[dict[str, Any]]) -> str | None:
    """Error message for an unacceptable new-user name, None when it is fine."""
    if not _NAME_RE.match(name or ""):
        return "name must be a slug: lowercase letters, digits and hyphens"
    if name in RESERVED_NAMES:
        return f"'{name}' is a reserved name"
    if any(u["name"] == name for u in existing):
        return f"user '{name}' already exists"
    return None


def create_user(data_dir: Path, name: str) -> tuple[dict[str, Any], str]:
    """Add a user with a server-generated token; returns (entry, raw_token).

    The raw token is never recoverable later — only its sha256 is stored.
    Raises ValueError on invalid/reserved/duplicate names.
    """
    users = load_users(data_dir)
    err = validate_name(name, users)
    if err:
        raise ValueError(err)
    token = secrets.token_urlsafe(32)
    entry = {"name": name, "token_sha256": hash_token(token),
             "created_at": time.time()}
    users.append(entry)
    save_users(data_dir, users)
    return entry, token


def delete_user(data_dir: Path, name: str) -> bool:
    """Revoke a user (their token stops resolving). False when unknown."""
    users = load_users(data_dir)
    kept = [u for u in users if u["name"] != name]
    if len(kept) == len(users):
        return False
    save_users(data_dir, kept)
    return True


def resolve_user(data_dir: Path, supplied: str, owner_token: str) -> str | None:
    """``"owner"`` for the ADA_API_TOKEN, a user's name for their token, else None.

    Comparisons are constant-time: the raw owner token directly, stored users
    via sha256(supplied) against their stored hash.
    """
    supplied = (supplied or "").strip()
    if not supplied:
        return None
    if owner_token and secrets.compare_digest(supplied, owner_token):
        return OWNER
    supplied_hash = hash_token(supplied)
    for u in load_users(data_dir):
        if secrets.compare_digest(supplied_hash, u["token_sha256"]):
            return u["name"]
    return None
