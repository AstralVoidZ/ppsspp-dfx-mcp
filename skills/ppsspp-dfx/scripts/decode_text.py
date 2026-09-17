#!/usr/bin/env python3
"""Decode multi-byte text captured from PSP game memory.

Pairs with `ppsspp_read_memory(action="read_bytes")`: feed the returned hex
bytes here instead of hand-decoding, so encoding mistakes surface as visible
replacement characters rather than silent garbage.

Usage:
  python decode_text.py <hexbytes> [--encoding auto]
  python decode_text.py --file DUMP.BIN --offset 0x480 --size 128
  ... | python decode_text.py -

`<hexbytes>` is a hex string, `0x` prefix and spaces tolerated.
`--encoding auto` (default) tries utf-8 / shift_jis / gbk and reports each;
a named encoding decodes with errors="replace".
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ENCODINGS = ("utf-8", "shift_jis", "gbk")


def _parse_hex(text: str) -> bytes:
    cleaned = text.strip().replace("0x", "").replace("0X", "").replace(" ", "").replace("\n", "")
    if not cleaned or len(cleaned) % 2:
        raise ValueError(f"hex bytes must have non-zero even length, got {len(cleaned)} chars")
    return bytes.fromhex(cleaned)


def _read_source(args: argparse.Namespace) -> bytes:
    if args.hexbytes == "-":
        return sys.stdin.buffer.read()
    if args.hexbytes:
        return _parse_hex(args.hexbytes)
    if args.file:
        data = Path(args.file).read_bytes()
        start = args.offset if args.offset is not None else 0
        end = start + args.size if args.size is not None else None
        return data[start:end]
    raise ValueError("provide hex bytes, '-' for stdin, or --file/--offset/--size")


def _decode_strict(data: bytes, enc: str) -> str | None:
    try:
        return data.decode(enc)
    except UnicodeDecodeError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Decode multi-byte text from PSP memory captures (hex / file / stdin)."
    )
    parser.add_argument(
        "hexbytes",
        nargs="?",
        default="",
        help="hex string (0x prefix and spaces tolerated), or '-' for stdin",
    )
    parser.add_argument("--file", help="read raw bytes from this file instead")
    parser.add_argument("--offset", type=lambda s: int(s, 0), help="byte offset into --file")
    parser.add_argument("--size", type=lambda s: int(s, 0), help="byte count to read from --file")
    parser.add_argument(
        "--encoding", default="auto", help="auto (default) | utf-8 | shift_jis | gbk"
    )
    args = parser.parse_args()

    try:
        data = _read_source(args)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))

    if not data:
        print("no bytes to decode", file=sys.stderr)
        return 1

    if args.encoding != "auto":
        if args.encoding not in ENCODINGS:
            parser.error(
                f"unsupported encoding {args.encoding!r}; choose from: auto, {', '.join(ENCODINGS)}"
            )
        text = data.decode(args.encoding, errors="replace")
        print(f"[{args.encoding}, len={len(data)}B]")
        print(text)
        return 0

    clean = [enc for enc in ENCODINGS if _decode_strict(data, enc) is not None]
    for enc in ENCODINGS:
        strict = _decode_strict(data, enc)
        marker = "clean" if strict is not None else "lossy"
        text = strict if strict is not None else data.decode(enc, errors="replace")
        print(f"[{enc}, {marker}, len={len(data)}B]")
        print(text)
    if len(clean) == 1:
        print(f"# decoded cleanly as {clean[0]}")
    elif len(clean) > 1:
        # shift_jis covers the half-width kana range, making almost any byte
        # stream "valid" — a clean decode under several encodings is ambiguous.
        print(
            f"# AMBIGUOUS: decodes cleanly under {', '.join(clean)}; "
            "pick the game's actual encoding with --encoding"
        )
    else:
        print(
            "# no encoding decoded cleanly; lines above are best-effort (replacement chars mark bad bytes)"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
