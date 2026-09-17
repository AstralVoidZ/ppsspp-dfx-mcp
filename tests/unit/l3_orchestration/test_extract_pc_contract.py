"""L3 orchestration tests: extract_pc GPR contract (V027 §5.5 → V028 refactor).

Anchors: B.2 spec §5.5 V027 documented decision (originally two
duplicate `_extract_pc` implementations on SteppingManager and
PpssppDebugClient). V028 refactor centralized them into a single
shared function `core.registers.extract_pc` — the duplication
documented in V027 no longer exists.

L3 focus (NOT covered by L4 / unit tests):
- V028: the shared `extract_pc` function produces the correct PC for
  the same inputs that V027 originally anchored. Both call sites
  (SteppingManager.safe_get_pc and PpssppDebugClient.safe_get_pc)
  now delegate to this single function — divergence is no longer
  possible by construction.
- GPR category name is hardcoded "GPR" (PPSSPP CPURegsSubscriber.cpp
  contract — the category name is always "GPR" for MIPS general-purpose
  registers, including the PC register).
- Graceful degradation on malformed input (empty / missing fields /
  non-GPR categories) returns 0 (NOT an exception).

V027 (B.2 §5.5): extract_pc only matches `cat.name == "GPR"` — it
does not handle category name localization or missing names. This is
acceptable because PPSSPP's CPURegsSubscriber.cpp hardcodes "GPR" as
the category name (not localized). L3 anchors this contract.
"""

from __future__ import annotations

from typing import Any

import pytest

from ppsspp_dfx_mcp.core.registers import extract_pc

# ============================================================================
# Shared extract_pc produces the correct PC (V028 — replaces V027 duplication)
# ============================================================================


class TestExtractPcReturnValue:
    """L3: the shared `extract_pc` function returns the correct PC.

    V028 centralized the two duplicate `_extract_pc` static methods
    (originally on SteppingManager and PpssppDebugClient) into a single
    shared function in `core/registers.py`. Both call sites now import
    and use this function, so divergence is no longer possible by
    construction. This test anchors the shared function's contract.
    """

    @pytest.mark.parametrize(
        "regs,expected_pc,description",
        [
            # Normal case: PC in GPR category
            (
                {
                    "categories": [
                        {
                            "name": "GPR",
                            "registerNames": ["r0", "r1", "pc"],
                            "uintValues": [0, 0, 0x08804000],
                        }
                    ]
                },
                0x08804000,
                "normal GPR with pc",
            ),
            # PC not in GPR (only r0, r1)
            (
                {
                    "categories": [
                        {
                            "name": "GPR",
                            "registerNames": ["r0", "r1"],
                            "uintValues": [0, 0],
                        }
                    ]
                },
                0,
                "GPR without pc register",
            ),
            # PC in FPU category (must be ignored — only GPR is searched)
            (
                {
                    "categories": [
                        {
                            "name": "FPU",
                            "registerNames": ["pc"],
                            "uintValues": [0xDEAD],
                        }
                    ]
                },
                0,
                "pc in FPU category (non-GPR ignored)",
            ),
            # Empty categories
            ({"categories": []}, 0, "empty categories"),
            # Missing categories key
            ({}, 0, "missing categories key"),
            # Multiple categories, GPR has pc
            (
                {
                    "categories": [
                        {
                            "name": "FPU",
                            "registerNames": ["f0"],
                            "uintValues": [0],
                        },
                        {
                            "name": "GPR",
                            "registerNames": ["r0", "pc"],
                            "uintValues": [0, 0x12345678],
                        },
                        {
                            "name": "VFPU",
                            "registerNames": ["v0"],
                            "uintValues": [0],
                        },
                    ]
                },
                0x12345678,
                "multiple categories, GPR has pc",
            ),
            # GPR category with pc as first register
            (
                {
                    "categories": [
                        {
                            "name": "GPR",
                            "registerNames": ["pc", "r0"],
                            "uintValues": [0xABCDEF, 0],
                        }
                    ]
                },
                0xABCDEF,
                "pc as first register in GPR",
            ),
        ],
    )
    def test_extract_pc_returns_expected_pc(
        self, regs: dict[str, Any], expected_pc: int, description: str
    ):
        """extract_pc returns the expected PC value for each input shape.

        V028: the shared function in `core/registers.py` must satisfy
        the same contract that V027 originally anchored on both
        duplicate implementations.
        """
        pc = extract_pc(regs)
        assert pc == expected_pc, (
            f"extract_pc returned {pc:#x}, expected {expected_pc:#x} ({description})"
        )


