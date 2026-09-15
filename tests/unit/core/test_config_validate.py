"""`config.validate_config()` — env-driven validation.

Relocated from `tests/system/ppsspp_dfx/test_config_strategy.py` (root suite).

Why they moved: `validate_config()` imports `ppsspp_dfx_mcp.errors` (for
`ConfigInvalid`), and `errors` imports `mcp.server.mcpserver` — which exists only
in this subproject's venv. Run from the root interpreter these three tests raised
`ModuleNotFoundError: No module named 'mcp.server.mcpserver'`, while the other
19 tests in that file passed (they never reach `errors`). The root suite had no
way to make them green, and deleting them would have dropped the *only* coverage
of `validate_config` in the repo. So they moved here, where the dependency is
satisfied and the autouse session-isolation fixture applies.

The `validate_config` contract: range-check the env-supplied config so a bad
value fails loudly at startup instead of surfacing as a confusing WS failure
later.
"""

from __future__ import annotations

import pytest

from ppsspp_dfx_mcp import config


def test_validate_config_passes_with_valid_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid env config (port in range, rate_limit >= 0) passes validation."""
    monkeypatch.setenv("PPSSPP_DFX_WS_PORT", "12345")
    monkeypatch.setenv("PPSSPP_DFX_RATE_LIMIT", "60")
    assert config.validate_config() is None  # must not raise


def test_validate_config_rejects_out_of_range_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PPSSPP_DFX_WS_PORT=0 (out of 1-65535) fails validation."""
    monkeypatch.setenv("PPSSPP_DFX_WS_PORT", "0")
    from ppsspp_dfx_mcp.errors import ConfigInvalid

    with pytest.raises(ConfigInvalid):
        config.validate_config()


def test_validate_config_rejects_negative_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PPSSPP_DFX_RATE_LIMIT=-1 (negative) fails validation."""
    monkeypatch.setenv("PPSSPP_DFX_RATE_LIMIT", "-1")
    from ppsspp_dfx_mcp.errors import ConfigInvalid

    with pytest.raises(ConfigInvalid):
        config.validate_config()
