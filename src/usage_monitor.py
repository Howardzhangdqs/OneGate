from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

SESSION_ORPHAN_TTL = 120.0
SESSION_SWEEP_INTERVAL = 30.0


@dataclass
class UsageSession:
    session_id: str
    token: str
    start: str | None
    end: str | None
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=256))
    created_at: float = field(default_factory=time.time)
    attached: bool = False


class UsageMonitor:
    def __init__(self, token_pool: dict):
        self.token_pool = token_pool
        self.sessions: dict[str, UsageSession] = {}
        self.token_sessions: dict[str, set[str]] = {}
        self._last_sweep = 0.0

    def is_active(self, token: str | None) -> bool:
        self._sweep_sessions()
        if not token:
            return False
        return bool(self.token_sessions.get(token))

    def create_session(
        self, token: str, start: str | None = None, end: str | None = None
    ) -> str | None:
        self._sweep_sessions()
        entry = self.token_pool.get(token)
        if not entry:
            return None
        session_id = secrets.token_hex(16)
        session = UsageSession(session_id=session_id, token=token, start=start, end=end)
        self.sessions[session_id] = session
        self.token_sessions.setdefault(token, set()).add(session_id)
        logger.info(
            "usage session created: %s... for token %s...",
            session_id[:8],
            token[:8],
        )
        return session_id

    def _sweep_sessions(self) -> None:
        now = time.time()
        if now - self._last_sweep < SESSION_SWEEP_INTERVAL:
            return
        self._last_sweep = now
        for session in list(self.sessions.values()):
            if not session.attached and now - session.created_at > SESSION_ORPHAN_TTL:
                self.remove_session(session.session_id)

    def remove_session(self, session_id: str) -> None:
        session = self.sessions.pop(session_id, None)
        if session:
            sids = self.token_sessions.get(session.token)
            if sids:
                sids.discard(session_id)
                if not sids:
                    del self.token_sessions[session.token]
            logger.info("usage session removed: %s...", session_id[:8])

    def notify_usage(self, token: str, delta: dict) -> None:
        sids = self.token_sessions.get(token)
        if not sids:
            return
        payload = f"event: delta\ndata: {json.dumps(delta, ensure_ascii=False)}\n\n"
        encoded = payload.encode("utf-8")
        dead = []
        for sid in list(sids):
            session = self.sessions.get(sid)
            if not session:
                dead.append(sid)
                continue
            try:
                session.queue.put_nowait(encoded)
            except asyncio.QueueFull:
                dead.append(sid)
        for sid in dead:
            self.remove_session(sid)
