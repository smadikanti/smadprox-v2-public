"""
Supabase client helper for NoHuman.

Provides both a service-role client (for server-side operations)
and per-request user clients (for RLS-respecting operations).
Uses httpx for direct REST API calls (no SDK dependency).
"""

import httpx
import jwt
from typing import Optional

from app.config import settings

BASE_URL = settings.SUPABASE_URL
ANON_KEY = settings.SUPABASE_ANON_KEY
SERVICE_KEY = settings.SUPABASE_SERVICE_ROLE_KEY


def _headers(token: Optional[str] = None) -> dict:
    """Build request headers for Supabase REST API."""
    key = token or ANON_KEY
    return {
        "apikey": ANON_KEY,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def _rest_url(table: str) -> str:
    """Build PostgREST URL for a table."""
    return f"{BASE_URL}/rest/v1/{table}"


def _auth_url(path: str) -> str:
    """Build Supabase Auth URL."""
    return f"{BASE_URL}/auth/v1/{path}"


def decode_jwt(token: str) -> dict:
    """Decode a Supabase JWT (without full verification for speed)."""
    try:
        return jwt.decode(token, options={"verify_signature": False})
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

async def signup(email: str, password: str, name: str = "") -> dict:
    """Register a new user via Supabase Auth."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            _auth_url("signup"),
            headers={"apikey": ANON_KEY, "Content-Type": "application/json"},
            json={
                "email": email,
                "password": password,
                "data": {"name": name},
            },
        )
        resp.raise_for_status()
        return resp.json()


async def login(email: str, password: str) -> dict:
    """Login via email/password and return tokens."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            _auth_url("token?grant_type=password"),
            headers={"apikey": ANON_KEY, "Content-Type": "application/json"},
            json={"email": email, "password": password},
        )
        resp.raise_for_status()
        return resp.json()


async def get_user(access_token: str) -> dict:
    """Get the current user from their access token."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            _auth_url("user"),
            headers={
                "apikey": ANON_KEY,
                "Authorization": f"Bearer {access_token}",
            },
        )
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------

async def get_profile(token: str, user_id: str) -> Optional[dict]:
    """Get a user's profile."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{_rest_url('profiles')}?id=eq.{user_id}&select=*",
            headers=_headers(token),
        )
        resp.raise_for_status()
        data = resp.json()
        return data[0] if data else None


async def update_profile(token: str, user_id: str, updates: dict) -> dict:
    """Update a user's profile."""
    async with httpx.AsyncClient() as client:
        resp = await client.patch(
            f"{_rest_url('profiles')}?id=eq.{user_id}",
            headers=_headers(token),
            json=updates,
        )
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Contexts
# ---------------------------------------------------------------------------

async def list_contexts(token: str, user_id: str) -> list[dict]:
    """List all contexts for a user."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{_rest_url('contexts')}?user_id=eq.{user_id}&select=*&order=updated_at.desc",
            headers=_headers(token),
        )
        resp.raise_for_status()
        return resp.json()


async def get_context(token: str, context_id: int) -> Optional[dict]:
    """Get a single context by ID."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{_rest_url('contexts')}?id=eq.{context_id}&select=*",
            headers=_headers(token),
        )
        resp.raise_for_status()
        data = resp.json()
        return data[0] if data else None


async def create_context(token: str, user_id: str, data: dict) -> dict:
    """Create a new context."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            _rest_url("contexts"),
            headers=_headers(token),
            json={**data, "user_id": user_id},
        )
        resp.raise_for_status()
        result = resp.json()
        return result[0] if result else result


async def update_context(token: str, context_id: int, data: dict) -> dict:
    """Update a context."""
    async with httpx.AsyncClient() as client:
        resp = await client.patch(
            f"{_rest_url('contexts')}?id=eq.{context_id}",
            headers=_headers(token),
            json=data,
        )
        resp.raise_for_status()
        result = resp.json()
        return result[0] if result else result


async def delete_context(token: str, context_id: int) -> None:
    """Delete a context."""
    async with httpx.AsyncClient() as client:
        resp = await client.delete(
            f"{_rest_url('contexts')}?id=eq.{context_id}",
            headers={**_headers(token), "Prefer": ""},
        )
        resp.raise_for_status()


# ---------------------------------------------------------------------------
# Context Documents
# ---------------------------------------------------------------------------

async def list_documents(token: str, context_id: int) -> list[dict]:
    """List all documents in a context."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{_rest_url('context_documents')}?context_id=eq.{context_id}&select=*&order=created_at.asc",
            headers=_headers(token),
        )
        resp.raise_for_status()
        return resp.json()


