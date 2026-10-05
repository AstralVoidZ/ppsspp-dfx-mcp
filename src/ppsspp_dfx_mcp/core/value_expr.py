"""Numeric extraction from a ``cpu.evaluate`` response (W19).

Pure helper with no tool-layer dependency, so both ``tools/evaluate.py``
(the tool body) and ``tools/workflows.py`` (breakpoint conditions) can use
it without a local import that existed only to dodge an import cycle.
"""

from __future__ import annotations

from typing import Any

__all__ = ["extract_value"]


def extract_value(response: dict[str, Any] | None) -> int | None:
    """Best-effort numeric extraction from a cpu.evaluate response.

    PPSSPP's response shape varies; tolerate 'value' / 'result' / 'int' /
    'uintValue' keys, each as int or numeric str. Return None if no
    numeric value is found.
    """
    if not isinstance(response, dict):
        return None
    for key in ("value", "result", "int", "uintValue"):
        raw = response.get(key)
        if isinstance(raw, bool):
            # bool is a subclass of int; skip to avoid surprises.
            continue
        if isinstance(raw, int):
            return raw
        if isinstance(raw, str):
            try:
                return int(raw, 0) if raw.startswith(("0x", "0X")) else int(raw)
            except ValueError:
                continue
    return None