# ============================================================================
# GPR category name hardcoded contract (PPSSPP CPURegsSubscriber.cpp)
# ============================================================================


class TestGprCategoryNameContract:
    """L3: GPR category name is hardcoded "GPR" (PPSSPP contract).

    PPSSPP's CPURegsSubscriber.cpp:L48-95 hardcodes the category name
    as "GPR" for MIPS general-purpose registers. The PC register lives
    in this category. extract_pc relies on this contract — it matches
    `cat.name == "GPR"` exactly (case-sensitive).

    L3 anchors:
    1. Lowercase "gpr" does NOT match (case-sensitive contract)
    2. "General Purpose Registers" (full name) does NOT match
    3. Only exact "GPR" matches
    """

    @pytest.mark.parametrize(
        "category_name,should_match",
        [
            ("GPR", True),
            ("gpr", False),
            ("Gpr", False),
            ("General Purpose Registers", False),
            ("GENERAL PURPOSE", False),
            ("MIPS GPR", False),
            ("", False),
        ],
    )
    def test_only_exact_gpr_matches(self, category_name: str, should_match: bool):
        """Only exact category name "GPR" matches (case-sensitive).

        PPSSPP CPURegsSubscriber.cpp:L48: `catName = "GPR"` — the
        category name is a hardcoded string literal. extract_pc's
        `cat.get("name") == "GPR"` match is correct because PPSSPP
        never localizes or varies this name.
        """
        regs = {
            "categories": [
                {
                    "name": category_name,
                    "registerNames": ["pc"],
                    "uintValues": [0xDEAD],
                }
            ]
        }
        pc = extract_pc(regs)

        if should_match:
            assert pc == 0xDEAD, (
                f"Category name {category_name!r} should match 'GPR' "
                f"contract (PPSSPP CPURegsSubscriber.cpp:L48)."
            )
        else:
            assert pc == 0, (
                f"Category name {category_name!r} must NOT match — only "
                f"exact 'GPR' is the PPSSPP contract."
            )


# ============================================================================
# Graceful degradation on malformed input
# ============================================================================


class TestExtractPcGracefulDegradation:
    """L3: extract_pc behavior on edge-case input (V027 §5.5).

    V027 §5.5 documented decision: extract_pc is NOT defensive — it
    assumes well-formed PPSSPP responses. For inputs that match the
    expected shape but lack a "pc" register, it returns 0. For
    malformed inputs (non-list categories, None categories, missing
    uintValues when "pc" is in registerNames), it raises exceptions.

    L3 anchors both behaviors:
    1. Well-formed but pc-absent → returns 0 (graceful)
    2. Malformed → raises (V027 §5.5: no defensive processing)
    """

    def test_missing_register_names_returns_0(self):
        """GPR category without registerNames → 0 (graceful).

        registerNames defaults to [] (via cat.get("registerNames", [])),
        so "pc" not in [] → returns 0. No exception.
        """
        regs = {
            "categories": [
                {"name": "GPR", "uintValues": [0xDEAD]}
                # Missing registerNames
            ]
        }
        pc = extract_pc(regs)
        assert pc == 0

    def test_empty_input_dict_returns_0(self):
        """Empty dict → 0 (graceful).

        regs.get("categories", []) returns [] → for loop does not
        execute → returns 0.
        """
        pc = extract_pc({})
        assert pc == 0

    def test_missing_uint_values_raises_index_error(self):
        """GPR with pc in registerNames but no uintValues → IndexError.

        V027 §5.5: extract_pc does NOT validate that uintValues has
        enough entries. If "pc" is in registerNames but uintValues is
        missing/empty, `vals[names.index("pc")]` raises IndexError.
        This is the documented non-defensive behavior.
        """
        regs = {
            "categories": [
                {"name": "GPR", "registerNames": ["pc"]}
                # Missing uintValues
            ]
        }
        with pytest.raises(IndexError):
            extract_pc(regs)

    def test_categories_not_a_list_raises(self):
        """categories field not a list → AttributeError (V027 §5.5).

        If categories is a string, `for cat in "string"` iterates
        characters, and `cat.get("name")` raises AttributeError
        (strings have no .get method). Non-defensive behavior.
        """
        regs = {"categories": "not a list"}
        with pytest.raises(AttributeError):
            extract_pc(regs)

    def test_categories_is_none_raises(self):
        """categories field is None → TypeError (V027 §5.5).

        regs.get("categories", []) returns None (key exists, value is
        None), and `for cat in None` raises TypeError. Non-defensive.
        """
        regs = {"categories": None}
        with pytest.raises(TypeError):
            extract_pc(regs)
