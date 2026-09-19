"""test_completion_contract.py — L2 契约：prompt 参数补全。

Anchor: openspec change `ppsspp-dfx-mcp-protocol-and-schema`
- `specs/prompt-argument-completions/spec.md`（ADDED）
- `specs/ppsspp-dfx-mcp-server/spec.md`（capabilities 一致性）

契约：
1. 注册 completion handler → `capabilities.completions` 出现在握手响应中。
2. 两个 memory wizard 的 `address` 参数返回可**直接使用**的候选（十六进制
   字符串），前缀匹配生效。
3. 无匹配 / 非 address 参数 / 非 prompt ref → 空候选**而非错误**。
4. **候选不得包含非运行时地址**——`addresses.yaml` 里 `known_render_vars`
   等节存的是 IDA 偏移，直接塞进 `ppsspp_breakpoint` 会指错内存。这是本
   文件最重要的一条断言。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp.types import (
    CompletionArgument,
    PromptReference,
    ResourceTemplateReference,
)

from ppsspp_dfx_mcp import completions
from ppsspp_dfx_mcp.completions import address_candidates, complete

# test_completion_contract.py → l2_mcp_contract → unit → tests → repo root.
# (Standalone-repo layout since v0.1.2. The old monorepo `parents[5]` made
# this module resolve a config dir OUTSIDE the repo — it silently skipped
# on every fresh clone / CI checkout, so the whole completion contract,
# including the non-runtime address-leak guard below, never ran there.
# C4, review v2: resolution is repo-internal and CI-enforced again.)
_REPO_ROOT = Path(__file__).resolve().parents[3]
_EXAMPLES_DIR = _REPO_ROOT / "examples"

_BREAKPOINT = PromptReference(name="memory-breakpoint-wizard")
_TRACE = PromptReference(name="memory-trace-wizard")


@pytest.fixture(autouse=True)
def _real_config_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point config_dir() at the repo-committed `examples/addresses.yaml`.

    `config.py:_project_root()` is deliberately cwd-relative (no upward
    search — git/npm convention), so without this fixture the completions
    capability would see whatever config happens to sit near the cwd.
    `examples/` is the committed template: it ships top_base/known_modules/
    known_functions/game_mode_addr/state_probes placeholders in BOTH
    address spaces, which is exactly what the leak guard below needs —
    and being committed, the contract is enforced identically on every
    fresh clone and in CI (a workspace-local config would make this a
    machine-dependent "pass").
    """
    assert _EXAMPLES_DIR.is_dir(), f"examples dir missing: {_EXAMPLES_DIR}"
    monkeypatch.setenv("PPSSPP_DFX_CONFIG_DIR", str(_EXAMPLES_DIR))


def _arg(name: str = "address", value: str = "") -> CompletionArgument:
    return CompletionArgument(name=name, value=value)


class TestCompletionsCapabilityDeclared:
    """5.3：能力必须在握手中声明（SDK 由已注册 handler 派生）。"""

    def test_completions_declared(self):
        from ppsspp_dfx_mcp.server import mcp

        options = mcp._lowlevel_server.create_initialization_options()
        caps = options.capabilities.model_dump(exclude_none=True)
        assert "completions" in caps

    def test_handler_registered_on_lowlevel_server(self):
        """能力来源是真实 handler，不是手写的 capability 结构。"""
        from ppsspp_dfx_mcp.server import mcp

        assert "completion/complete" in mcp._lowlevel_server._request_handlers


class TestAddressCompletion:
    """5.1：`address` 参数返回前缀匹配的地址候选。"""

    async def test_prefix_match_returns_candidates(self):
        result = await complete(_BREAKPOINT, _arg("address", "0x088"), None)
        assert result is not None
        assert result.values, "0x088 前缀应至少命中一个地址"
        for value in result.values:
            assert value.lower().startswith("0x088"), value

    async def test_candidates_are_directly_usable_hex(self):
        """候选即参数值——必须是能被工具直接接受的十六进制地址。"""
        result = await complete(_BREAKPOINT, _arg("address", ""), None)
        assert result is not None
        for value in result.values:
            assert value.startswith("0x"), value
            int(value, 16)  # 解析失败即抛错

    async def test_empty_prefix_lists_all_and_reports_total(self):
        result = await complete(_BREAKPOINT, _arg("address", ""), None)
        assert result is not None
        assert result.total == len(result.values) or result.has_more
        assert result.has_more is False  # 本仓地址规模远低于协议上限

    async def test_query_without_0x_prefix_still_matches(self):
        """`8804` 应命中 `0x08804000`——不能要求调用方记住补零规则。"""
        result = await complete(_BREAKPOINT, _arg("address", "8804"), None)
        assert result is not None
        assert "0x08804000" in result.values

    async def test_case_insensitive(self):
        lower = await complete(_BREAKPOINT, _arg("address", "0x088"), None)
        upper = await complete(_BREAKPOINT, _arg("address", "0X088"), None)
        assert lower is not None and upper is not None
        assert lower.values == upper.values

    async def test_trace_wizard_also_served(self):
        result = await complete(_TRACE, _arg("address", "0x088"), None)
        assert result is not None
        assert result.values


