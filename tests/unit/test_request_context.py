"""Unit tests for request_context.py (ContextVar wrapper)."""

import asyncio
import pytest

from request_context import current_user_id


@pytest.mark.unit
def test_default_is_none():
    # Reset via .set to avoid contamination from concurrent tests
    token = current_user_id.set(None)
    try:
        assert current_user_id.get() is None
    finally:
        current_user_id.reset(token)


@pytest.mark.unit
@pytest.mark.parametrize("uid", ["user1", "user-abc", "uuid-123", "12345"])
def test_set_get_roundtrip(uid):
    token = current_user_id.set(uid)
    try:
        assert current_user_id.get() == uid
    finally:
        current_user_id.reset(token)


@pytest.mark.unit
def test_reset_restores_previous():
    token1 = current_user_id.set("a")
    try:
        token2 = current_user_id.set("b")
        assert current_user_id.get() == "b"
        current_user_id.reset(token2)
        assert current_user_id.get() == "a"
    finally:
        current_user_id.reset(token1)


@pytest.mark.unit
@pytest.mark.parametrize("uid", ["a", "b", "c", "d"])
def test_set_returns_distinct_token(uid):
    token = current_user_id.set(uid)
    try:
        assert token is not None
    finally:
        current_user_id.reset(token)


@pytest.mark.unit
def test_isolation_between_async_contexts():
    async def task(uid):
        token = current_user_id.set(uid)
        try:
            await asyncio.sleep(0)
            return current_user_id.get()
        finally:
            current_user_id.reset(token)

    async def main():
        return await asyncio.gather(task("u1"), task("u2"), task("u3"))

    out = asyncio.run(main())
    assert out == ["u1", "u2", "u3"]
