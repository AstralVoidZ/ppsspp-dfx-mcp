"""W11 fix (2026-09-06): normalize_reg_name robustness.

PPSSPP's cpu.getReg / cpu.setReg require the exact lowercase ABI names
(MIPSDebugInterface::GetRegName) plus special-cased "pc"/"hi"/"lo".
Pre-fix, "$ra" and "PC" were forwarded verbatim and rejected with
"Invalid 'name' parameter" — the docstring claimed lowercasing the code
never did.
"""

from __future__ import annotations

from ppsspp_dfx_mcp.core.registers import normalize_reg_name


class TestNormalizeRegName:
    def test_numeric_style_maps_to_abi(self):
        assert normalize_reg_name("r3") == "v1"
        assert normalize_reg_name("r0") == "zero"
        assert normalize_reg_name("r31") == "ra"

    def test_abi_names_pass_through(self):
        assert normalize_reg_name("a0") == "a0"
        assert normalize_reg_name("t9") == "t9"

    def test_dollar_prefix_stripped(self):
        assert normalize_reg_name("$ra") == "ra"
        assert normalize_reg_name("$a0") == "a0"

    def test_uppercase_lowered(self):
        assert normalize_reg_name("PC") == "pc"
        assert normalize_reg_name("RA") == "ra"
        assert normalize_reg_name("A0") == "a0"

    def test_special_registers_lowered(self):
        assert normalize_reg_name("HI") == "hi"
        assert normalize_reg_name("Lo") == "lo"

    def test_whitespace_trimmed(self):
        assert normalize_reg_name("  $a0 ") == "a0"