async def create_document(token: str, context_id: int, data: dict) -> dict:
    """Create a new document in a context."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            _rest_url("context_documents"),
            headers=_headers(token),
            json={**data, "context_id": context_id},
        )
        resp.raise_for_status()
        result = resp.json()
        return result[0] if result else result


async def update_document(token: str, doc_id: int, data: dict) -> dict:
    """Update a document."""
    async with httpx.AsyncClient() as client:
        resp = await client.patch(
            f"{_rest_url('context_documents')}?id=eq.{doc_id}",
            headers=_headers(token),
            json=data,
        )
        resp.raise_for_status()
        result = resp.json()
        return result[0] if result else result


async def delete_document(token: str, doc_id: int) -> None:
    """Delete a document."""
    async with httpx.AsyncClient() as client:
        resp = await client.delete(
            f"{_rest_url('context_documents')}?id=eq.{doc_id}",
            headers={**_headers(token), "Prefer": ""},
        )
        resp.raise_for_status()


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

async def create_session(token: str, user_id: str, context_id: int) -> dict:
    """Create a new coaching session."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            _rest_url("sessions"),
            headers=_headers(token),
            json={
                "user_id": user_id,
                "context_id": context_id,
                "status": "waiting",
            },
        )
        resp.raise_for_status()
        result = resp.json()
        return result[0] if result else result


async def update_session(token: str, session_id: str, data: dict) -> dict:
    """Update a session's status."""
    async with httpx.AsyncClient() as client:
        resp = await client.patch(
            f"{_rest_url('sessions')}?id=eq.{session_id}",
            headers=_headers(token),
            json=data,
        )
        resp.raise_for_status()
        result = resp.json()
        return result[0] if result else result


async def get_session(token: str, session_id: str) -> Optional[dict]:
    """Get a session by ID."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{_rest_url('sessions')}?id=eq.{session_id}&select=*",
            headers=_headers(token),
        )
        resp.raise_for_status()
        data = resp.json()
        return data[0] if data else None


# ---------------------------------------------------------------------------
# Messages (server-side, uses service role key)
# ---------------------------------------------------------------------------

async def save_message(session_id: str, role: str, content: str) -> dict:
    """Save a message to the database (service-role, bypasses RLS)."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            _rest_url("messages"),
            headers={
                "apikey": ANON_KEY,
                "Authorization": f"Bearer {SERVICE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            },
            json={
                "session_id": session_id,
                "role": role,
                "content": content,
            },
        )
        resp.raise_for_status()
        result = resp.json()
        return result[0] if result else result


# ---------------------------------------------------------------------------
# Service-Role Headers (bypasses RLS)
# ---------------------------------------------------------------------------

def _service_headers() -> dict:
    """Build headers using the service role key (bypasses RLS)."""
    return {
        "apikey": ANON_KEY,
        "Authorization": f"Bearer {SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


# ---------------------------------------------------------------------------
# Profile by Clerk ID (service-role)
# ---------------------------------------------------------------------------

async def get_profile_by_clerk_id(clerk_user_id: str) -> Optional[dict]:
    """Get a user's profile by their Clerk user ID (service-role)."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{_rest_url('profiles')}?clerk_user_id=eq.{clerk_user_id}&select=*",
            headers=_service_headers(),
        )
        resp.raise_for_status()
        data = resp.json()
        return data[0] if data else None


# ---------------------------------------------------------------------------
# Credits (service-role)
# ---------------------------------------------------------------------------

async def get_credits(user_id: str) -> Optional[dict]:
    """Get a user's credit balance."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{_rest_url('credits')}?user_id=eq.{user_id}&select=*",
            headers=_service_headers(),
        )
        resp.raise_for_status()
        data = resp.json()
        return data[0] if data else None


async def add_credits(user_id: str, minutes: float) -> float:
    """Add credits to a user's balance. Returns the new balance."""
    async with httpx.AsyncClient() as client:
        existing = await get_credits(user_id)
        if existing:
            new_balance = existing["balance_minutes"] + minutes
            resp = await client.patch(
                f"{_rest_url('credits')}?user_id=eq.{user_id}",
                headers=_service_headers(),
                json={
                    "balance_minutes": new_balance,
                    "updated_at": "now()",
                },
            )
            resp.raise_for_status()
            return new_balance
        else:
            resp = await client.post(
                _rest_url("credits"),
                headers=_service_headers(),
                json={
                    "user_id": user_id,
                    "balance_minutes": minutes,
                },
            )
            resp.raise_for_status()
            return minutes


