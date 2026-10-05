"""JSON formatter for stderr logging + PPSSPP broadcast-log mirror."""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Logger that receives PPSSPP's broadcast log events (GameStateObserver's
# log consumer injects them here).
PPSSPP_LOG_LOGGER_NAME = "ppsspp_dfx_mcp.ppsspp_log"

# Growth cap for the mirrored log file — matches tools/_common.MAX_LOG_BYTES
# (the read-side cap used by ppsspp_analyze_log). Kept as a local constant to
# avoid a core→tools import; if you change one, change both.
MIRROR_MAX_BYTES = 10 * 1024 * 1024

# Space reserved for the "cap reached" marker written below.
#
# Measured 2026-09-30: the marker used to be appended AFTER the
# decision to cap, so the marker itself pushed the file past
# MIRROR_MAX_BYTES. The mirror settled at 10,485,871 bytes while the
# reader (`tools/_common.MAX_LOG_BYTES`) rejects anything above
# 10,485,760 — 111 bytes over, which is the marker's own length. The
# default `ppsspp_analyze_log` path was then permanently unusable and no
# argument could recover it.
#
# Reserving the space up front makes `size <= MIRROR_MAX_BYTES` an
# invariant, so the reader and the writer can never disagree. 128 bytes is
# a bound, not an estimate: the marker is ~105 bytes with a date stamp.
MARKER_MAX_BYTES = 128

# Measured 2026-10-03: the reservation above only holds if the marker is
# written at most once per FILE. The "already marked" flag used to live on the
# handler (per process), so every server restart appended one more marker to a
# mirror that was already inside the reserved band: 10,485,736 + ~104 = a file
# past MIRROR_MAX_BYTES, which the reader rejects forever. The writer now
# decides from the file's own size (see _write_cap_marker) so the invariant
# survives restarts.
CAP_MARKER_PREFIX = "[ppsspp-dfx] log mirror capped"

# Fallback form used when the full marker does not fit under the cap — a
# degenerate small max_bytes, or a file that arrived already inside the
# reserved band. Still one line, so capping stays visible (never silent).
COMPACT_CAP_MARKER = f"\n{CAP_MARKER_PREFIX}\n"


class RequestIdFilter(logging.Filter):
    """Attach the current ``request_id`` to every log record.

    The rate-limit/request-id middleware stores the id in a ContextVar
    (``middleware.get_request_id()``), but nothing ever copied it onto log
    records — so the id existed yet no log line carried it. Installing this
    filter on the stderr handler makes correlation automatic: JSON logs get
    a ``request_id`` field, text logs expose it as ``record.request_id``
    for any downstream consumer.

    The middleware import is deferred to call time: ``middleware`` imports
    ``config`` at module scope and ``config`` imports this module lazily
    during ``configure_logging()``, so a top-level import here would risk a
    cycle. An explicit ``extra={"request_id": ...}`` on a record wins.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            from ppsspp_dfx_mcp.middleware import get_request_id

            record.request_id = get_request_id()
        return True


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
            # The stop used to be silent — analyze_log would
            # forever show a stale tail with no hint that logging stopped.
            # The record that CROSSES the cap is replaced by a one-time
            # marker; everything after is suppressed without growth.
            if size > self._max_bytes:
                return
            # Reserve room for the cap marker BEFORE deciding to cap.
            # Without the reservation the marker is appended past the cap,
            # the file overshoots, and the reader rejects it forever.
            headroom = self._max_bytes - MARKER_MAX_BYTES
            if size + len(line.encode("utf-8")) > headroom:
                self._write_cap_marker(size, len(line.encode("utf-8")))
                return
            with self._path.open("a", encoding="utf-8") as f:
                f.write(line)
        except Exception:  # noqa: BLE001 — logging must never raise
            self.handleError(record)

    def _write_cap_marker(self, size: int, line_bytes: int) -> None:
        """Replace the record that crosses the cap with a one-time marker.

        The marker is appended only when it still fits under
        ``self._max_bytes``. A file can arrive here already inside the
        reserved band — written by a process whose in-memory flag died with
        it, or by an older build without the reservation. Appending the full
        marker in that state was exactly what pushed the mirror past the cap
        and made ``ppsspp_analyze_log``'s default path fail permanently, so
        the fallback is a shorter notice, then nothing at all.
        """
        if getattr(self, "_cap_marked", False):
            return
        self._cap_marked = True
        marker = self._full_cap_marker()
        if size + len(marker.encode("utf-8")) > self._max_bytes:
            marker = COMPACT_CAP_MARKER
            if size + len(marker.encode("utf-8")) > self._max_bytes:
                logger.debug(
                    "log mirror %s at %d bytes has no room for a cap marker "
                    "(line of %d bytes suppressed)",
                    self._path,
                    size,
                    line_bytes,
                )
                return
        with self._path.open("a", encoding="utf-8") as f:
            f.write(marker)

    def _full_cap_marker(self) -> str:
        return (
            f"\n{CAP_MARKER_PREFIX} at "
            f"{self._max_bytes} bytes on "
            f"{time.strftime('%Y-%m-%d %H:%M:%S')} — "
            "further records suppressed.\n"
        )


def _rotate_oversized_mirror(path: Path, max_bytes: int) -> Path | None:
    """Move an over-cap mirror aside so the default log path works again.

    A mirror written by a version without the reserved-marker budget (or
    by any other writer) can sit above ``max_bytes``. The read side rejects
    anything above it and no argument recovers, so the default
    ``ppsspp_analyze_log`` path would stay broken forever. Rotating at attach
    time makes the next run start from an empty file; the old content is
    preserved next to it rather than deleted.

    Returns the rotated path, or None when nothing had to move.
    """
    try:
        if not path.exists():
            return None
        size = path.stat().st_size
        if size <= max_bytes:
            return None
        rotated = path.with_name(path.name + ".1")
        rotated.unlink(missing_ok=True)
        path.replace(rotated)
        logger.warning(
            "log mirror %s was %d bytes (over the %d-byte cap) — rotated to %s; "
            "ppsspp_analyze_log's default path starts clean",
            path,
            size,
            max_bytes,
            rotated,
        )
        return rotated
    except OSError as e:  # pragma: no cover — filesystem-dependent
        logger.debug("could not rotate oversized log mirror %s: %s", path, e)
        return None


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
