"""Principal, the authenticators, and the env configuration."""

import hmac

import pytest
from starlette.requests import Request

from split_video.editor.auth import (
    TOKEN_ENV,
    LocalAuth,
    TokenAuth,
    authenticator_from_env,
    bearer_token,
)
from split_video.identity import LOCAL_PRINCIPAL, Principal, can_see_job
from split_video.store_provider import SharedStoreProvider, StoreProvider

FAKE = "fake-token-for-tests-0000"


def _request(authorization=None):
    headers = [] if authorization is None else [(b"authorization", authorization.encode())]
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers})


def test_local_principal_is_the_default_single_user():
    assert LOCAL_PRINCIPAL == Principal(id="local", name="Local user", kind="local")
    with pytest.raises(AttributeError):
        LOCAL_PRINCIPAL.id = "other"  # frozen


def test_job_visibility_rules():
    token = Principal(id="token", kind="token")
    assert can_see_job("local", LOCAL_PRINCIPAL) and not can_see_job("local", token)
    assert can_see_job("token", token) and not can_see_job("token", LOCAL_PRINCIPAL)
    assert can_see_job(None, LOCAL_PRINCIPAL) and not can_see_job(None, token)  # ownerless: the local user's


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, None),
        ("", None),
        (f"Bearer {FAKE}", FAKE),
        (f"bearer   {FAKE}  ", FAKE),
        ("Bearer", None),
        ("Bearer   ", None),
        (f"Basic {FAKE}", None),
        (FAKE, None),
    ],
)
def test_bearer_token_parsing(header, expected):
    assert bearer_token(_request(header)) == expected


def test_token_auth_accepts_only_the_token_and_compares_in_constant_time(monkeypatch):
    calls = []
    real = hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(hmac, "compare_digest", spy)
    auth = TokenAuth.single(FAKE)
    assert auth.required
    principal = auth.authenticate(_request(f"Bearer {FAKE}"))
    assert principal == Principal(id="token", name="API token", kind="token")
    assert auth.authenticate(_request("Bearer wrong")) is None
    assert auth.authenticate(_request(None)) is None
    assert auth.authenticate(_request("Bearer héllo")) is None  # non-ASCII must not raise
    assert len(calls) == 3  # every presented token was compared with compare_digest


def test_token_auth_maps_several_named_tokens():
    a, b = Principal(id="a", kind="token"), Principal(id="b", kind="token")
    auth = TokenAuth({"tok-a": a, "tok-b": b})
    assert auth.authenticate(_request("Bearer tok-a")) == a
    assert auth.authenticate(_request("Bearer tok-b")) == b
    with pytest.raises(ValueError):
        TokenAuth({})
    with pytest.raises(ValueError):
        TokenAuth({"": a})


def test_local_auth_never_rejects_and_ignores_headers():
    assert not LocalAuth.required
    assert LocalAuth().authenticate(_request(None)) is LOCAL_PRINCIPAL
    assert LocalAuth().authenticate(_request("Bearer anything")) is LOCAL_PRINCIPAL


@pytest.mark.parametrize("value", [None, "", "   ", "\n"])
def test_unset_or_blank_env_is_local_mode(value):
    env = {} if value is None else {TOKEN_ENV: value}
    assert isinstance(authenticator_from_env(env), LocalAuth)


def test_env_token_is_stripped():
    auth = authenticator_from_env({TOKEN_ENV: f"  {FAKE}\n"})
    assert isinstance(auth, TokenAuth)
    assert auth.authenticate(_request(f"Bearer {FAKE}")) is not None


def test_env_is_read_from_the_process_environment_by_default(monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, FAKE)
    assert authenticator_from_env().required
    monkeypatch.delenv(TOKEN_ENV)
    assert not authenticator_from_env().required


def test_shared_provider_gives_everyone_the_same_stores():
    from split_video.media_store import InMemoryMediaStore
    from split_video.store import InMemoryProjectStore

    store, media = InMemoryProjectStore(), InMemoryMediaStore()
    provider = SharedStoreProvider(store, media)
    assert isinstance(provider, StoreProvider)
    other = Principal(id="x", kind="token")
    assert provider.project_store(LOCAL_PRINCIPAL) is provider.project_store(other) is store
    assert provider.media_store(LOCAL_PRINCIPAL) is provider.media_store(other) is media
