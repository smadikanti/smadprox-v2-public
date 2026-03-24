"""
Clerk JWT Validation for NoHuman.

Validates Clerk-issued JWTs by fetching the JWKS from Clerk's API.
Falls back to unverified decode in dev mode.
Also handles long-lived desktop tokens for the Electron app.
"""

import hashlib
import logging
import time
import uuid
from typing import Optional

import jwt
from jwt import PyJWKClient

from app.config import settings

DESKTOP_TOKEN_EXPIRY_DAYS = 90

logger = logging.getLogger("nohuman.clerk")

# Cache for JWKS
_jwks_client: Optional[PyJWKClient] = None
_jwks_last_fetch: float = 0
_JWKS_CACHE_SECONDS = 3600  # Re-fetch JWKS every hour


def _get_jwks_client() -> Optional[PyJWKClient]:
    """Get or create the JWKS client for Clerk JWT verification."""
    global _jwks_client, _jwks_last_fetch

    clerk_issuer = getattr(settings, "CLERK_ISSUER_URL", "") or ""

    if not clerk_issuer:
        # Try to derive from publishable key or use Clerk's default
        clerk_pk = getattr(settings, "CLERK_PUBLISHABLE_KEY", "") or ""
        if clerk_pk:
            # In dev mode, Clerk issuer is usually the Clerk frontend API
            # This is a best-effort fallback
            pass
        return None

    now = time.time()
    if _jwks_client is None or (now - _jwks_last_fetch) > _JWKS_CACHE_SECONDS:
        jwks_url = f"{clerk_issuer}/.well-known/jwks.json"
        _jwks_client = PyJWKClient(jwks_url)
        _jwks_last_fetch = now
        logger.info(f"JWKS client initialized from {jwks_url}")

    return _jwks_client


async def verify_clerk_token(token: str) -> Optional[dict]:
    """
    Verify a Clerk JWT and return the decoded payload.
    
    Returns dict with at least 'sub' (Clerk user ID) on success.
    Returns None on failure.
    """
    if not token:
        return None

    # Try verified decode first
    jwks_client = _get_jwks_client()
    clerk_issuer = getattr(settings, "CLERK_ISSUER_URL", "") or ""

    if jwks_client and clerk_issuer:
        try:
            signing_key = jwks_client.get_signing_key_from_jwt(token)
            decoded = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                issuer=clerk_issuer,
                options={"verify_aud": False},
            )
            return decoded
        except Exception as e:
            logger.warning(f"Clerk JWT verification failed: {e}")
            # Fall through to dev fallback instead of returning None

    # Dev mode fallback: decode without verification
    # ONLY allowed when ENV != production
    if settings.ENV.lower() in ("production", "prod"):
        logger.error("Clerk JWKS verification failed in production — rejecting token")
        return None

    try:
        decoded = jwt.decode(token, options={"verify_signature": False})
        if decoded.get("sub"):
            logger.warning(
                f"DEV ONLY: unverified Clerk JWT for user {decoded['sub']}. "
                "This path is disabled in production."
            )
            return decoded
        return None
    except Exception as e:
        logger.warning(f"Clerk JWT decode failed: {e}")
        return None


async def get_clerk_user_id(token: str) -> Optional[str]:
    """Extract the Clerk user ID from a JWT token."""
    decoded = await verify_clerk_token(token)
    if decoded:
        return decoded.get("sub")
    return None


async def get_user_profile_from_clerk_token(token: str) -> Optional[dict]:
    """
    Verify Clerk token, look up the user profile in Supabase,
    and return the profile dict.
    """
    clerk_user_id = await get_clerk_user_id(token)
    if not clerk_user_id:
        return None

    # Import here to avoid circular imports
    from app.supabase_client import get_profile_by_clerk_id
    return await get_profile_by_clerk_id(clerk_user_id)


def _desktop_signing_key() -> str:
    """Derive a stable signing key for desktop tokens from an existing secret."""
    base = settings.SUPABASE_SERVICE_ROLE_KEY or "nohuman-desktop-fallback"
    return hashlib.sha256(f"nohuman-desktop:{base}".encode()).hexdigest()


def create_desktop_token(clerk_user_id: str) -> str:
    """Issue a long-lived HS256 JWT for desktop apps."""
    payload = {
        "sub": clerk_user_id,
        "type": "desktop",
        "iat": int(time.time()),
        "exp": int(time.time()) + 86400 * DESKTOP_TOKEN_EXPIRY_DAYS,
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, _desktop_signing_key(), algorithm="HS256")


def verify_desktop_token(token: str) -> Optional[dict]:
    """Verify a desktop-issued HS256 JWT. Returns payload or None."""
    try:
        return jwt.decode(token, _desktop_signing_key(), algorithms=["HS256"])
    except Exception:
        return None


async def get_user_profile_from_any_token(token: str) -> Optional[dict]:
    """
    Try Clerk JWT first, fall back to desktop token.
    Returns the Supabase profile dict or None.
    """
    profile = await get_user_profile_from_clerk_token(token)
    if profile:
        return profile

    desktop_payload = verify_desktop_token(token)
    if desktop_payload and desktop_payload.get("sub"):
        from app.supabase_client import get_profile_by_clerk_id
        return await get_profile_by_clerk_id(desktop_payload["sub"])

    return None
