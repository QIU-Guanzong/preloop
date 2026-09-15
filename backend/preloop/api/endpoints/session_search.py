"""Ranked keyword search across runtime session content.

``POST``, not ``GET``, and deliberately so. The two search surfaces that
already exist (``GET /account/gateway-usage/search`` and ``GET /search``) put
the query text in the request path, where every proxy, load balancer and
access log on the way keeps a copy. The text an operator types here is what
they are hunting for in their own agent transcripts, which is the last kind of
string that should be sitting in a log file. A body costs a caller nothing and
keeps it out.

The permission is ``view_runtime_sessions``: this reads session content, so it
takes the permission that already guards reading sessions rather than the cost
permission the older account wide search happens to sit behind.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.api.common import get_account_for_user
from preloop.api.loop_safety import run_db_off_loop
from preloop.models.db.session import get_db_session
from preloop.models.models.account import Account
from preloop.models.models.user import User
from preloop.schemas.session_search import (
    SessionSearchRequest,
    SessionSearchResponse,
)
from preloop.services import session_search
from preloop.utils.permissions import require_permission

router = APIRouter()


@router.post(
    "/runtime-sessions/search",
    response_model=SessionSearchResponse,
    summary="Search session content by relevance",
)
@require_permission("view_runtime_sessions")
async def search_runtime_sessions(
    payload: SessionSearchRequest,
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> SessionSearchResponse:
    """Rank the caller's runtime sessions by relevance to a query.

    Returns sessions ordered by a fused relevance score, each with snippets
    that name the turn the match came from. A mode other than ``keyword`` is
    answered with keyword results and a degraded marker rather than an error,
    so a client written against the eventual hybrid contract works today.
    """
    return await run_db_off_loop(
        lambda: session_search.search_sessions(
            db,
            account_id=account.id,
            request=payload,
        )
    )
