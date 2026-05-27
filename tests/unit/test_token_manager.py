"""Exhaustive unit tests for token_manager.py.

TokenManager is a small file-backed token cache. All disk I/O goes through
tmp_path; network calls are mocked.
"""

import json
import os
import time
from unittest.mock import MagicMock, patch

import pytest

from token_manager import TokenManager


@pytest.fixture
def tm(tmp_path):
    return TokenManager(storage_path=str(tmp_path))


# ── Constructor ──────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_constructor_creates_storage_dir(tmp_path):
    target = tmp_path / "newdir"
    assert not target.exists()
    TokenManager(storage_path=str(target))
    assert target.exists()
    assert target.is_dir()

@pytest.mark.unit
def test_constructor_idempotent(tmp_path):
    TokenManager(storage_path=str(tmp_path))
    TokenManager(storage_path=str(tmp_path))  # no error second time


# ── _file_path ───────────────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("provider", ["gmail", "outlook", "facebook", "slack", "github"])
def test_file_path_uses_provider_name(tm, provider):
    path = tm._file_path(provider)
    assert path.endswith(f"{provider}.json")


# ── save + get round-trip ────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("provider", ["gmail", "outlook", "facebook"])
def test_save_then_get_roundtrip(tm, provider):
    token = {"access_token": "abc", "expires_at": 9999999999, "refresh_token": "r"}
    tm.save(provider, token)
    got = tm.get(provider)
    assert got == token

@pytest.mark.unit
def test_get_returns_none_when_missing(tm):
    assert tm.get("never-saved-provider") is None

@pytest.mark.unit
@pytest.mark.parametrize("provider", ["gmail", "outlook", "facebook"])
def test_save_overwrites_existing(tm, provider):
    tm.save(provider, {"v": 1})
    tm.save(provider, {"v": 2})
    assert tm.get(provider) == {"v": 2}


# ── delete ───────────────────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("provider", ["gmail", "outlook"])
def test_delete_removes_token(tm, provider):
    tm.save(provider, {"x": 1})
    assert tm.get(provider) is not None
    tm.delete(provider)
    assert tm.get(provider) is None

@pytest.mark.unit
def test_delete_missing_provider_no_error(tm):
    tm.delete("never-existed")  # must not raise


# ── is_expired ───────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_is_expired_returns_true_when_no_token(tm):
    assert tm.is_expired("gmail") is True

@pytest.mark.unit
def test_is_expired_returns_true_when_expired(tm):
    tm.save("gmail", {"expires_at": time.time() - 100})
    assert tm.is_expired("gmail") is True

@pytest.mark.unit
def test_is_expired_returns_false_when_valid(tm):
    tm.save("gmail", {"expires_at": time.time() + 10000})
    assert tm.is_expired("gmail") is False

@pytest.mark.unit
@pytest.mark.parametrize("offset", [-100, -10, -1, 0, 1])
def test_is_expired_when_at_or_before_now(tm, offset):
    tm.save("x", {"expires_at": time.time() + offset})
    if offset > 0:
        assert tm.is_expired("x") is False
    else:
        assert tm.is_expired("x") is True

@pytest.mark.unit
def test_is_expired_treats_missing_field_as_expired(tm):
    tm.save("x", {})
    assert tm.is_expired("x") is True


# ── refresh dispatcher ──────────────────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("provider", ["facebook", "outlook"])
def test_refresh_unimplemented_providers(tm, provider):
    tm.save(provider, {"refresh_token": "r"})
    with pytest.raises(NotImplementedError):
        tm.refresh(provider)

@pytest.mark.unit
def test_refresh_unknown_provider(tm):
    with pytest.raises(NotImplementedError):
        tm.refresh("slack")

@pytest.mark.unit
def test_refresh_gmail_missing_token(tm):
    with pytest.raises(ValueError, match="No stored token"):
        tm.refresh("gmail")

@pytest.mark.unit
def test_refresh_gmail_missing_required_fields(tm):
    tm.save("gmail", {"refresh_token": "r"})  # missing client_id/secret
    with pytest.raises(ValueError, match="Missing required"):
        tm.refresh("gmail")


# ── _refresh_gmail HTTP behavior ─────────────────────────────────────────────

@pytest.mark.unit
def test_refresh_gmail_success(tm):
    tm.save("gmail", {
        "refresh_token": "rt", "client_id": "ci",
        "client_secret": "cs", "token_uri": "https://x.test/token",
    })
    with patch("token_manager.requests.post") as mock_post:
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"access_token": "new", "expires_in": 3600},
        )
        new = tm.refresh("gmail")
    assert new["access_token"] == "new"
    assert new["refresh_token"] == "rt"
    assert new["expires_at"] > time.time()

@pytest.mark.unit
def test_refresh_gmail_failure_status(tm):
    tm.save("gmail", {
        "refresh_token": "rt", "client_id": "ci", "client_secret": "cs",
    })
    with patch("token_manager.requests.post") as mock_post:
        mock_post.return_value = MagicMock(status_code=401, text="invalid_grant")
        with pytest.raises(Exception, match="Failed to refresh"):
            tm.refresh("gmail")

@pytest.mark.unit
def test_refresh_gmail_uses_default_token_uri(tm):
    tm.save("gmail", {
        "refresh_token": "rt", "client_id": "ci", "client_secret": "cs",
    })
    with patch("token_manager.requests.post") as mock_post:
        mock_post.return_value = MagicMock(
            status_code=200, json=lambda: {"access_token": "n", "expires_in": 1},
        )
        tm.refresh("gmail")
    assert mock_post.call_args[0][0] == "https://oauth2.googleapis.com/token"

@pytest.mark.unit
@pytest.mark.parametrize("expires_in", [60, 600, 3600, 86400])
def test_refresh_gmail_sets_expires_at_relative(tm, expires_in):
    tm.save("gmail", {
        "refresh_token": "rt", "client_id": "ci", "client_secret": "cs",
    })
    with patch("token_manager.requests.post") as mock_post:
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"access_token": "n", "expires_in": expires_in},
        )
        before = time.time()
        new = tm.refresh("gmail")
        after = time.time()
    assert before + expires_in <= new["expires_at"] <= after + expires_in + 0.5


# ── parametrized: many token shapes ──────────────────────────────────────────

TOKEN_SHAPES = [
    {"access_token": "a", "expires_at": 99999999999},
    {"access_token": "a", "refresh_token": "r", "expires_at": 99999999999},
    {"access_token": "x", "scope": "s", "expires_at": 99999999999, "token_type": "Bearer"},
    {"access_token": "", "expires_at": 0},
    {},
]

@pytest.mark.unit
@pytest.mark.parametrize("token", TOKEN_SHAPES)
def test_save_get_preserves_arbitrary_shape(tm, token):
    tm.save("any", token)
    assert tm.get("any") == token