async def decrement_credits(user_id: str, minutes: float) -> Optional[dict]:
    """
    Decrement a user's credit balance by the given minutes.
    Uses RPC or direct update. Returns updated credits row.
    """
    async with httpx.AsyncClient() as client:
        # First get current balance
        credits = await get_credits(user_id)
        if not credits:
            return None

        new_balance = max(0, credits["balance_minutes"] - minutes)
        resp = await client.patch(
            f"{_rest_url('credits')}?user_id=eq.{user_id}",
            headers=_service_headers(),
            json={
                "balance_minutes": new_balance,
                "updated_at": "now()",
            },
        )
        resp.raise_for_status()
        result = resp.json()
        return result[0] if result else None


async def check_can_generate(user_id: str) -> dict:
    """
    Check whether a user can generate a suggestion.
    Returns {"allowed": bool, "reason": str, "source": "free_trial"|"credits"|"none"}.
    """
    credits = await get_credits(user_id)

    if not credits:
        # No credits row → create one with 3 free generations
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                _rest_url("credits"),
                headers=_service_headers(),
                json={
                    "user_id": user_id,
                    "balance_minutes": 0,
                    "free_generations_remaining": 3,
                },
            )
            resp.raise_for_status()
        return {"allowed": True, "reason": "Free trial (3 generations)", "source": "free_trial"}

    free_remaining = credits.get("free_generations_remaining", 0)
    balance = credits.get("balance_minutes", 0)

    if free_remaining > 0:
        return {"allowed": True, "reason": f"{free_remaining} free generations left", "source": "free_trial"}
    elif balance > 0:
        return {"allowed": True, "reason": f"{balance:.1f} minutes remaining", "source": "credits"}
    else:
        return {"allowed": False, "reason": "No credits or free generations remaining", "source": "none"}


async def use_free_generation(user_id: str) -> int:
    """
    Decrement the free_generations_remaining counter by 1.
    Returns the new remaining count.
    """
    credits = await get_credits(user_id)
    if not credits:
        return 0

    remaining = max(0, credits.get("free_generations_remaining", 0) - 1)
    async with httpx.AsyncClient() as client:
        resp = await client.patch(
            f"{_rest_url('credits')}?user_id=eq.{user_id}",
            headers=_service_headers(),
            json={
                "free_generations_remaining": remaining,
                "updated_at": "now()",
            },
        )
        resp.raise_for_status()
    return remaining


async def record_credit_usage(
    user_id: str, session_id: str, minutes_used: float
) -> dict:
    """Record credit usage for a session."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            _rest_url("credit_usage"),
            headers=_service_headers(),
            json={
                "user_id": user_id,
                "session_id": session_id,
                "minutes_used": minutes_used,
            },
        )
        resp.raise_for_status()
        result = resp.json()
        return result[0] if result else result


# ---------------------------------------------------------------------------
# Sessions (service-role for new SaaS endpoints)
# ---------------------------------------------------------------------------

async def create_session_v2(
    user_id: str,
    context_id: Optional[int],
    data: dict,
) -> dict:
    """Create a new session with additional SaaS fields (service-role)."""
    payload = {
        "user_id": user_id,
        "status": "waiting",
    }
    if context_id:
        payload["context_id"] = context_id
    # Add optional SaaS fields
    for field in [
        "company_name", "round_type", "visa_status",
        "recruiter_notes", "resume_text", "job_description",
    ]:
        if data.get(field):
            payload[field] = data[field]

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            _rest_url("sessions"),
            headers=_service_headers(),
            json=payload,
        )
        resp.raise_for_status()
        result = resp.json()
        return result[0] if result else result


async def list_user_sessions(user_id: str) -> list[dict]:
    """List all sessions for a user (service-role)."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{_rest_url('sessions')}?user_id=eq.{user_id}&select=*&order=created_at.desc",
            headers=_service_headers(),
        )
        resp.raise_for_status()
        return resp.json()


async def get_session_by_id(session_id: str) -> Optional[dict]:
    """Get a session by ID (service-role)."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{_rest_url('sessions')}?id=eq.{session_id}&select=*",
            headers=_service_headers(),
        )
        resp.raise_for_status()
        data = resp.json()
        return data[0] if data else None


async def update_session_v2(session_id: str, data: dict) -> dict:
    """Update a session (service-role)."""
    async with httpx.AsyncClient() as client:
        resp = await client.patch(
            f"{_rest_url('sessions')}?id=eq.{session_id}",
            headers=_service_headers(),
            json=data,
        )
        resp.raise_for_status()
        result = resp.json()
        return result[0] if result else result


async def get_session_messages(session_id: str) -> list[dict]:
    """Get all messages/events for a session, ordered chronologically."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{_rest_url('messages')}?session_id=eq.{session_id}&select=*&order=created_at.asc",
            headers=_service_headers(),
        )
        resp.raise_for_status()
        return resp.json()
