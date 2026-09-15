"""F-10 fix lock: dynamically exposed script tools must carry a return
annotation so the SDK infers an output schema and emits
structuredContent (2026-09-08).

Root cause (verified against mcp 2.x `func_metadata` before the fix):
`_build_exposed_wrapper` overrode `__signature__`/`__annotations__`
with the Input-model fields only, dropping the `-> dict[str, Any]`
return annotation. With no return annotation the SDK sets
output_schema=None and the tool answers in the TEXT channel only —
while decorator-registered tools (which annotate `-> dict[str, Any]`)
emit structuredContent normally.
"""

from __future__ import annotations

import inspect
from typing import Any

from mcp.server.mcpserver.utilities.func_metadata import func_metadata
from pydantic import BaseModel, Field

from ppsspp_dfx_mcp.server import _build_exposed_wrapper


class _Input(BaseModel):
    session_id: str | None = Field(default=None)


class _Output(BaseModel):
    message: str = Field(default="ok")


def _fake_entry() -> Any:
    from types import SimpleNamespace
    return SimpleNamespace(
        name="unit_fake_script",
        description="fake",
        category="misc",
        requires_ppsspp=False,
        input_model="_Input",
        output_model="_Output",
    )


def test_exposed_wrapper_keeps_return_annotation() -> None:
    wrapper = _build_exposed_wrapper(_fake_entry(), _Input, _Output)
    assert wrapper.__annotations__.get("return") is not None, (
        "F-10 regression: the exposed wrapper dropped its return "
        "annotation again — dynamic tools would lose structuredContent"
    )
    sig_return = wrapper.__signature__.return_annotation
    assert sig_return is not inspect.Signature.empty


def test_exposed_wrapper_yields_output_schema() -> None:
    """The SDK gate this fix targets: func_metadata(wrapper) must
    produce a non-None output_schema (parity with decorator tools)."""
    wrapper = _build_exposed_wrapper(_fake_entry(), _Input, _Output)
    metadata = func_metadata(wrapper)
    assert metadata.output_schema is not None, (
        "F-10 regression: no output schema inferred — dynamically "
        "exposed script tools would answer text-only again"
    )


def test_exposed_wrapper_session_id_forwarding_preserved() -> None:
    """The annotation fix must not break the F2 session_id channel:
    the wrapper still accepts the Input-model fields as kwargs."""
    wrapper = _build_exposed_wrapper(_fake_entry(), _Input, _Output)
    sig = wrapper.__signature__
    assert "session_id" in sig.parameters
