#!/usr/bin/env python3
"""Pull the cookie header out of a "Copy as cURL" command and write it to .env.

Copying the cookie header by hand out of DevTools is easy to get wrong -- it is
several hundred characters, wraps in the panel, and a truncated paste fails in a
way that looks like a bad cookie rather than a bad copy. So instead:

    Chrome -> F12 -> Network -> reload -> right-click the first request
           -> Copy -> Copy as cURL

then run one of:

    python scripts/cookie_from_curl.py --clipboard      # macOS, reads pbpaste
    pbpaste | python scripts/cookie_from_curl.py
    python scripts/cookie_from_curl.py curl.txt

The cookie value is never printed -- only a summary of which cookies were found.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.linkedin.endpoints import DEVICE_COOKIES, parse_cookie_header  # noqa: E402

# Cookie *values* contain quotes of their own -- bcookie="v=2&...", lidc="b=OB01"
# -- so a regex that stops at the first quote truncates the header. Find where a
# flag's quoted argument starts, then scan to its matching unescaped close quote.
_FLAG_START = re.compile(
    r"""(?:^|\s)-{1,2}(?P<flag>H|header|b|cookie)\s+(?P<q>['"])""",
    re.IGNORECASE,
)


def _closing_quote(text: str, start: int, quote: str) -> int | None:
    """Index of the quote that closes the one opened before `start`."""
    index = start
    while index < len(text):
        char = text[index]
        if char == "\\":
            index += 2
            continue
        if char == quote:
            return index
        index += 1
    return None


def extract(text: str) -> str | None:
    """The cookie header value, from any form DevTools or a shell produces."""
    for match in _FLAG_START.finditer(text):
        quote = match.group("q")
        start = match.end()
        end = _closing_quote(text, start, quote)
        if end is None:
            continue
        raw = text[start:end]
        if quote == '"':
            # cmd.exe / PowerShell escape inner quotes.
            raw = raw.replace('\\"', '"')
        raw = raw.strip()

        if raw[:7].lower() == "cookie:":
            value = raw[7:].strip()
            if value:
                return value
        # `-b 'name=value; ...'` with no header prefix.
        elif match.group("flag").lower() in ("b", "cookie") and "=" in raw:
            return raw

    # Or the header value pasted on its own.
    stripped = text.strip()
    if stripped[:7].lower() == "cookie:":
        stripped = stripped[7:].strip()
    if "li_at=" in stripped:
        return stripped
    return None


def write_env(cookie: str, env_path: Path) -> None:
    line = f"LINKEDIN_COOKIE={cookie}"
    if not env_path.exists():
        env_path.write_text(line + "\n")
        env_path.chmod(0o600)
        return
    lines = env_path.read_text().splitlines()
    for index, existing in enumerate(lines):
        if existing.startswith("LINKEDIN_COOKIE="):
            lines[index] = line
            break
    else:
        lines.append(line)
    env_path.write_text("\n".join(lines) + "\n")
    env_path.chmod(0o600)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file", nargs="?", help="File containing the cURL command.")
    parser.add_argument("--clipboard", action="store_true", help="Read from the clipboard (macOS).")
    parser.add_argument("--env", default=str(ROOT / ".env"))
    args = parser.parse_args()

    if args.clipboard:
        try:
            text = subprocess.run(["pbpaste"], capture_output=True, text=True, check=True).stdout
        except (OSError, subprocess.CalledProcessError) as exc:
            print(f"error: could not read the clipboard ({exc}).", file=sys.stderr)
            return 2
    elif args.file:
        text = Path(args.file).read_text()
    else:
        text = sys.stdin.read()

    if not text.strip():
        print("error: nothing to read.", file=sys.stderr)
        return 2

    cookie = extract(text)
    if not cookie:
        print(
            "error: no cookie header found. Make sure you used "
            "'Copy as cURL' on a linkedin.com request while logged in.",
            file=sys.stderr,
        )
        return 1

    jar = parse_cookie_header(cookie)
    if "li_at" not in jar:
        print(
            "error: the cookie header has no li_at, so it is not an authenticated "
            "request. Reload linkedin.com/feed while logged in and copy the first "
            "document request.",
            file=sys.stderr,
        )
        return 1

    write_env(cookie, Path(args.env))

    print(f"wrote LINKEDIN_COOKIE to {args.env} ({len(jar)} cookies, value not shown)\n")
    print("  credential cookies")
    print(f"    li_at        {len(jar['li_at'])} chars  ok")
    jsid = jar.get("JSESSIONID")
    print(f"    JSESSIONID   {'present  ok' if jsid else 'MISSING -- CSRF calls will fail'}")
    print("\n  device cookies (these are what keep the session alive)")
    for name in DEVICE_COOKIES:
        print(f"    {name:12s} {'present' if name in jar else '-- absent'}")
    missing = [n for n in ("bcookie", "bscookie") if n not in jar]
    if missing:
        print(
            f"\n  warning: {', '.join(missing)} absent. LinkedIn tends to invalidate "
            f"sessions without its device-identity cookies."
        )
    else:
        print("\n  bcookie and bscookie both present -- this is what we need.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