class TestNoMatchIsNotAnError:
    """5.1/5.2：不匹配返回空候选，不抛错。"""

    async def test_no_match_returns_none(self):
        assert await complete(_BREAKPOINT, _arg("address", "0x999"), None) is None

    async def test_non_address_argument_returns_none(self):
        """`size` 无枚举来源（1..64 的整数区间）。"""
        assert await complete(_BREAKPOINT, _arg("size", "4"), None) is None

    async def test_unknown_prompt_returns_none(self):
        ref = PromptReference(name="not-a-real-prompt")
        assert await complete(ref, _arg("address", "0x088"), None) is None

    async def test_resource_ref_returns_none(self):
        """两个资源是无变量的普通 URI，没有可补全的模板参数。"""
        ref = ResourceTemplateReference(uri="ppsspp://game-state")
        assert await complete(ref, _arg("address", "0x088"), None) is None

    async def test_unreadable_config_returns_empty_not_raise(self, monkeypatch):
        """配置不可读时补全退化为"无建议"，不阻断 prompt 调用。"""

        def boom() -> dict:
            raise OSError("simulated config failure")

        monkeypatch.setattr(completions.config, "addresses", boom)
        assert address_candidates("0x088") == ([], 0)
        assert await complete(_BREAKPOINT, _arg("address", "0x088"), None) is None


class TestCandidatesExcludeNonRuntimeValues:
    """地址带过滤：只有能直接使用的运行时地址才可作为候选。

    `addresses.yaml` 混装多个地址空间。若把 IDA 地址当候选吐出去，
    Agent 会把 known_functions 里的 `0x000286A8`（user_main，IDA 相对
    偏移，运行时地址应为 0x0882C6A8）直接交给 `ppsspp_breakpoint` ——
    补全反而制造错误。本组断言把该过滤固化为契约，对 examples/
    addresses.yaml 的非运行时取值逐一生效。
    """

    @pytest.mark.parametrize(
        "ida_or_file_offset",
        ["0x000286A8", "0x00000000"],
    )
    async def test_off_band_values_never_appear(self, ida_or_file_offset: str):
        result = await complete(_BREAKPOINT, _arg("address", ""), None)
        assert result is not None
        assert ida_or_file_offset.upper() not in {v.upper() for v in result.values}

    async def test_all_candidates_are_in_runtime_bands(self):
        result = await complete(_BREAKPOINT, _arg("address", ""), None)
        assert result is not None
        for value in result.values:
            n = int(value, 16)
            assert any(lo <= n < hi for lo, hi in completions._RUNTIME_BANDS), (
                f"{value} 不在任何运行时地址带内"
            )

    async def test_known_runtime_addresses_present(self):
        """确定在带内的代表性地址应出现（防止过滤过严导致补全全空）。"""
        result = await complete(_BREAKPOINT, _arg("address", ""), None)
        assert result is not None
        values = {v.upper() for v in result.values}
        assert "0X08804000" in values  # top.prx 运行时基址
        assert "0X08A0D000" in values  # game_mode


class TestValueCap:
    """协议规定 `values` 不得超过 100 条；超限时给出 total/has_more。"""

    async def test_cap_and_pagination_hint(self, monkeypatch):
        # examples/ 只有 2 个运行时地址，凑不满 cap —— 注入合成配置
        # 专测分页语义（与 TestNoMatch 的 unreadable-config 注入同法）。
        synthetic = {
            "known_modules": {f"mod{i}": 0x08810000 + i * 0x10 for i in range(5)},
        }
        monkeypatch.setattr(completions.config, "addresses", lambda: synthetic)
        monkeypatch.setattr(completions, "_MAX_VALUES", 3)
        result = await complete(_BREAKPOINT, _arg("address", ""), None)
        assert result is not None
        assert len(result.values) == 3
        assert result.total is not None and result.total > 3
        assert result.has_more is True
