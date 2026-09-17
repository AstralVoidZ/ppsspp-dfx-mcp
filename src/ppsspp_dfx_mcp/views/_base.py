"""FrozenModel base class for Pydantic v2 public JSON contracts.

All views MUST inherit from FrozenModel. `extra='forbid'` prevents
clients from injecting unexpected fields. `frozen=True` makes instances
hashable and immutable after construction.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class FrozenModel(BaseModel):
    """Base class for all MCP response views.

    - `frozen=True`: immutable + hashable (safe to cache / share across coroutines)
    - `extra='forbid'`: reject unknown fields (defensive contract)
    - `from_domain()`: opt-in classmethod hook for converting domain models
      to views. NOT an abstract method — most subclasses use a dedicated
      `from_<noun>()` factory (e.g. `from_session`, `from_result`) instead.
      Only override `from_domain` when a single generic entry point is
      desired (see `views/session.py:SessionResponse.from_domain` for the
      sole current override).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    @classmethod
    def from_domain(cls, data: Any) -> FrozenModel:
        """Opt-in hook: convert a domain model (frozen dataclass or dict) to a view.

        NOT an abstract method — subclasses are NOT required to override
        this. The default implementation handles dict + dataclass inputs;
        subclasses that need custom domain→view mapping typically expose a
        dedicated `from_<noun>()` factory (e.g. `from_session`,
        `from_result`) and may additionally override `from_domain` to
        dispatch to it (as `SessionResponse` does).

        Subclasses that override this method should accept the same input
        types (dict + their domain dataclass) and raise `TypeError` for
        anything else.
        """
        if isinstance(data, dict):
            return cls(**data)
        # If data is a dataclass, convert to dict via asdict().
        import dataclasses

        if dataclasses.is_dataclass(data):
            from dataclasses import asdict

            return cls(**asdict(data))
        raise TypeError(f"from_domain() cannot convert {type(data).__name__}")
