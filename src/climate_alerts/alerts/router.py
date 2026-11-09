from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Query, status

from climate_alerts.alerts import service
from climate_alerts.alerts.models import Alert
from climate_alerts.alerts.schemas import AlertCreate, AlertOut, AlertUpdate
from climate_alerts.api.dependencies import MAX_BIGINT, CurrentUserId, SessionDep

router = APIRouter()

FieldId = Annotated[int, Path(gt=0, le=MAX_BIGINT)]
AlertId = Annotated[int, Path(gt=0, le=MAX_BIGINT)]
FieldFilter = Annotated[int | None, Query(gt=0, le=MAX_BIGINT)]


def _alert_not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, detail="Alert not found")


@router.post(
    "/fields/{field_id}/alerts",
    response_model=AlertOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_alert(
    field_id: FieldId,
    body: AlertCreate,
    session: SessionDep,
    user_id: CurrentUserId,
) -> Alert:
    try:
        return await service.create_alert(
            session, user_id, field_id, body.event_type, body.threshold
        )
    except service.FieldNotFound as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail="Field not found"
        ) from exc
    except service.UnknownEventType as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Unknown event type"
        ) from exc
    except service.AlertAlreadyExists as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail="Alert already exists"
        ) from exc


@router.get("/alerts", response_model=list[AlertOut])
async def list_alerts(
    session: SessionDep, user_id: CurrentUserId, field_id: FieldFilter = None
) -> list[Alert]:
    return await service.list_alerts(session, user_id, field_id)


@router.get("/alerts/{alert_id}", response_model=AlertOut)
async def get_alert(
    alert_id: AlertId, session: SessionDep, user_id: CurrentUserId
) -> Alert:
    try:
        return await service.get_alert(session, user_id, alert_id)
    except service.AlertNotFound as exc:
        raise _alert_not_found() from exc


@router.patch("/alerts/{alert_id}", response_model=AlertOut)
async def update_alert(
    alert_id: AlertId,
    body: AlertUpdate,
    session: SessionDep,
    user_id: CurrentUserId,
) -> Alert:
    try:
        return await service.update_threshold(
            session, user_id, alert_id, body.threshold
        )
    except service.AlertNotFound as exc:
        raise _alert_not_found() from exc


@router.delete("/alerts/{alert_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_alert(
    alert_id: AlertId, session: SessionDep, user_id: CurrentUserId
) -> None:
    try:
        await service.delete_alert(session, user_id, alert_id)
    except service.AlertNotFound as exc:
        raise _alert_not_found() from exc
