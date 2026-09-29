"""Environment + YAML configuration (stderr-only logging).

Three-layer parallel config (no parent walking, git/npm/ripgrep style):
1. env vars (highest priority)
2. ./.ppsspp-dfx/config/*.yaml (project-level)
3. cwd default (lowest)

PPSSPP exe path is configured via yaml, not hardcoded.

Project root resolution (``project_root()``):
- ``PPSSPP_DFX_PROJECT_ROOT`` env var > cwd.
- Env var set but invalid (missing/non-dir) → ``ConfigInvalid`` (explicit
  config errors fail immediately; source: cwd-config-refactor experience).
- Env var unset and cwd lacks the ``.ppsspp-dfx/`` marker dir → warning
  only, not blocking (backward compat; source: cwd-config-refactor experience).
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

import yaml

LOGGER_NAME = "ppsspp_dfx_mcp"
log = logging.getLogger(LOGGER_NAME)
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_RATE_LIMIT = "60"
DEFAULT_WS_HOST = "127.0.0.1"
DEFAULT_WS_PORT = "12345"
DEFAULT_SESSIONS_PATH = "~/.ppsspp-dfx/sessions.json"

# Config file location (project-level, discovered via cwd, no parent walking).
_CONFIG_DIR_ENV = "PPSSPP_DFX_CONFIG_DIR"
_DEFAULT_CONFIG_DIR = ".ppsspp-dfx/config"
_PROJECT_ROOT_ENV = "PPSSPP_DFX_PROJECT_ROOT"

# Project marker directory: its presence distinguishes a real project root
# from an arbitrary directory (e.g. a MCP host's temp spawn dir). Used for
# early-warning detection — absence does NOT block (backward compat), but
# surfaces a warning so misconfiguration is not silent.
_PROJECT_MARKER_DIR = ".ppsspp-dfx"

# Module-level guard so the marker-missing warning fires at most once per
# process. project_root() is called on every config read; without this
# guard, a misconfigured root would flood the stderr log.
_project_root_marker_warned = False


def project_root() -> Path:
    """Project root: PPSSPP_DFX_PROJECT_ROOT env var > cwd.

    Default follows the git/npm/ripgrep pattern: caller is responsible
    for invoking from the project root, and we do NOT search upward.

    The env override exists because MCP hosts do not always honor a
    configured ``cwd``: some spawn stdio servers from a temp directory,
    which silently misresolves the PPSSPP exe path, the script manifest
    paths, and ``output_dir()``. Setting this pins the root regardless
    of the host's spawn directory.

    Error handling (env var > cwd policy):
    - Env var set but path does not exist → raises ``ConfigInvalid``
      (an explicit config error should fail immediately, not silently
      fall back to cwd — that would mask the user's intent).
    - Env var set but path is not a directory → raises ``ConfigInvalid``.
    - Env var set, path valid, but no ``.ppsspp-dfx/`` marker dir →
      warning (not blocking; the root may be intentionally non-standard).
    - Env var unset and cwd has no ``.ppsspp-dfx/`` marker dir →
      warning (not blocking; preserves backward compat, but surfaces
      the likely-misconfigured spawn directory instead of silently
      using a temp dir as the project root).
    """
    global _project_root_marker_warned
    raw = os.environ.get(_PROJECT_ROOT_ENV, "")
    if raw:
        p = Path(raw).expanduser()
        if not p.exists():
            from ppsspp_dfx_mcp.errors import ConfigInvalid

            raise ConfigInvalid(
                f"PPSSPP_DFX_PROJECT_ROOT={raw!r} does not exist — "
                f"unset the env var to fall back to cwd, or fix the path",
            )
        if not p.is_dir():
            from ppsspp_dfx_mcp.errors import ConfigInvalid

            raise ConfigInvalid(
                f"PPSSPP_DFX_PROJECT_ROOT={raw!r} is not a directory",
            )
        root = p.resolve()
        if not (root / _PROJECT_MARKER_DIR).exists() and not _project_root_marker_warned:
            log.warning(
                "PPSSPP_DFX_PROJECT_ROOT=%s has no %s/ marker directory — "
                "config/output paths may not resolve as expected",
                raw,
                _PROJECT_MARKER_DIR,
            )
            _project_root_marker_warned = True
        return root
    cwd = Path.cwd()
    if not (cwd / _PROJECT_MARKER_DIR).exists() and not _project_root_marker_warned:
        log.warning(
            "cwd=%s has no %s/ marker directory — if the MCP host spawned "
            "from a temp dir, set PPSSPP_DFX_PROJECT_ROOT to pin the project root",
            cwd,
            _PROJECT_MARKER_DIR,
        )
        _project_root_marker_warned = True
    return cwd


def config_dir() -> Path:
    """Return the config directory path.

    Priority: PPSSPP_DFX_CONFIG_DIR env var > cwd/.ppsspp-dfx/config.
    """
    raw = os.environ.get(_CONFIG_DIR_ENV, "")
    if raw:
        return Path(raw).expanduser().resolve()
    return (project_root() / _DEFAULT_CONFIG_DIR).resolve()


def log_level() -> str:
    return os.environ.get("PPSSPP_DFX_LOG_LEVEL", DEFAULT_LOG_LEVEL).upper()


def log_format() -> str:
    return os.environ.get("PPSSPP_DFX_LOG_FORMAT", "text").lower()


def rate_limit() -> int:
    raw = os.environ.get("PPSSPP_DFX_RATE_LIMIT", DEFAULT_RATE_LIMIT)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return int(DEFAULT_RATE_LIMIT)


def ws_host() -> str:
    return os.environ.get("PPSSPP_DFX_WS_HOST", DEFAULT_WS_HOST)


def ws_port() -> int:
    raw = os.environ.get("PPSSPP_DFX_WS_PORT", DEFAULT_WS_PORT)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return int(DEFAULT_WS_PORT)


def sessions_path() -> Path:
    """Return the sessions JSON path (user-level, cross-process)."""
    raw = os.environ.get("PPSSPP_DFX_SESSIONS_PATH", DEFAULT_SESSIONS_PATH)
    return Path(raw).expanduser().resolve()


def output_dir() -> Path:
    """Return the output directory path (.ppsspp-dfx/output/).

    Auto-creates the directory. Output is gitignored.
    """
    path = project_root() / ".ppsspp-dfx" / "output"
    path.mkdir(parents=True, exist_ok=True)
    return path


def ppsspp_exe_path() -> Path | None:
    """Return PPSSPP executable path.

    Priority: PPSSPP_DFX_EXE_PATH env var > .ppsspp-dfx/config/project.yaml:ppsspp_exe.
    A relative yaml value resolves against `project_root()` (not cwd), so a
    host that spawns the server from elsewhere cannot silently redirect it.
    Returns None if not configured (caller may raise).
    """
    raw = os.environ.get("PPSSPP_DFX_EXE_PATH", "")
    if raw:
        return Path(raw).expanduser().resolve()
    val = _load_yaml_value("project.yaml", "ppsspp_exe", default="")
    if val:
        p = Path(val).expanduser()
        return p.resolve() if p.is_absolute() else (project_root() / p).resolve()
    return None


# ── Test mode (MCP Inspector integration) ─────────────────────────────────
#
# When PPSSPP_DFX_TEST_MODE=fake, the MCP server substitutes a
# FakeTransport pre-loaded with recorded fixtures for the real
# WsTransport. This lets L2 MCP-contract tests and MCP Inspector
# end-to-end tests exercise the full server → tool → transport stack
# without a live PPSSPP process.
#
# PPSSPP_DFX_FIXTURE_DIR points at the directory of recorded per-event
# JSON fixtures (one file per WS event, produced by
# `python -m ppsspp_dfx_mcp.scripts.record_fixtures`). Required when
# test_mode == "fake"; raises if missing.
#
# Default behavior (env vars unset): real WsTransport, no fixture
# injection. This preserves the production code path verbatim.

_TEST_MODE_ENV = "PPSSPP_DFX_TEST_MODE"
_FIXTURE_DIR_ENV = "PPSSPP_DFX_FIXTURE_DIR"


def boot_heal_quarantine_gpu_blacklist() -> bool:
    """Whether the wedge self-heal may quarantine the GPU
    backend failure blacklist (FailedGraphicsBackends.txt — rename only,
    never delete; see session/safe_boot.py). Default ON; set
    PPSSPP_DFX_BOOT_HEAL_QUARANTINE=0 to disable (boot wedge healing
    then relaunches WITHOUT touching the file).
    """
    raw = os.environ.get("PPSSPP_DFX_BOOT_HEAL_QUARANTINE", "1")
    return raw.strip().lower() not in ("0", "false", "no", "off")


def test_mode() -> str:
    """Return the test mode flag.

    Returns:
        "fake" — substitute FakeTransport + recorded fixtures for the
            real WsTransport. Used by MCP Inspector tests.
        "" (empty) — production mode (real WsTransport). Default.
    """
    return os.environ.get(_TEST_MODE_ENV, "").lower()


def fixture_dir() -> Path | None:
    """Return the recorded fixtures directory (test mode only).

    Priority: PPSSPP_DFX_FIXTURE_DIR env var > None.
    Returns None if not configured (caller may raise when test_mode=="fake").
    """
    raw = os.environ.get(_FIXTURE_DIR_ENV, "")
    if raw:
        return Path(raw).expanduser().resolve()
    return None


def addresses() -> dict[str, Any]:
    """Return address constants from .ppsspp-dfx/config/addresses.yaml."""
    data = _load_yaml("addresses.yaml", default={})
    if not isinstance(data, dict):
        log.warning("addresses.yaml root is not a dict (got %r); using empty", type(data).__name__)
        return {}
    return data


def _load_yaml(filename: str, default: Any = None) -> Any:
    """Load a YAML config file from the config directory."""
    if default is None:
        default = {}
    path = config_dir() / filename
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
            return data if data is not None else default
    except (OSError, yaml.YAMLError) as e:
        log.warning("yaml load failed for %s, using default: %s", filename, e)
        return default


def _load_yaml_value(filename: str, key: str, default: Any = None) -> Any:
    """Load a single key from a YAML config file."""
    data = _load_yaml(filename, default={})
    if not isinstance(data, dict):
        return default
    return data.get(key, default)


def configure_logging() -> None:
    """Configure stderr-only logging with optional JSON format."""
    level_name = log_level().upper()
    level = getattr(logging, level_name, None)
    if not isinstance(level, int):
        # S9 (review v2): a typo like INF0 used to fall back to INFO with
        # no trace — validate_config covers file fields, not this env var.
        logging.getLogger(__name__).warning(
            "PPSSPP_DFX_LOG_LEVEL=%r is not a valid level; falling back to INFO",
            log_level(),
        )
        level = logging.INFO
    handler = logging.StreamHandler(sys.stderr)
    if log_format() == "json":
        from ppsspp_dfx_mcp.logging import JsonFormatter

        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    # Mirror PPSSPP broadcast logs to
    # .ppsspp-dfx/output/ppsspp.log so ppsspp_analyze_log's default path
    # (log_path=None) reads a real file. Idempotent attachment.
    from ppsspp_dfx_mcp.logging import attach_ppsspp_log_mirror

    attach_ppsspp_log_mirror()


def validate_config() -> None:
    """Validate config at startup. Collects all errors before raising.

    Raises:
        ConfigInvalid: one or more config values failed validation.
    """
    from ppsspp_dfx_mcp.errors import ConfigInvalid

    errors: list[str] = []
    if ws_port() < 1 or ws_port() > 65535:
        errors.append(f"PPSSPP_DFX_WS_PORT={ws_port()} not in 1-65535")
    if rate_limit() < 0:
        errors.append(f"PPSSPP_DFX_RATE_LIMIT={rate_limit()} must be >= 0")
    if errors:
        raise ConfigInvalid("config validation failed:\n  " + "\n  ".join(errors))
