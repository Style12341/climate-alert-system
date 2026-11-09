from fastapi import APIRouter, status

from climate_alerts.alerts import service
from climate_alerts.alerts.models import Alert
from climate_alerts.alerts.schemas import AlertCreate, AlertOut, AlertUpdate
from climate_alerts.api.dependencies import (
    CurrentUserId,
    OptionalIdQuery,
    PathId,
    SessionDep,
)
from climate_alerts.api.errors import error_responses
from climate_alerts.errors import (
    AlertAlreadyExists,
    AlertNotFound,
    FieldNotFound,
    InvalidCredentials,
    UnknownEventType,
)

router = APIRouter(tags=["alerts"], responses=error_responses(InvalidCredentials))


@router.post(
    "/fields/{field_id}/alerts",
    response_model=AlertOut,
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(FieldNotFound, UnknownEventType, AlertAlreadyExists),
)
async def create_alert(
    field_id: PathId,
    body: AlertCreate,
    session: SessionDep,
    user_id: CurrentUserId,
) -> Alert:
    return await service.create_alert(
        session, user_id, field_id, body.event_type, body.threshold
    )


@router.get("/alerts", response_model=list[AlertOut])
async def list_alerts(
    session: SessionDep, user_id: CurrentUserId, field_id: OptionalIdQuery = None
) -> list[Alert]:
    return await service.list_alerts(session, user_id, field_id)


@router.get(
    "/alerts/{alert_id}",
    response_model=AlertOut,
    responses=error_responses(AlertNotFound),
)
async def get_alert(
    alert_id: PathId, session: SessionDep, user_id: CurrentUserId
) -> Alert:
    return await service.get_alert(session, user_id, alert_id)


@router.patch(
    "/alerts/{alert_id}",
    response_model=AlertOut,
    responses=error_responses(AlertNotFound),
)
async def update_alert(
    alert_id: PathId,
    body: AlertUpdate,
    session: SessionDep,
    user_id: CurrentUserId,
) -> Alert:
    return await service.update_threshold(session, user_id, alert_id, body.threshold)


@router.delete(
    "/alerts/{alert_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=error_responses(AlertNotFound),
)
async def delete_alert(
    alert_id: PathId, session: SessionDep, user_id: CurrentUserId
) -> None:
    await service.delete_alert(session, user_id, alert_id)
