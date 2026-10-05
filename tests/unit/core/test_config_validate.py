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

import logging

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


# ── W4/W5: silent config fallbacks now warn and name the raw value ──────


def test_bad_rate_limit_warns_and_falls_back(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A non-integer PPSSPP_DFX_RATE_LIMIT warns (naming the raw value)."""
    monkeypatch.setenv("PPSSPP_DFX_RATE_LIMIT", "sixty")
    with caplog.at_level(logging.WARNING, logger="ppsspp_dfx_mcp"):
        assert config.rate_limit() == int(config.DEFAULT_RATE_LIMIT)
    assert any(
        "PPSSPP_DFX_RATE_LIMIT" in r.getMessage() and "sixty" in r.getMessage()
        for r in caplog.records
    ), caplog.text


def test_bad_ws_port_warns_and_falls_back(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A non-integer PPSSPP_DFX_WS_PORT warns (naming the raw value)."""
    monkeypatch.setenv("PPSSPP_DFX_WS_PORT", "12 45")
    with caplog.at_level(logging.WARNING, logger="ppsspp_dfx_mcp"):
        assert config.ws_port() == int(config.DEFAULT_WS_PORT)
    assert any(
        "PPSSPP_DFX_WS_PORT" in r.getMessage() and "12 45" in r.getMessage() for r in caplog.records
    ), caplog.text


def test_non_utf8_yaml_warns_and_uses_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A GBK-saved addresses.yaml must not escape as a raw UnicodeDecodeError."""
    cfg = tmp_path / "config"
    cfg.mkdir()
    # 0xC0 0xC4 is GBK "ÄÄ"; invalid as UTF-8 → UnicodeDecodeError on read.
    (cfg / "addresses.yaml").write_bytes(b"top_base: \xc0\xc4\n")
    monkeypatch.setenv("PPSSPP_DFX_CONFIG_DIR", str(cfg))
    with caplog.at_level(logging.WARNING, logger="ppsspp_dfx_mcp"):
        assert config.addresses() == {}
    assert any(
        "addresses.yaml" in r.getMessage() and "using default" in r.getMessage()
        for r in caplog.records
    ), caplog.text


# ── A5/A13: output_dir() is a pure getter; ensure_output_dir() creates ──


def test_output_dir_is_pure_and_ensure_creates(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """`output_dir()` must not create; `ensure_output_dir()` must."""
    (tmp_path / ".ppsspp-dfx").mkdir()
    monkeypatch.setenv("PPSSPP_DFX_PROJECT_ROOT", str(tmp_path))
    expected = (tmp_path / ".ppsspp-dfx" / "output").resolve()
    assert config.output_dir().resolve() == expected
    assert not config.output_dir().exists(), "getter must have no side effects"
    created = config.ensure_output_dir()
    assert created.is_dir()


def test_analyze_allowed_roots_raise_config_invalid_on_bad_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A5: root resolution failure surfaces as ConfigInvalid, lazily."""
    from ppsspp_dfx_mcp.errors import ConfigInvalid
    from ppsspp_dfx_mcp.tools import analyze as analyze_mod

    monkeypatch.setenv("PPSSPP_DFX_PROJECT_ROOT", str(tmp_path / "does-not-exist"))
    analyze_mod._log_allowed_roots.cache_clear()
    try:
        with pytest.raises(ConfigInvalid):
            analyze_mod._log_allowed_roots()
    finally:
        analyze_mod._log_allowed_roots.cache_clear()
