"""Containment lock for `resolve_output_path` (v0.1.7 review C-10 — withdrawn).

Review claim C-10 said a client-supplied file name could escape the output tree
(`Path(base)/".."`, or an absolute child overriding the base). Measurement
against the SHIPPED function rejects every such vector:

    REJECTED 'memory_reads' '../../../etc/passwd'
    REJECTED 'memory_reads' '..\\..\\..\\Windows\\System32\\evil.bin'
    REJECTED 'memory_reads' 'C:\\Windows\\System32\\evil.bin'
    REJECTED 'memory_reads' '/etc/passwd'

The claim came from a probe that modelled a naive join instead of calling the
function, so it is withdrawn — but the invariant it described had no direct
test (existing coverage only ever passes a legal name, e.g.
`resolve_output_path("replays", "rec.ppr")`). This file pins the rejection
behaviour so the guard cannot be refactored away unnoticed.

The two checks that implement it:
  1. `Path(filename).name != filename` — no directory parts, no absolute path,
     no `..` component (it makes `..` appear as a path part).
  2. resolved-path containment — defence in depth for separators the first
     check might miss on some platform.
"""

from __future__ import annotations

import pytest

from ppsspp_dfx_mcp.errors import ArgsInvalid
from ppsspp_dfx_mcp.tools._common import resolve_output_path

ESCAPES = [
    "../../../etc/passwd",
    "..\\..\\..\\Windows\\System32\\evil.bin",
    "C:\\Windows\\System32\\evil.bin",
    "/etc/passwd",
    "sub/child.bin",
    "..",
    "",
]


class TestClientFileNameCannotEscape:
    @pytest.mark.parametrize("filename", ESCAPES)
    def test_escape_attempts_are_rejected(self, filename: str) -> None:
        with pytest.raises(ArgsInvalid):
            resolve_output_path("memory_reads", filename)

    def test_legal_name_lands_under_the_output_tree(self) -> None:
        """Control: the guard must not reject ordinary names."""
        path = resolve_output_path("memory_reads", "ok.bin")
        assert path.name == "ok.bin"
        assert "memory_reads" in path.parts

    def test_the_server_chooses_the_directory(self) -> None:
        """Different subdirs stay siblings — the name never re-roots the path."""
        a = resolve_output_path("memory_reads", "x.bin")
        b = resolve_output_path("replays", "x.bin")
        assert a.parent != b.parent
        assert a.name == b.name == "x.bin"
