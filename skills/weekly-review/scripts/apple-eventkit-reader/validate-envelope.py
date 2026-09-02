#!/usr/bin/env python3
"""Strictly validate and normalize one native reader JSON envelope."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any


MAXIMUM_OUTPUT_BYTES = 4_194_304
READER_VERSION = "1.0.0"
PROTOCOL_VERSION = 1
INVALID_ENVELOPE_EXIT = 10


class DuplicateKeyError(ValueError):
    pass


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKeyError(key)
        result[key] = value
    return result


def reject_nonfinite(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("JSON exponent overflows a finite number")
    return parsed


def require_finite_numbers(value: Any) -> None:
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite parsed number")
        return
    if isinstance(value, dict):
        for child in value.values():
            require_finite_numbers(child)
        return
    if isinstance(value, list):
        for child in value:
            require_finite_numbers(child)


def nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def load_strict_json(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    if len(raw) > MAXIMUM_OUTPUT_BYTES:
        raise ValueError("input exceeds protocol limit")
    text = raw.decode("utf-8", errors="strict")
    value = json.loads(
        text,
        object_pairs_hook=unique_object,
        parse_constant=reject_nonfinite,
        parse_float=finite_float,
    )
    if not isinstance(value, dict):
        raise ValueError("top-level value is not an object")
    require_finite_numbers(value)
    return value


def validate_envelope(value: dict[str, Any], worker_status: int) -> int:
    if value.get("reader_version") != READER_VERSION:
        raise ValueError("reader version mismatch")
    protocol = value.get("protocol_version")
    if isinstance(protocol, bool) or protocol != PROTOCOL_VERSION:
        raise ValueError("protocol version mismatch")
    if value.get("eventkit_data_mutated") is not False:
        raise ValueError("unsafe mutation flag")

    ok = value.get("ok")
    if ok is True:
        if worker_status != 0 or not nonempty_string(value.get("command")):
            raise ValueError("success envelope/status mismatch")
        return 0
    if ok is False:
        error = value.get("error")
        if worker_status != 2 or not isinstance(error, dict):
            raise ValueError("failure envelope/status mismatch")
        if not nonempty_string(error.get("code")) or not nonempty_string(error.get("message")):
            raise ValueError("incomplete error object")
        if "details" in error and not isinstance(error["details"], dict):
            raise ValueError("error details must be an object")
        return 2
    raise ValueError("ok must be a boolean")


def main() -> int:
    if len(sys.argv) != 3:
        return INVALID_ENVELOPE_EXIT
    try:
        path = Path(sys.argv[1])
        worker_status = int(sys.argv[2], 10)
        if worker_status < 0 or worker_status > 255:
            raise ValueError("invalid worker status")
        value = load_strict_json(path)
        final_status = validate_envelope(value, worker_status)
        normalized = (
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
        if len(normalized) > MAXIMUM_OUTPUT_BYTES:
            raise ValueError("normalized output exceeds protocol limit")
        sys.stdout.buffer.write(normalized)
        return final_status
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return INVALID_ENVELOPE_EXIT


if __name__ == "__main__":
    raise SystemExit(main())
