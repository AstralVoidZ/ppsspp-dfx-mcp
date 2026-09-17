"""L4 regression tests for V023.

Violation:
- V023 [MEDIUM]: `CaptureService._output_screenshot` accessed
  `self._client._transport.call(...)` — crossing the encapsulation
  boundary of `PpssppDebugClient._transport` (a private attribute).
  See B.2 §4.1 for the problem statement.

Fix (B.2 §4.2, Phase C): `CaptureService.__init__` now takes an
explicit `transport: WsTransport` parameter (required, no default).
`_output_screenshot` uses `self._transport.call(...)` instead of
`self._client._transport.call(...)`. Callers (e.g. `session_capture`)
pass the transport explicitly via the new `session_client_with_transport`
context manager.

Anchors:
- L4 I15: `capture.py` source contains no `_client._transport` string
  (static source check — encapsulation violation eliminated).
- L4 I16: `PpssppDebugClient` exposes no method that calls
  `gpu.buffer.screenshot` (CRASH-RISK API remains scoped to
  CaptureService only).
- L4 I17: `CaptureService.__init__` signature requires `transport`
  parameter (covered by test_capture.py TestConstructor; duplicated
  here as an L4 anchor for the V023 spec invariant).

Note: I17 behavioral tests (transport required, constructor signature)
are duplicated here as L4 anchors; the V023 spec invariant is locked
to this file. This file focuses on the static source-level invariants
(I15/I16) that cannot be expressed as runtime behavior tests.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from ppsspp_dfx_mcp.service.capture import CaptureService
from ppsspp_dfx_mcp.service.debug_client import PpssppDebugClient


def _capture_source() -> str:
    """Read capture.py source text (for static invariant checks)."""
    capture_path = Path(inspect.getfile(CaptureService))
    return capture_path.read_text(encoding="utf-8")


def _debug_client_source() -> str:
    """Read debug_client.py source text (for static invariant checks)."""
    debug_client_path = Path(inspect.getfile(PpssppDebugClient))
    return debug_client_path.read_text(encoding="utf-8")


def _code_lines(source: str) -> list[tuple[int, str]]:
    """Return (line_no, line) pairs excluding docstrings and comment-only lines.

    Uses a triple-quote counter to track docstring state across lines:
    - A line is "inside a docstring" if the cumulative triple-quote
      count before it is odd.
    - Lines inside a docstring are skipped.
    - Lines that open AND close a docstring on the same line (e.g. a
      single-line docstring) are also skipped.
    - Comment-only lines (starting with '#' after stripping) are skipped.

    This is a conservative heuristic — it may over-skip (e.g. a line
    that happens to contain triple-quotes inside a regular string), but
    for the V023 invariant checks (which look for specific identifiers
    like `_client._transport`), over-skipping is safe.
    """
    code_lines: list[tuple[int, str]] = []
    triple_count = 0  # Cumulative triple-quote count seen so far
    for line_no, line in enumerate(source.splitlines(), start=1):
        starts_in_docstring = triple_count % 2 == 1
        line_triple_count = line.count('"""') + line.count("'''")

        if starts_in_docstring:
            # Line is inside a multi-line docstring — skip it
            triple_count += line_triple_count
            continue

        if line_triple_count > 0:
            # Line opens (and maybe closes) a docstring — skip it
            triple_count += line_triple_count
            continue

        # Not in a docstring, no triple-quotes → check if comment-only
        stripped = line.strip()
        if stripped.startswith("#"):
            continue

        code_lines.append((line_no, line))
    return code_lines


class TestV023CaptureEncapsulation:
    """V023: CaptureService encapsulation boundary invariants (I15/I16/I17)."""

    def test_capture_no_private_transport_access(self):
        """L4 I15: capture.py contains no `_client._transport` string in code.

        V023 fix: `_output_screenshot` now uses `self._transport.call(...)`
        (explicitly injected) instead of `self._client._transport.call(...)`
        (encapsulation violation). This static check ensures the violation
        does not regress.

        The string `_client._transport` may legitimately appear in
        docstrings/comments describing the V023 fix — those references
        describe what NOT to do, so they're allowed. The check below
        specifically excludes lines that are comments or docstrings.
        """
        source = _capture_source()
        violations: list[str] = []
        for line_no, line in _code_lines(source):
            if "_client._transport" in line:
                violations.append(f"line {line_no}: {line.rstrip()}")
        assert not violations, (
            "V023 I15 violation: capture.py contains `_client._transport` "
            "in code (not comments/docstrings):\n  " + "\n  ".join(violations)
        )

    def test_debug_client_does_not_expose_screenshot_output(self):
        """L4 I16: PpssppDebugClient has no method that calls gpu.buffer.screenshot.

        V023 I16 (B.2 §4.4): the CRASH-RISK `gpu.buffer.screenshot` API
        must remain scoped to `CaptureService._output_screenshot` — it
        must NOT be exposed as a method on `PpssppDebugClient`. Otherwise
        all tools could call it, defeating the CRASH-RISK limitation.

        The existing debug_client.py has the string in docstrings only
        (explaining why it's NOT exposed). We exclude docstrings and
        verify no code line references the event.
        """
        source = _debug_client_source()
        violations: list[str] = []
        for line_no, line in _code_lines(source):
            if "gpu.buffer.screenshot" in line:
                violations.append(f"line {line_no}: {line.rstrip()}")
        assert not violations, (
            "V023 I16 violation: debug_client.py contains "
            "`gpu.buffer.screenshot` in code (not comments/docstrings):\n  "
            + "\n  ".join(violations)
        )

    def test_capture_constructor_transport_required(self):
        """L4 I17: CaptureService.__init__ requires transport parameter.

        V023 Phase C: `transport` has no default value. Passing only
        `client` must raise TypeError. (Runtime behavior test duplicated
        from test_capture.py for L4 anchor completeness.)
        """
        sig = inspect.signature(CaptureService.__init__)
        params = sig.parameters
        assert "transport" in params, (
            "CaptureService.__init__ missing `transport` parameter. "
            "V023 I17 requires explicit transport injection."
        )
        assert params["transport"].default is inspect.Parameter.empty, (
            f"V023 I17: `transport` must be required (no default). "
            f"Got default={params['transport'].default!r}"
        )

    def test_capture_uses_self_transport_in_output_screenshot(self):
        """L4 I15 (positive): `_output_screenshot` uses `self._transport.call(...)`.

        Complement to `test_capture_no_private_transport_access`: verifies
        the correct `self._transport` reference IS present in
        `_output_screenshot` source (otherwise the fix would be incomplete
        — the method would have no transport to call).
        """
        source = inspect.getsource(CaptureService._output_screenshot)
        assert "self._transport.call" in source, (
            "V023 I15: _output_screenshot should call "
            "`self._transport.call(...)` (explicit injection). "
            f"Source:\n{source}"
        )
