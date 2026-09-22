"""Focused unit test for the endpoint-response cache's deep-copy-on-hit behavior.

Isolated from FastAPI wiring (no app import, no TestClient): we exercise only
the ``cached_response`` decorator in app/cache.py directly with a hand-rolled
async handler that returns a mutable dict/list payload, to prove two callers of
the same cached entry cannot corrupt each other's data by mutating what they
received.

Follows the repo's existing test style (plain sync test functions calling
``asyncio.run()``) since pytest-asyncio is not installed in this environment --
this keeps the test runnable with no new dependency, matching tests/test_*.py's
current pattern rather than adding a new async-test convention here.
"""

import asyncio

from app.cache import cached_response, reset_endpoint_cache


def _run():
    call_count = {"n": 0}

    @cached_response("deepcopy.test.json")
    async def fake_handler():
        call_count["n"] += 1
        return {
            "items": [1, 2],
            "meta": {"label": "original"},
        }

    first = asyncio.run(fake_handler())
    second = asyncio.run(fake_handler())
    assert call_count["n"] == 1, "second retrieval must be a cache hit"

    # Mutate the FIRST retrieved copy in place (both its list and dict fields).
    first["items"].append(99)
    first["meta"]["label"] = "MUTATED"
    first["unexpected_new_key"] = 42

    assert second == {
        "items": [1, 2],
        "meta": {"label": "original"},
    }, f"second retrieval was corrupted by mutating the first: {second}"


def test_cached_response_hit_returns_independent_deepcopy():
    reset_endpoint_cache()
    try:
        _run()
    finally:
        reset_endpoint_cache()
