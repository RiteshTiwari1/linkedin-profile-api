#!/usr/bin/env python3
"""Record one real LinkedIn payload to fixtures/raw/ for offline development.

This is the script that makes the rest of the project safe to build. The parser
needs dozens of iterations; running those against live LinkedIn would spend
hundreds of profile views and get the account blocked. So capture a handful of
real payloads once, then develop entirely offline against them.

    python scripts/record_fixture.py https://www.linkedin.com/in/some-person
    python scripts/record_fixture.py some-person --contact-info

Recorded fixtures contain real personal data and are gitignored. Only files
named `synthetic_*.json` are committed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.errors import ApiError  # noqa: E402
from app.fixtures import FixtureStore  # noqa: E402
from app.linkedin.client import VoyagerClient  # noqa: E402
from app.linkedin.session_pool import SessionPool  # noqa: E402
from app.ratelimit import RateLimiter  # noqa: E402
from app.url import extract_public_identifier  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="LinkedIn profile URL or vanity name")
    parser.add_argument(
        "--strategy",
        choices=["auto", "profile_view", "dash"],
        default="auto",
        help="Which endpoint to record from (default: auto -- try profileView, then dash).",
    )
    parser.add_argument("--contact-info", action="store_true", help="Also record contact info.")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.sessions:
        print(
            "error: LINKEDIN_SESSIONS is not set. Copy .env.example to .env first.",
            file=sys.stderr,
        )
        return 2

    public_id = extract_public_identifier(args.url)
    pool = SessionPool(settings.sessions)
    limiter = RateLimiter(
        per_hour=settings.max_profiles_per_hour,
        per_day=settings.max_profiles_per_day,
        min_delay=settings.min_delay_seconds,
        max_delay=settings.max_delay_seconds,
    )

    async with VoyagerClient(pool, limiter, settings) as client:
        available = {
            "profile_view": client.fetch_profile_view,
            "dash": client.fetch_dash_profile,
        }
        order = (
            list(available.items())
            if args.strategy == "auto"
            else [(args.strategy, available[args.strategy])]
        )

        payload = None
        used = None
        for name, fetch in order:
            print(f"-> trying {name} ...", end=" ", flush=True)
            try:
                payload = await fetch(public_id)
            except ApiError as exc:
                print(f"{exc.code}: {exc.message}")
                continue
            print("ok")
            used = name
            break

        if payload is None or used is None:
            print("error: no strategy returned a payload.", file=sys.stderr)
            return 1

        contact = None
        if args.contact_info:
            print("-> fetching contact info ...", end=" ", flush=True)
            contact = await client.fetch_contact_info(public_id)
            print("ok" if contact else "unavailable (not a 1st-degree connection)")

    store = FixtureStore(settings.fixtures_dir)
    path = store.save(public_id, used, payload, contact)
    size_kb = path.stat().st_size / 1024
    entities = len(payload.get("included") or [])
    print(f"\nsaved {path} ({size_kb:.1f} KB, {entities} included entities, strategy={used})")
    print("This file contains real personal data and is gitignored.")

    # Immediate feedback: does the parser actually understand what we captured?
    from app.linkedin.normalize import resolve
    from app.parsers import dash as dash_parser
    from app.parsers import profile_view as pv

    profile = (
        pv.parse(resolve(payload), public_id)
        if used == "profile_view"
        else dash_parser.parse(payload, public_id)
    )
    filled = {
        name: len(value) if isinstance(value, list) else bool(value)
        for name, value in profile.model_dump().items()
        if name not in ("public_identifier", "profile_url")
    }
    print("\nparsed result:")
    print(json.dumps({k: v for k, v in filled.items() if v}, indent=2, default=str)[:1600])
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
