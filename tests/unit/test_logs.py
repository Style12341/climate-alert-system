import json
import subprocess
import sys
from pathlib import Path

# In a subprocess because configuring the root logger would leak into the suite.
EMIT_THROUGH_EVERY_LOGGER = """
import logging.config

import structlog
import uvicorn.config

from climate_alerts.logs import configure_logging

# uvicorn installs its own text handlers before the application is created.
logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)
configure_logging("INFO")

structlog.get_logger().info("app.event", value=1)
logging.getLogger("uvicorn.error").info("Started server process [%d]", 7)
logging.getLogger("uvicorn.access").info(
    '%s - "%s %s HTTP/%s" %d', "127.0.0.1:1", "GET", "/health/live", "1.1", 200
)
logging.getLogger("apscheduler.scheduler").warning("maximum number of instances")
logging.getLogger("apscheduler.executors.default").info("Running job")
try:
    raise ValueError("boom")
except ValueError:
    logging.getLogger("uvicorn.error").exception("Exception in ASGI application")
"""


def emitted_lines(tmp_path: Path) -> list[dict[str, object]]:
    result = subprocess.run(
        [sys.executable, "-c", EMIT_THROUGH_EVERY_LOGGER],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=True,
    )
    assert result.stderr == ""
    return [json.loads(line) for line in result.stdout.splitlines()]


def test_stdlib_loggers_share_the_json_pipeline(tmp_path: Path) -> None:
    lines = emitted_lines(tmp_path)

    by_event = {line["event"]: line for line in lines}
    assert by_event["app.event"]["level"] == "info"
    assert by_event["app.event"]["value"] == 1
    assert by_event["Started server process [7]"]["level"] == "info"
    assert by_event["Started server process [7]"]["logger"] == "uvicorn.error"
    assert '"GET /health/live HTTP/1.1" 200' in " ".join(by_event)
    assert by_event["maximum number of instances"]["level"] == "warning"
    assert all("timestamp" in line for line in lines)
    assert not any("_record" in line or "color_message" in line for line in lines)


def test_foreign_exceptions_are_rendered_in_the_json_line(tmp_path: Path) -> None:
    [failure] = [
        line
        for line in emitted_lines(tmp_path)
        if line["event"] == "Exception in ASGI application"
    ]

    assert "ValueError: boom" in str(failure["exception"])


def test_scheduler_chatter_stays_out_of_info_logs(tmp_path: Path) -> None:
    events = [line["event"] for line in emitted_lines(tmp_path)]

    assert "Running job" not in events
