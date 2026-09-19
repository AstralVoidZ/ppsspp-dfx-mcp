"""JSON formatter for stderr logging + PPSSPP broadcast-log mirror (W4)."""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Logger that receives PPSSPP's broadcast log events (GameStateObserver's
# log consumer injects them here).
PPSSPP_LOG_LOGGER_NAME = "ppsspp_dfx_mcp.ppsspp_log"

# Growth cap for the mirrored log file — matches tools/_common.MAX_LOG_BYTES
# (the read-side cap used by ppsspp_analyze_log). Kept as a local constant to
# avoid a core→tools import; if you change one, change both.
MIRROR_MAX_BYTES = 10 * 1024 * 1024


class PPSSPPLogMirrorHandler(logging.Handler):
    """Append PPSSPP broadcast-log records to a file.

    Why: ``ppsspp_analyze_log``'s documented default path ("the launcher's
    configured log") never worked — a fresh PpssppLauncher has log_path=None
    and read_log() returned "" unconditionally (real-PPSSPP probe confirmed).
    This handler gives the default path a REAL file: PPSSPP's own broadcast
    log events (error/warning lines emitted by the running game) are mirrored
    to ``.ppsspp-dfx/output/ppsspp.log`` as they arrive.

    Appends are tiny single lines (the observer's log consumer emits one
    record per PPSSPP log broadcast) — synchronous appends here match the
    existing sync logging behavior of the consumer loop.
    """

    def __init__(self, path: Path, max_bytes: int = MIRROR_MAX_BYTES) -> None:
        super().__init__()
        self._path = path
        self._max_bytes = max_bytes

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record) + "\n"
            self._path.parent.mkdir(parents=True, exist_ok=True)
            size = self._path.stat().st_size if self._path.exists() else 0
            # Growth guard: stop appending past the cap (diagnostic log only;
            # analyze_log's read side enforces the same cap independently).
            # S9 (review v2): the stop used to be silent — analyze_log would
            # forever show a stale tail with no hint that logging stopped.
            # The record that CROSSES the cap is replaced by a one-time
            # marker; everything after is suppressed without growth.
            if size > self._max_bytes:
                return
            if size + len(line.encode("utf-8")) > self._max_bytes:
                if not getattr(self, "_cap_marked", False):
                    self._cap_marked = True
                    marker = (
                        "\n[ppsspp-dfx] log mirror capped at "
                        f"{self._max_bytes} bytes on "
                        f"{time.strftime('%Y-%m-%d %H:%M:%S')} — "
                        "further records suppressed.\n"
                    )
                    with self._path.open("a", encoding="utf-8") as f:
                        f.write(marker)
                return
            with self._path.open("a", encoding="utf-8") as f:
                f.write(line)
        except Exception:  # noqa: BLE001 — logging must never raise
            self.handleError(record)


def attach_ppsspp_log_mirror() -> Path | None:
    """Attach the PPSSPP broadcast-log mirror handler (idempotent).

    Returns the mirror file path, or None when attachment was skipped
    (handler already present). Called from ``configure_logging()`` so the
    mirror lives for the whole server lifetime.
    """
    ppsspp_logger = logging.getLogger(PPSSPP_LOG_LOGGER_NAME)
    if any(isinstance(h, PPSSPPLogMirrorHandler) for h in ppsspp_logger.handlers):
        return None

    from ppsspp_dfx_mcp.config import output_dir

    mirror_path = output_dir() / "ppsspp.log"
    handler = PPSSPPLogMirrorHandler(mirror_path)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    ppsspp_logger.addHandler(handler)
    return mirror_path


class JsonFormatter(logging.Formatter):
    """Single-line JSON log format.

    Emits structured logs with timestamp, level, logger name, message,
    and any `extra` fields.  Used when PPSSPP_DFX_LOG_FORMAT=json.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Attach any extra fields (e.g. tool, session_id, request_id).
        for key, value in record.__dict__.items():
            if key in {
                "args",
                "msg",
                "levelname",
                "levelno",
                "pathname",
                "filename",
                "module",
                "exc_info",
                "exc_text",
                "stack_info",
                "lineno",
                "funcName",
                "created",
                "msecs",
                "relativeCreated",
                "thread",
                "threadName",
                "processName",
                "process",
                "name",
                "taskName",
            }:
                continue
            if isinstance(value, (str, int, float, bool, type(None))):
                payload[key] = value
            else:
                # Non-scalar extras (dict/list/
                # datetime/Path) were silently dropped — structured-log
                # consumers lost fields with no trace of them. Coerce to
                # string so the field is always present.
                payload[key] = str(value)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)
