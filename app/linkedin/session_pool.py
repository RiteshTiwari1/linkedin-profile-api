"""A health-tracked pool of LinkedIn sessions.

Written as a pool even when there is only one cookie, because the interesting
failure mode of this whole project is *account* failure, not server failure.
LinkedIn does not return a clean 429 -- it returns a 999, or an HTML challenge
page, or a 403, and it holds a grudge. So each session is a small state
machine:

    HEALTHY  --rate-limit/challenge-->  COOLING  --timeout-->  HEALTHY
    HEALTHY  --401 / dead cookie----->  DEAD     (terminal)

A COOLING session is skipped until its timer expires; a DEAD one is never
retried, because retrying an invalidated cookie is how you turn a soft block
into a hard one. Adding a second account is then a config change, not a code
change.
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from enum import StrEnum

from ..errors import NoSessionsConfigured, SessionExpired, UpstreamBlocked

# How long a session sits out after LinkedIn pushes back. Generous on purpose:
# hammering a throttled account is what escalates to a permanent restriction.
COOLDOWN_RATE_LIMIT = 30 * 60
COOLDOWN_CHALLENGE = 60 * 60
COOLDOWN_TRANSIENT = 60

# Consecutive transient failures before we assume the session is the problem.
FAILURES_BEFORE_COOLDOWN = 3


class SessionState(StrEnum):
    HEALTHY = "healthy"
    COOLING = "cooling"
    DEAD = "dead"


class FailureKind(StrEnum):
    EXPIRED = "expired"        # cookie no longer valid -> DEAD
    CHALLENGE = "challenge"    # bot check / 999       -> long cooldown
    RATE_LIMIT = "rate_limit"  # 429                   -> cooldown
    TRANSIENT = "transient"    # 5xx, network          -> tolerate a few


@dataclass
class LinkedInSession:
    li_at: str
    jsessionid: str
    label: str
    state: SessionState = SessionState.HEALTHY
    cooldown_until: float = 0.0
    consecutive_failures: int = 0
    requests_served: int = 0
    total_failures: int = 0
    last_used: float | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def available(self) -> bool:
        if self.state is SessionState.DEAD:
            return False
        if self.state is SessionState.COOLING:
            return time.time() >= self.cooldown_until
        return True

    @property
    def cooldown_remaining(self) -> int:
        return max(0, int(self.cooldown_until - time.time()))

    def redacted(self) -> str:
        """Never log a full cookie."""
        return f"{self.li_at[:6]}...{self.li_at[-4:]}" if len(self.li_at) > 12 else "***"


class SessionPool:
    def __init__(self, sessions: list[tuple[str, str]]) -> None:
        self._sessions = [
            LinkedInSession(li_at=li_at, jsessionid=jsid, label=f"session-{i + 1}")
            for i, (li_at, jsid) in enumerate(sessions)
            if li_at
        ]
        self._cycle = itertools.cycle(range(len(self._sessions))) if self._sessions else None

    @property
    def configured(self) -> bool:
        return bool(self._sessions)

    def acquire(self) -> LinkedInSession:
        """Next available session, round-robin so load spreads evenly.

        Raises the *specific* reason nothing is available, so the caller can
        return SESSION_EXPIRED vs UPSTREAM_BLOCKED rather than a vague 503.
        """
        if not self._sessions:
            raise NoSessionsConfigured()

        for _ in range(len(self._sessions)):
            session = self._sessions[next(self._cycle)]
            if session.state is SessionState.COOLING and session.available:
                session.state = SessionState.HEALTHY
                session.consecutive_failures = 0
            if session.available:
                session.last_used = time.time()
                return session

        cooling = [s for s in self._sessions if s.state is SessionState.COOLING]
        if cooling:
            soonest = min(s.cooldown_remaining for s in cooling)
            raise UpstreamBlocked(
                "Every LinkedIn session is in cooldown after upstream push-back.",
                retry_after=soonest,
                details={"sessions_cooling": len(cooling)},
            )
        raise SessionExpired(
            "Every configured LinkedIn session has been invalidated. Log in "
            "again and update LINKEDIN_SESSIONS."
        )

    def report_success(self, session: LinkedInSession) -> None:
        session.requests_served += 1
        session.consecutive_failures = 0
        if session.state is SessionState.COOLING:
            session.state = SessionState.HEALTHY

    def report_failure(self, session: LinkedInSession, kind: FailureKind, note: str = "") -> None:
        session.total_failures += 1
        session.consecutive_failures += 1
        if note:
            session.notes = (session.notes + [note])[-5:]

        if kind is FailureKind.EXPIRED:
            session.state = SessionState.DEAD
            return
        if kind is FailureKind.CHALLENGE:
            self._cool(session, COOLDOWN_CHALLENGE)
            return
        if kind is FailureKind.RATE_LIMIT:
            self._cool(session, COOLDOWN_RATE_LIMIT)
            return
        if session.consecutive_failures >= FAILURES_BEFORE_COOLDOWN:
            self._cool(session, COOLDOWN_TRANSIENT)

    @staticmethod
    def _cool(session: LinkedInSession, seconds: int) -> None:
        session.state = SessionState.COOLING
        session.cooldown_until = time.time() + seconds

    def status(self) -> list[dict]:
        return [
            {
                "label": s.label,
                "state": s.state.value,
                "requests_served": s.requests_served,
                "failures": s.total_failures,
                "cooldown_seconds_remaining": s.cooldown_remaining,
                "last_used": s.last_used,
                "cookie": s.redacted(),
                "recent_notes": s.notes,
            }
            for s in self._sessions
        ]
