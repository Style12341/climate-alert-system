from typing import Any

from pydantic import BaseModel


class ErrorOut(BaseModel):
    detail: str


UNAUTHORIZED: dict[int | str, dict[str, Any]] = {401: {"model": ErrorOut}}
NOT_FOUND: dict[int | str, dict[str, Any]] = {404: {"model": ErrorOut}}
CONFLICT: dict[int | str, dict[str, Any]] = {409: {"model": ErrorOut}}
UNAVAILABLE: dict[int | str, dict[str, Any]] = {503: {"model": ErrorOut}}
