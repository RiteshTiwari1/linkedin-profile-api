"""The URN resolver -- the piece most likely to break silently."""

from app.linkedin.normalize import entities_of_type, find_urn, first_of_type, resolve


def test_dereferences_star_keys():
    payload = {
        "data": {"*profile": "urn:li:p:1"},
        "included": [{"entityUrn": "urn:li:p:1", "$type": "x.Profile", "firstName": "Ada"}],
    }
    assert resolve(payload)["profile"]["firstName"] == "Ada"


def test_dereferences_lists():
    payload = {
        "data": {"*elements": ["urn:li:a:1", "urn:li:a:2"]},
        "included": [
            {"entityUrn": "urn:li:a:1", "name": "one"},
            {"entityUrn": "urn:li:a:2", "name": "two"},
        ],
    }
    assert [e["name"] for e in resolve(payload)["elements"]] == ["one", "two"]


def test_cuts_reference_cycles_without_recursing_forever():
    payload = {
        "data": {"*a": "urn:a"},
        "included": [
            {"entityUrn": "urn:a", "*b": "urn:b"},
            {"entityUrn": "urn:b", "*a": "urn:a"},
        ],
    }
    result = resolve(payload)
    assert result["a"]["b"]["a"]["$circular"] is True


def test_keeps_dangling_references_as_urns():
    """LinkedIn omits entities you may not see. That is data, not an error."""
    payload = {"data": {"*hidden": "urn:li:fs_profile:WITHHELD"}, "included": []}
    assert resolve(payload)["hidden"] == "urn:li:fs_profile:WITHHELD"


def test_survives_garbage_input():
    assert resolve({}) == {}
    assert resolve({"included": [None, 3, "x"], "data": {"a": 1}}) == {"a": 1}
    assert resolve({"data": None}) is None


def test_depth_limit_terminates_on_deep_chains():
    included = [{"entityUrn": f"urn:{i}", "*next": f"urn:{i + 1}"} for i in range(200)]
    payload = {"data": {"*head": "urn:0"}, "included": included}
    resolve(payload)  # must return, not blow the stack


def test_type_directed_lookup(synthetic_payload):
    positions = entities_of_type(synthetic_payload, ".profile.Position")
    assert len(positions) == 3
    assert first_of_type(synthetic_payload, ".profile.Profile")["firstName"] == "Priya"


def test_find_urn(synthetic_payload):
    assert find_urn(synthetic_payload, "urn:li:fs_profile:").startswith("urn:li:fs_profile:")
    assert find_urn(synthetic_payload, "urn:li:nonexistent:") is None
