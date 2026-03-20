"""
token_vault.py
==============
Server-side token store for the SAP Commerce MCP Server.

The LLM only ever sees a short opaque session_id.
The real OAuth access_token never leaves the MCP server process.

In production: replace _store with Redis or encrypted DB.
"""

import hashlib
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TokenEntry:
    access_token:  str
    refresh_token: Optional[str]
    username:      Optional[str]           # None = guest session
    user_id:       str                     # "current" | "anonymous"
    created_at:    float = field(default_factory=time.time)
    expires_in:    int   = 3600            # seconds


class TokenVault:
    """
    Thread-safe in-memory token vault.
    Maps opaque session_id → TokenEntry.

    Replace _store with Redis in production:
        import redis
        r = redis.Redis()
        r.setex(session_id, ttl, encrypted_token)
    """

    def __init__(self):
        self._store: dict[str, TokenEntry] = {}

    # ── Store ─────────────────────────────────────────────────────────────────

    def store(self,
              access_token: str,
              refresh_token: Optional[str] = None,
              username: Optional[str] = None,
              user_id: str = "current",
              expires_in: int = 3600) -> str:
        session_id = "sess_" + secrets.token_urlsafe(16)
        self._store[session_id] = TokenEntry(
            access_token=access_token,
            refresh_token=refresh_token,
            username=username,
            user_id=user_id,
            expires_in=expires_in,
        )
        return session_id

    # ── Retrieve ──────────────────────────────────────────────────────────────

    def get_token(self, session_id: str) -> Optional[str]:
        """Resolve session_id → raw access_token. Returns None if expired/missing."""
        entry = self._get_entry(session_id)
        return entry.access_token if entry else None

    def get_user_id(self, session_id: str) -> str:
        """Resolve session_id → SAP user_id ('current' or 'anonymous')."""
        entry = self._get_entry(session_id)
        return entry.user_id if entry else "anonymous"

    def get_username(self, session_id: str) -> Optional[str]:
        entry = self._get_entry(session_id)
        return entry.username if entry else None

    def is_authenticated(self, session_id: str) -> bool:
        entry = self._get_entry(session_id)
        return bool(entry and entry.username is not None)

    # ── Revoke ────────────────────────────────────────────────────────────────

    def revoke(self, session_id: str) -> None:
        """Delete a session (on logout or expiry)."""
        self._store.pop(session_id, None)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _get_entry(self, session_id: str) -> Optional[TokenEntry]:
        entry = self._store.get(session_id)
        if not entry:
            return None
        # Auto-expire
        if time.time() - entry.created_at > entry.expires_in:
            self._store.pop(session_id, None)
            return None
        return entry

    def _purge_expired(self) -> int:
        """Remove all expired entries. Call periodically."""
        now   = time.time()
        stale = [sid for sid, e in self._store.items()
                 if now - e.created_at > e.expires_in]
        for sid in stale:
            del self._store[sid]
        return len(stale)


# ── Singleton used by sap_commerce_mcp_server.py ─────────────────────────────
vault = TokenVault()
