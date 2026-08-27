"""Recorded LinkedIn payloads.

Two jobs, both important:

1. **Development without burning the account.** The parser is where nearly all
   the work and nearly all the iteration is. Debugging it against live
   LinkedIn would cost hundreds of profile views and get the account blocked
   long before the code was finished. So `scripts/record_fixture.py` captures a
   handful of real payloads once, and everything after that is offline.

2. **A demo that cannot fail.** With DEMO_MODE=true the service answers purely
   from fixtures. Useful locally, and a safety net if LinkedIn has locked the
   account on the day someone is evaluating the deployment.

A fixture is the *raw* upstream payload plus the strategy that produced it, so
replaying it exercises the real resolver and parser -- not a hand-written happy
path that quietly diverges from reality.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class Fixture:
    __slots__ = ("contact_info", "path", "payload", "public_identifier", "strategy")

    def __init__(self, data: dict[str, Any], path: Path) -> None:
        self.public_identifier: str = data["public_identifier"]
        self.strategy: str = data.get("strategy", "profile_view")
        self.payload: dict[str, Any] = data["payload"]
        self.contact_info: dict[str, Any] | None = data.get("contact_info")
        self.path = path


class FixtureStore:
    def __init__(self, directory: str) -> None:
        self.directory = Path(directory)
        self._index: dict[str, Fixture] = {}
        self.reload()

    def reload(self) -> int:
        self._index.clear()
        if not self.directory.is_dir():
            return 0
        for path in sorted(self.directory.glob("*.json")):
            try:
                data = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("skipping unreadable fixture %s: %s", path.name, exc)
                continue
            required = ("public_identifier", "payload")
            if not isinstance(data, dict) or any(k not in data for k in required):
                log.warning("skipping malformed fixture %s", path.name)
                continue
            fixture = Fixture(data, path)
            self._index[fixture.public_identifier.lower()] = fixture
        return len(self._index)

    def get(self, public_identifier: str) -> Fixture | None:
        hit = self._index.get(public_identifier.lower())
        if hit is None and self.directory.is_dir():
            # Cheap: a fixture recorded after startup should be visible.
            self.reload()
            hit = self._index.get(public_identifier.lower())
        return hit

    def available(self) -> list[str]:
        return sorted(self._index)

    def save(
        self,
        public_identifier: str,
        strategy: str,
        payload: dict[str, Any],
        contact_info: dict[str, Any] | None = None,
    ) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{public_identifier}.json"
        path.write_text(
            json.dumps(
                {
                    "public_identifier": public_identifier,
                    "strategy": strategy,
                    "payload": payload,
                    "contact_info": contact_info,
                },
                indent=2,
            )
        )
        self.reload()
        return path
