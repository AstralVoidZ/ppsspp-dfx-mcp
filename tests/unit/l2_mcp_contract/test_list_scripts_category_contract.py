"""M12 contract: ppsspp_list_scripts category validation.

An unknown category used to silently return an empty list —
indistinguishable from "category exists but has no scripts" (blind-test
round A). It must fail with ARGS_INVALID listing the valid values, and a
known category must keep working.
"""

from __future__ import annotations

import pytest

from ppsspp_dfx_mcp.errors import ArgsInvalid
from ppsspp_dfx_mcp.tools._common import translate_tool_errors
from ppsspp_dfx_mcp.tools.script import list_scripts


def test_valid_category_returns_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    # manifest discovery is workspace-relative; a missing manifest yields
    # an empty-but-valid listing — the contract under test is the
    # category validation, exercised via the real (decorated) callable.
    fn = (
        translate_tool_errors(list_scripts.__wrapped__)
        if hasattr(list_scripts, "__wrapped__")
        else list_scripts
    )
    result = fn(category="misc")
    assert result["category"] == "misc"
    assert result["count"] == len(result["scripts"])


def test_unknown_category_rejected_with_valid_values() -> None:
    fn = (
        translate_tool_errors(list_scripts.__wrapped__)
        if hasattr(list_scripts, "__wrapped__")
        else list_scripts
    )
    with pytest.raises(ArgsInvalid) as ei:
        fn(category="nope")
    msg = str(ei.value)
    assert "nope" in msg
    for cat in ("eboot", "memory", "misc", "ndx", "p0ab", "recipe", "state"):
        assert cat in msg
