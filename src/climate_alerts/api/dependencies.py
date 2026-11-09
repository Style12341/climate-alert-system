from collections.abc import AsyncIterator
from typing import Annotated, cast

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from climate_alerts.auth import MAX_BIGINT, InvalidCredentials, verify_token
from climate_alerts.config import Settings
from climate_alerts.db import SessionFactory

__all__ = ["MAX_BIGINT", "CurrentUserId", "SessionDep", "SettingsDep"]

bearer = HTTPBearer()


def get_app_settings(request: Request) -> Settings:
    return cast(Settings, request.app.state.settings)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    factory = cast(SessionFactory, request.app.state.sessions)
    async with factory() as session:
        yield session


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]


def get_current_user_id(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(bearer)],
    settings: SettingsDep,
) -> int:
    try:
        return verify_token(credentials.credentials, settings)
    except InvalidCredentials as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


CurrentUserId = Annotated[int, Depends(get_current_user_id)]
