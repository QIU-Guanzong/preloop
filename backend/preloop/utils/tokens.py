"""Token generation and validation for Preloop."""

import hashlib
import os
import secrets
from datetime import UTC, datetime, timedelta

import jwt
from jwt import PyJWTError

# Configuration
from preloop.config import settings

SECRET_KEY: str = settings.security.secret_key
ALGORITHM = "HS256"
EMAIL_TOKEN_EXPIRE_MINUTES = int(
    os.getenv("EMAIL_TOKEN_EXPIRE_MINUTES", "1440")
)  # 24 hours
PASSWORD_RESET_TOKEN_EXPIRE_MINUTES = int(
    os.getenv("PASSWORD_RESET_TOKEN_EXPIRE_MINUTES", "30")
)


class TokenError(Exception):
    """Raised when token validation fails."""

    pass


def create_email_verification_token(email: str) -> str:
    """Create an email verification token.

    Args:
        email: The email address to verify.

    Returns:
        A JWT token.
    """
    expire = datetime.now(UTC) + timedelta(minutes=EMAIL_TOKEN_EXPIRE_MINUTES)
    to_encode = {"sub": email, "exp": expire, "type": "email_verification"}
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def create_password_reset_token(email: str) -> str:
    """Create a password reset token.

    Args:
        email: The email address of the user.

    Returns:
        A JWT token.
    """
    expire = datetime.now(UTC) + timedelta(minutes=PASSWORD_RESET_TOKEN_EXPIRE_MINUTES)
    to_encode = {"sub": email, "exp": expire, "type": "password_reset"}
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def verify_token(token: str, token_type: str) -> str:
    """Verify a token and return the email address.

    Args:
        token: The JWT token to verify.
        token_type: The expected token type ("email_verification" or "password_reset").

    Returns:
        The email address from the token.

    Raises:
        TokenError: If the token is invalid, expired, or has the wrong type.
    """
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        token_purpose: str = payload.get("type")

        if not email:
            raise TokenError("Invalid token: Missing email")

        if token_purpose != token_type:
            raise TokenError(
                f"Invalid token: Expected {token_type} token, got {token_purpose}"
            )

        return email
    except PyJWTError:
        raise TokenError("Invalid or expired token")


def create_token(
    email: str,
    token_type: str,
    *,
    expires_delta: timedelta | None = None,
) -> str:
    """Create a JWT token with optional custom expiry.

    Args:
        email: The email address to encode.
        token_type: Token type (e.g. "onboarding", "email_verification").
        expires_delta: Time until expiry. Defaults to 24 hours.

    Returns:
        A JWT token.
    """
    expire = datetime.now(UTC) + (
        expires_delta or timedelta(minutes=EMAIL_TOKEN_EXPIRE_MINUTES)
    )
    to_encode = {"sub": email, "exp": expire, "type": token_type}
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def create_onboarding_token(email: str) -> str:
    """Creates a short-lived token for the onboarding process."""
    return create_token(email, "onboarding", expires_delta=timedelta(hours=1))


#: Type marker for the account-claim token minted after a completed checkout.
ONBOARDING_CLAIM_TOKEN_TYPE = "onboarding_claim"

#: How long the welcome link stays usable. Short on purpose: it is handed to
#: the browser that just completed checkout and is used seconds later. Anyone
#: who loses the link recovers through the ordinary password reset email, not
#: through a second claim.
ONBOARDING_CLAIM_TOKEN_EXPIRE_MINUTES = 60


def create_onboarding_claim_token(
    *,
    email: str,
    account_id: str,
    checkout_session_id: str,
) -> str:
    """Mint the single-use token that claims a checkout-created account.

    The account is created server side with a ``NEEDS_RESET`` placeholder
    password, so something has to prove that the caller who sets the real
    password is the person who paid. That proof is this token: it is signed
    with the instance secret, expires, carries a random ``jti`` so the server
    can consume it exactly once, and is bound to both the account it opens and
    the Stripe checkout session that created it. Knowing the address is not
    enough, and a token for one account cannot claim another.

    Args:
        email: The address on the completed checkout, also the claim subject.
        account_id: The account the token opens.
        checkout_session_id: The Stripe checkout session that created it,
            recorded so a claim can be traced back to the payment.

    Returns:
        A JWT to put in the welcome URL.
    """
    expire = datetime.now(UTC) + timedelta(
        minutes=ONBOARDING_CLAIM_TOKEN_EXPIRE_MINUTES
    )
    to_encode = {
        "sub": email,
        "exp": expire,
        "type": ONBOARDING_CLAIM_TOKEN_TYPE,
        "account_id": str(account_id),
        "checkout_session_id": str(checkout_session_id),
        "jti": secrets.token_urlsafe(32),
    }
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def hash_onboarding_claim_token(token: str) -> str:
    """Fingerprint a claim token for storage.

    The server stores this, never the token, so a database copy does not hand
    anyone a working claim link.

    Args:
        token: The JWT handed to the browser.

    Returns:
        Hex SHA-256 of the token.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_onboarding_claim_token(token: str) -> dict:
    """Verify a claim token's signature, expiry, type and bindings.

    Args:
        token: The JWT presented by the welcome page.

    Returns:
        A dict with ``email``, ``account_id`` and ``checkout_session_id``.

    Raises:
        TokenError: If the token is invalid, expired, of the wrong type, or
            missing either binding.
    """
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except PyJWTError:
        raise TokenError("Invalid or expired onboarding claim token")

    if payload.get("type") != ONBOARDING_CLAIM_TOKEN_TYPE:
        raise TokenError("Invalid onboarding claim token: wrong type")

    email = payload.get("sub")
    account_id = payload.get("account_id")
    checkout_session_id = payload.get("checkout_session_id")
    if not email or not account_id or not checkout_session_id:
        raise TokenError("Invalid onboarding claim token: missing binding")

    return {
        "email": email,
        "account_id": account_id,
        "checkout_session_id": checkout_session_id,
    }
