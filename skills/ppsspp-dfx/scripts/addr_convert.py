#!/usr/bin/env python3
"""Convert between IDA offsets and PPSSPP runtime addresses (offline).

runtime address = IDA offset + load base. The MCP tool
v0.1.6 un-tooled `ppsspp_convert_address`; this
script works without a running session and accepts any base, including one
read from `.ppsspp-dfx/config/addresses.yaml` (top_base.ppsspp).

Usage:
  python addr_convert.py 0x126DBC                    # IDA offset -> runtime (base auto-discovered)
  python addr_convert.py 0x1AEDBBC --to ida          # runtime -> IDA offset
  python addr_convert.py 0x126DBC --base 0x08804000  # explicit base, no config lookup
  python addr_convert.py 0x126DBC 0x24594            # batch, one result per line

Addresses may carry a `0x` prefix or be bare hex (e.g. 126DBC); decimal
input is rejected — the bare-string-is-decimal trap this skill warns about.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

MARKER = Path(".ppsspp-dfx") / "config" / "addresses.yaml"
_TOP_BASE_RE = re.compile(r"^\s*ppsspp:\s*(0x[0-9A-Fa-f]+|\d+)\s*(?:#.*)?$")


def find_base() -> int | None:
    """Marker-upward search from CWD for addresses.yaml, parse top_base.ppsspp."""
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        config = parent / MARKER
        if not config.is_file():
            continue
        in_top_base = False
        for line in config.read_text(encoding="utf-8", errors="replace").splitlines():
            if re.match(r"^\s*top_base\s*:", line):
                in_top_base = True
                continue
            if in_top_base:
                match = _TOP_BASE_RE.match(line)
                if match:
                    return int(match.group(1), 0)
                if line.strip() and not line.startswith((" ", "\t", "#")):
                    in_top_base = False
    return None


def parse_addr(text: str) -> int:
    text = text.strip()
    if text.lower().startswith("0x"):
        return int(text, 16)
    if re.fullmatch(r"[0-9A-Fa-f]+", text):
        return int(text, 16)  # bare strings are read as hex on purpose; decimal input is the trap
    raise ValueError(f"{text!r}: use 0x-prefixed or bare hex; decimal input is rejected")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Offline IDA offset <-> PPSSPP runtime address conversion."
    )
    parser.add_argument("addresses", nargs="+", help="hex addresses (0x-prefixed or bare hex)")
    parser.add_argument(
        "--to",
        choices=("runtime", "ida"),
        default="runtime",
        help="direction of the input addresses (default: runtime)",
    )
    parser.add_argument(
        "--base",
        type=lambda s: int(s, 0),
        help="load base; default: top_base.ppsspp from .ppsspp-dfx/config/addresses.yaml",
    )
    args = parser.parse_args()

    base = args.base
    if base is None:
        base = find_base()
        if base is None:
            parser.error(
                f"no base given and {MARKER} (top_base.ppsspp) not found from CWD; "
                "pass --base 0x... explicitly"
            )
    direction = -1 if args.to == "ida" else 1

    print(f"# base=0x{base:08X} direction=input {args.to}")
    status = 0
    for text in args.addresses:
        try:
            value = parse_addr(text)
        except ValueError as exc:
            print(f"{text}: ERROR {exc}", file=sys.stderr)
            status = 1
            continue
        result = value + direction * base
        if not 0 <= result <= 0xFFFFFFFF:
            print(
                f"0x{value:08X} -> 0x{result & 0xFFFFFFFF:08X}  # WARNING: wrapped (input outside base range)"
            )
        else:
            print(f"0x{value:08X} -> 0x{result:08X}")
    return status


if __name__ == "__main__":
    sys.exit(main())
