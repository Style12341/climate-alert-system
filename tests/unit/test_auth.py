from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest

from climate_alerts.agrobot.tokens import issue_token
from climate_alerts.auth import MAX_BIGINT, InvalidCredentials, verify_token
from climate_alerts.config import Settings

# 64 characters: PyJWT warns on HMAC keys shorter than the hash, and the
# suite turns warnings into errors, so the HS512 case needs a key this long.
SECRET = "s" * 64
OTHER_SECRET = "o" * 64

s = Settings(
    _env_file=None,
    database_url="postgresql+asyncpg://unused",
    jwt_secret=SECRET,
)


def in_future() -> datetime:
    return datetime.now(UTC) + timedelta(minutes=5)


def encode(
    claims: dict[str, Any], secret: str = SECRET, algorithm: str = "HS256"
) -> str:
    return jwt.encode(claims, secret, algorithm=algorithm)


def with_tampered_signature(token: str) -> str:
    """Change the signature's first character.

    The last character of a base64url signature can carry unused bits, so
    altering it may leave the decoded signature identical.
    """
    head, payload, signature = token.split(".")
    replacement = "A" if signature[0] != "A" else "B"
    return f"{head}.{payload}.{replacement}{signature[1:]}"


def test_valid_token_returns_user_id() -> None:
    assert verify_token(issue_token(7, s, timedelta(minutes=5)), s) == 7


def test_largest_bigint_is_accepted() -> None:
    token = encode({"sub": str(MAX_BIGINT), "exp": in_future()})

    assert verify_token(token, s) == MAX_BIGINT


@pytest.mark.parametrize(
    "build_token",
    [
        pytest.param(
            lambda: encode(
                {"sub": "7", "exp": datetime.now(UTC) - timedelta(minutes=1)}
            ),
            id="expired",
        ),
        pytest.param(lambda: encode({"sub": "7"}), id="no_exp"),
        pytest.param(lambda: encode({"exp": in_future()}), id="no_sub"),
        pytest.param(
            lambda: encode({"sub": "abc", "exp": in_future()}), id="sub_not_a_number"
        ),
        pytest.param(lambda: encode({"sub": "0", "exp": in_future()}), id="sub_zero"),
        pytest.param(
            lambda: encode({"sub": "-1", "exp": in_future()}), id="sub_negative"
        ),
        pytest.param(
            lambda: encode({"sub": "99999999999999999999", "exp": in_future()}),
            id="sub_above_bigint",
        ),
        pytest.param(
            lambda: encode({"sub": str(MAX_BIGINT + 1), "exp": in_future()}),
            id="sub_just_above_bigint",
        ),
        pytest.param(
            lambda: encode({"sub": " 7 ", "exp": in_future()}), id="sub_with_spaces"
        ),
        pytest.param(
            lambda: encode({"sub": "1_000", "exp": in_future()}),
            id="sub_with_underscore",
        ),
        pytest.param(
            lambda: encode({"sub": "\u0667", "exp": in_future()}),
            id="sub_with_non_ascii_digit",
        ),
        pytest.param(
            lambda: encode({"sub": "7\n", "exp": in_future()}),
            id="sub_with_newline",
        ),
        pytest.param(
            lambda: encode({"sub": "07", "exp": in_future()}), id="sub_leading_zero"
        ),
        pytest.param(
            lambda: encode({"sub": 7, "exp": in_future()}), id="sub_not_a_string"
        ),
        pytest.param(
            lambda: encode({"sub": "1" * 5000, "exp": in_future()}), id="sub_too_long"
        ),
        pytest.param(
            lambda: with_tampered_signature(encode({"sub": "7", "exp": in_future()})),
            id="tampered_signature",
        ),
        pytest.param(
            lambda: encode({"sub": "7", "exp": in_future()}, secret=OTHER_SECRET),
            id="wrong_secret",
        ),
        pytest.param(
            lambda: encode({"sub": "7", "exp": in_future()}, algorithm="HS512"),
            id="signed_with_hs512",
        ),
    ],
)
def test_rejected_tokens(build_token: Any) -> None:
    with pytest.raises(InvalidCredentials):
        verify_token(build_token(), s)
