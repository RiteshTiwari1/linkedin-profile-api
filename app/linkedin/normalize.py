"""Resolve LinkedIn's normalized JSON into an ordinary nested structure.

This module is the heart of the project. Voyager does not hand you a profile
object. With `accept: application/vnd.linkedin.normalized+json+2.1` it hands
you a *normalized graph*, the wire format of a client-side entity cache:

    {
      "data": {
        "$type": "com.linkedin.voyager.identity.profile.ProfileView",
        "*profile": "urn:li:fs_profile:ACoAAA",
        "*positionView": "urn:li:fs_positionView:ACoAAA"
      },
      "included": [
        {"entityUrn": "urn:li:fs_profile:ACoAAA", "firstName": "Ada", ...},
        {"entityUrn": "urn:li:fs_positionView:ACoAAA", "*elements": [...]},
        {"entityUrn": "urn:li:fs_position:1", "title": "Engineer", ...}
      ]
    }

Two conventions carry all the meaning:

* `included` is a flat, deduplicated pool of entities, each keyed by
  `entityUrn`. The same company object referenced by six positions appears
  exactly once.
* A key prefixed with `*` is a **reference**, not a value. Its value is a URN
  string (or a list of them) that must be looked up in `included`.

So the job is: index `included` by URN, then walk the tree replacing every
`*key` with the entity it points at, under the un-prefixed key name.

Two things make this non-trivial in practice:

* **Cycles.** A position references a company, and a company's
  `*miniCompany` can reference back. Naive recursion blows the stack, so we
  track the URNs on the current path and cut the loop with a stub.
* **Dangling references.** LinkedIn omits entities you are not allowed to see.
  A `*profile` pointing at a URN that is not in `included` is normal, not an
  error, so we keep the raw URN as the value -- it is still useful information
  and the parsers treat "a bare string here" as "not visible".
"""

from __future__ import annotations

from typing import Any

MAX_DEPTH = 16
_RECURSION_STUB = "$circular"


def build_index(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Index the `included` pool by entityUrn."""
    index: dict[str, dict[str, Any]] = {}
    for entity in payload.get("included") or []:
        if not isinstance(entity, dict):
            continue
        urn = entity.get("entityUrn")
        if isinstance(urn, str):
            index[urn] = entity
        # Some dash entities key themselves differently.
        for alt in ("objectUrn", "trackingUrn"):
            alt_urn = entity.get(alt)
            if isinstance(alt_urn, str) and alt_urn not in index:
                index[alt_urn] = entity
    return index


def resolve(payload: dict[str, Any], *, max_depth: int = MAX_DEPTH) -> Any:
    """Dereference a normalized Voyager payload into plain nested data."""
    if not isinstance(payload, dict):
        return payload
    index = build_index(payload)
    root = payload.get("data", payload)
    return _walk(root, index, frozenset(), 0, max_depth)


def _walk(
    node: Any,
    index: dict[str, dict[str, Any]],
    active: frozenset[str],
    depth: int,
    max_depth: int,
) -> Any:
    if depth >= max_depth:
        return node if not isinstance(node, (dict, list)) else None

    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key.startswith("*"):
                out[key[1:]] = _deref(value, index, active, depth + 1, max_depth)
            else:
                out[key] = _walk(value, index, active, depth + 1, max_depth)
        return out

    if isinstance(node, list):
        return [_walk(item, index, active, depth + 1, max_depth) for item in node]

    return node


def _deref(
    ref: Any,
    index: dict[str, dict[str, Any]],
    active: frozenset[str],
    depth: int,
    max_depth: int,
) -> Any:
    if isinstance(ref, list):
        return [_deref(item, index, active, depth, max_depth) for item in ref]

    if not isinstance(ref, str):
        # Occasionally a `*key` holds an inline object rather than a URN.
        return _walk(ref, index, active, depth, max_depth)

    if ref in active:
        # Cut the cycle but keep the identity so callers can still join on it.
        return {"entityUrn": ref, _RECURSION_STUB: True}

    entity = index.get(ref)
    if entity is None:
        # Dangling reference: entity withheld or paginated away. The URN itself
        # is the most informative thing we can return.
        return ref

    return _walk(entity, index, active | {ref}, depth, max_depth)


# ---------------------------------------------------------------------------
# Type-directed access
#
# The tree shape of dash/GraphQL responses changes far more often than the
# `$type` strings of the entities inside them. So for anything newer than
# profileView we ignore the tree and pull entities straight out of `included`
# by type. Much more robust against LinkedIn reshuffling its UI.
# ---------------------------------------------------------------------------


def entities_of_type(payload: dict[str, Any], *type_suffixes: str) -> list[dict[str, Any]]:
    """Every `included` entity whose $type ends with one of the given suffixes."""
    found: list[dict[str, Any]] = []
    index = build_index(payload)
    for entity in payload.get("included") or []:
        if not isinstance(entity, dict):
            continue
        etype = entity.get("$type")
        if not isinstance(etype, str):
            continue
        if any(etype.endswith(suffix) for suffix in type_suffixes):
            found.append(_walk(entity, index, frozenset(), 0, MAX_DEPTH))
    return found


def first_of_type(payload: dict[str, Any], *type_suffixes: str) -> dict[str, Any] | None:
    matches = entities_of_type(payload, *type_suffixes)
    return matches[0] if matches else None


def find_urn(payload: dict[str, Any], prefix: str) -> str | None:
    """First URN in the payload matching a prefix, e.g. 'urn:li:fsd_profile:'.

    Used to turn a vanity name into the profile URN that GraphQL section
    queries require.
    """
    stack: list[Any] = [payload]
    seen = 0
    while stack and seen < 200_000:
        node = stack.pop()
        seen += 1
        if isinstance(node, str):
            if node.startswith(prefix):
                return node
        elif isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return None
