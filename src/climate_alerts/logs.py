import logging
import sys

import structlog
from structlog.typing import Processor

_NOISY_AT_INFO = ("apscheduler",)
_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


class _JsonHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """Marks our handler, so configuring again replaces only this one."""


def configure_logging(level: str) -> None:
    """Render every log as JSON at the given level, structlog's and stdlib's.

    Libraries log through stdlib `logging`, so the root logger gets a handler
    that formats through the same processors. uvicorn installs its own text
    handlers before this runs, so they are removed and it propagates to root.

    Callers must not log phone numbers or emails.
    """
    numeric = logging.getLevelNamesMapping()[level]
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]
    structlog.configure(
        processors=[
            *shared,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric),
        logger_factory=structlog.PrintLoggerFactory(),
    )

    handler = _JsonHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=[*shared, structlog.stdlib.add_logger_name],
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.format_exc_info,
                structlog.processors.JSONRenderer(),
            ],
        )
    )
    root = logging.getLogger()
    root.handlers[:] = [h for h in root.handlers if not isinstance(h, _JsonHandler)]
    root.addHandler(handler)
    root.setLevel(numeric)

    for name in _UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
    # Two lines per job per interval at INFO. Its warnings, such as a run
    # skipped by max_instances, are what an operator needs.
    for name in _NOISY_AT_INFO:
        logging.getLogger(name).setLevel(max(numeric, logging.WARNING))
