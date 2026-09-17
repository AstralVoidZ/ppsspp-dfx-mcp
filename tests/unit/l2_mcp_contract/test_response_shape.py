"""R3 (design_ppsspp_dfx_mcp_test_refactor_v1 §R3): response-shape contract.

Locks the field names/types of the MCP-facing response views so that a
rename in ``views/`` or ``models/`` turns tests red immediately.

Regression anchor: verification report F-10/F-12 and the R1 harness
iterations — several validators broke because response keys drifted
(ranges vs regions, pc vs value, value as byte list) with nothing
guarding the contract. These are pure metadata tests (no transport).
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from ppsspp_dfx_mcp.views.analyze import AddressConversionResponse
from ppsspp_dfx_mcp.views.batch_step import BatchStepResponse
from ppsspp_dfx_mcp.views.breakpoint import BreakpointResponse
from ppsspp_dfx_mcp.views.memory import (
    MemoryReadResponse,
    MemoryWriteResponse,
)
from ppsspp_dfx_mcp.views.query import GetPcResponse
from ppsspp_dfx_mcp.views.session import SessionListResponse, SessionResponse

pytestmark = pytest.mark.asyncio

# view model → required field names (golden shape)
GOLDEN_SHAPES: list[tuple[type[BaseModel], set[str]]] = [
    (
        SessionResponse,
        {
            "session_id",
            "iso_path",
            "pid",
            "ws_url",
            "created_at",
            "last_active_at",
            "exec_count",
            "ws_connected",
            "recovered",
            "restored",
            "ppsspp_version",
        },
    ),
    (GetPcResponse, {"pc", "trust_level"}),
    # The seven subset-contract models (MemoryRead/MemoryWrite/
    # AddressConversion/Breakpoint/BatchStep/Query/SessionList responses)
    # deliberately have NO exact-shape entry here — they are covered by
    # REQUIRED_FIELDS below. Empty-field placeholder rows used to become
    # permanently-skipping test cases.
]

# view model → fields that MUST exist (subset contract for aggregate views)
REQUIRED_FIELDS: list[tuple[type[BaseModel], set[str]]] = [
    (MemoryReadResponse, {"action", "address", "value", "size"}),
    (MemoryWriteResponse, {"address", "format", "bytes_written"}),
    (
        AddressConversionResponse,
        {"original", "converted", "mode", "top_base_ppsspp", "top_base_ida"},
    ),
    (BreakpointResponse, {"action", "address", "breakpoints"}),
    (
        BatchStepResponse,
        {"action", "total", "executed", "succeeded", "failed", "skipped", "results"},
    ),
    (SessionListResponse, {"sessions", "count"}),
]


class TestResponseShapeContract:
    @pytest.mark.parametrize(
        ("model", "fields"),
        GOLDEN_SHAPES,
        ids=lambda v: v if isinstance(v, str) else "",
    )
    async def test_exact_shape(self, model: type[BaseModel], fields: set[str]) -> None:
        assert set(model.model_fields) == fields, (
            f"{model.__name__} field contract drifted — R3 response-shape "
            f"regression; update views + this golden table together"
        )

    @pytest.mark.parametrize(
        ("model", "fields"),
        REQUIRED_FIELDS,
    )
    async def test_required_fields_present(self, model: type[BaseModel], fields: set[str]) -> None:
        missing = fields - set(model.model_fields)
        assert not missing, (
            f"{model.__name__} lost required fields {sorted(missing)} — "
            f"R3 response-shape regression"
        )
