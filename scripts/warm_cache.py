#!/usr/bin/env python3
"""Pre-populate the cache before a demo or a deployment.

Rationale: the daily LinkedIn budget is around 100 profile views for the whole
service. If the first person to try the API triggers a live fetch, they wait
seconds and spend budget. If the cache is already warm for the profiles anyone
is likely to try, those requests are served in milliseconds and cost nothing.

    python scripts/warm_cache.py urls.txt
    python scripts/warm_cache.py --stdin < urls.txt

Runs strictly sequentially through the normal rate limiter, so it cannot itself
be the thing that gets the account blocked. Stops on the first hard block.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.errors import ApiError  # noqa: E402
from app.service import ProfileService  # noqa: E402

HALTING = {"RATE_LIMITED", "UPSTREAM_BLOCKED", "SESSION_EXPIRED", "NO_SESSIONS_CONFIGURED"}


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", nargs="?", help="File with one profile URL per line.")
    parser.add_argument("--stdin", action="store_true", help="Read URLs from stdin.")
    parser.add_argument("--depth", choices=["shallow", "full"], default="full")
    args = parser.parse_args()

    if args.stdin or not args.file:
        lines = sys.stdin.read().splitlines()
    else:
        lines = Path(args.file).read_text().splitlines()
    urls = [line.strip() for line in lines if line.strip() and not line.startswith("#")]
    if not urls:
        print("no URLs supplied", file=sys.stderr)
        return 2

    settings = get_settings()
    service = ProfileService(settings)
    await service.startup()

    ok = failed = skipped = 0
    started = time.perf_counter()
    try:
        for index, url in enumerate(urls, 1):
            prefix = f"[{index}/{len(urls)}] {url[:58]:58s}"
            try:
                response = await service.get_profile(url, depth=args.depth)
            except ApiError as exc:
                print(f"{prefix} {exc.code}")
                failed += 1
                if exc.code in HALTING:
                    skipped = len(urls) - index
                    print(f"\nhalting: {exc.message}")
                    break
                continue
            source = response.meta.source
            print(
                f"{prefix} {source:11s} {response.meta.upstream_requests} req  "
                f"{response.profile.full_name or '(no name)'}"
            )
            ok += 1
    finally:
        stats = service.cache.stats()
        await service.shutdown()

    elapsed = time.perf_counter() - started
    print(
        f"\nwarmed {ok}, failed {failed}, skipped {skipped} in {elapsed:.0f}s\n"
        f"cache now holds {stats['entries']} profiles ({stats['fresh_entries']} fresh)"
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
