#!/usr/bin/env python3
"""Read-only, allowlist-only Mac file activity collector.

Transport is one JSON object on stdin and one JSON object on stdout. The
collector keeps verified directory descriptors open from validation through
enumeration, never follows symlinks, and reads content only for an explicitly
requested local-file hash.
"""

from __future__ import annotations

import datetime as dt
import errno
import fnmatch
import hashlib
import json
import mimetypes
import os
import pwd
import stat
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple


COLLECTOR_VERSION = "1.1.0"
PROTOCOL_VERSION = 1
OPERATION = "scan"

MAX_STDIN_BYTES = 1_048_576
DEFAULT_MAX_VISITED_ENTRIES = 100_000
DEFAULT_MAX_CANDIDATES = 5_000
DEFAULT_HASH_MAX_BYTES = 10 * 1_048_576
DEFAULT_HASH_TOTAL_MAX_BYTES = 50 * 1_048_576
DEFAULT_DEADLINE_MS = 5_000
DEFAULT_MAX_OUTPUT_BYTES = 1_048_576
MIN_MAX_OUTPUT_BYTES = 1_024
MAX_MAX_OUTPUT_BYTES = 16 * 1_048_576
MAX_WINDOW_SECONDS = 8 * 24 * 60 * 60
HASH_CHUNK_BYTES = 64 * 1_024

FD_RELATIVE_PRIMITIVES_SUPPORTED = bool(
    os.open in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.stat in os.supports_follow_symlinks
    and os.scandir in os.supports_fd
)

# Darwin's Python does not currently expose SF_DATALESS even though the flag is
# part of sys/stat.h. Keep the documented platform value local to this detector.
SF_DATALESS = 0x40000000

EXCLUDED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".cache",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "caches",
        "coverage",
        "deriveddata",
        "dist",
        "node_modules",
        "out",
        "target",
        "venv",
    }
)

SENSITIVE_SUFFIXES = frozenset(
    {
        ".cer",
        ".crt",
        ".der",
        ".jks",
        ".key",
        ".kdbx",
        ".keystore",
        ".keychain",
        ".keychain-db",
        ".mobileprovision",
        ".ovpn",
        ".p12",
        ".pem",
        ".pfx",
        ".ppk",
    }
)

SENSITIVE_FILENAMES = frozenset(
    {
        "credentials.json",
        "client_secret.json",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
        "oauth.json",
        "secrets.json",
        "token.json",
        "tokens.json",
    }
)

SENSITIVE_ROOT_COMPONENTS = frozenset(
    {
        ".trash",
        ".trashes",
        "accounts",
        "cookies",
        "group containers",
        "identityservices",
        "keychains",
        "mail",
        "messages",
        "metadata",
        "mobile documents",
        "safari",
        "trash",
    }
)

# CloudStorage is the sole Library exception. Provider roots remain too broad,
# and an explicit descendant beneath a recognized provider is required.
CLOUD_PROVIDER_EXACT = frozenset({"box", "dropbox", "icloud drive"})
CLOUD_PROVIDER_PREFIXES = ("box-", "dropbox-", "googledrive-", "onedrive-")


class ContractError(Exception):
    """A stable, user-correctable request failure."""

    def __init__(
        self, code: str, message: str, details: Optional[Dict[str, Any]] = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def _base_output(ok: bool) -> Dict[str, Any]:
    return {
        "collector_version": COLLECTOR_VERSION,
        "ok": ok,
        "protocol_version": PROTOCOL_VERSION,
    }


def _encoded_payload(payload: Dict[str, Any]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def _emit(payload: Dict[str, Any], max_bytes: int = DEFAULT_MAX_OUTPUT_BYTES) -> None:
    encoded = _encoded_payload(payload)
    if len(encoded) > max_bytes:
        # Successful responses are fitted before this point. This fallback
        # keeps adversarial validation-error details within the CLI cap too.
        compact = _base_output(False)
        compact["error"] = {
            "code": "output_limit_exceeded",
            "message": "The response exceeded the collector output limit.",
        }
        encoded = _encoded_payload(compact)
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def _emit_failure(failure: ContractError) -> None:
    payload = _base_output(False)
    error: Dict[str, Any] = {
        "code": failure.code,
        "message": failure.message,
    }
    if failure.details:
        error["details"] = failure.details
    payload["error"] = error
    _emit(payload)


def _ensure_keys(
    value: Dict[str, Any], allowed: Iterable[str], context: str
) -> None:
    unknown = sorted(set(value).difference(allowed))
    if unknown:
        raise ContractError(
            "validation_error",
            f"Unknown key(s) in {context}.",
            {"key_count": len(unknown), "keys": unknown[:20]},
        )


def _require_bool(value: Dict[str, Any], key: str, default: bool) -> bool:
    if key not in value:
        return default
    result = value[key]
    if type(result) is not bool:
        raise ContractError(
            "validation_error", f"options.{key} must be a boolean."
        )
    return result


def _require_int(
    value: Dict[str, Any],
    key: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    if key not in value:
        return default
    result = value[key]
    if type(result) is not int or not minimum <= result <= maximum:
        raise ContractError(
            "validation_error",
            f"options.{key} must be an integer from {minimum} through {maximum}.",
        )
    return result


def _parse_timestamp(value: Any, field: str) -> dt.datetime:
    if not isinstance(value, str) or not value:
        raise ContractError(
            "validation_error", f"window.{field} must be a non-empty ISO 8601 string."
        )
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ContractError(
            "validation_error",
            f"window.{field} must be a valid ISO 8601 timestamp.",
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(
            "validation_error",
            f"window.{field} must include an explicit UTC offset.",
        )
    return parsed


def _read_request() -> Dict[str, Any]:
    raw = sys.stdin.buffer.read(MAX_STDIN_BYTES + 1)
    if len(raw) > MAX_STDIN_BYTES:
        raise ContractError(
            "invalid_json", f"stdin must not exceed {MAX_STDIN_BYTES} bytes."
        )
    if not raw.strip():
        raise ContractError("invalid_json", "A JSON object is required on stdin.")
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ContractError("invalid_json", "stdin is not valid JSON.") from exc
    if not isinstance(decoded, dict):
        raise ContractError("invalid_json", "stdin must contain one JSON object.")
    return decoded


def _is_within(path: str, parent: str) -> bool:
    try:
        return os.path.commonpath((path, parent)) == parent
    except ValueError:
        return False


def _matches_glob(relative_path: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatch(relative_path, pattern) for pattern in patterns)


def _directory_exclusion(
    name: str, relative_path: str, patterns: Sequence[str]
) -> Optional[str]:
    if name.startswith("."):
        return "hidden_directory"
    if name.casefold() in EXCLUDED_DIRECTORY_NAMES:
        return "excluded_directory_name"
    if _matches_glob(relative_path, patterns):
        return "custom_glob"
    return None


def _file_exclusion(
    name: str, relative_path: str, patterns: Sequence[str]
) -> Optional[str]:
    lower_name = name.casefold()
    if name.startswith("."):
        return "hidden_file"
    if lower_name in SENSITIVE_FILENAMES or Path(lower_name).suffix in SENSITIVE_SUFFIXES:
        return "sensitive_file_type"
    if lower_name.startswith(".env") or "secret" in lower_name:
        return "sensitive_file_type"
    if _matches_glob(relative_path, patterns):
        return "custom_glob"
    return None


def _root_component_is_forbidden(name: str) -> bool:
    folded = name.casefold()
    return bool(
        not name
        or name.startswith(".")
        or folded in EXCLUDED_DIRECTORY_NAMES
        or folded in SENSITIVE_ROOT_COMPONENTS
        or folded in SENSITIVE_FILENAMES
        or Path(folded).suffix in SENSITIVE_SUFFIXES
        or folded.startswith(".env")
        or "secret" in folded
    )


def _supported_cloud_provider(name: str) -> bool:
    folded = name.casefold()
    return folded in CLOUD_PROVIDER_EXACT or any(
        folded.startswith(prefix) for prefix in CLOUD_PROVIDER_PREFIXES
    )


def _classify_root_parts(
    parts: Sequence[str], patterns: Sequence[str], root_index: int
) -> str:
    details = {"root_alias": f"root_{root_index}", "root_index": root_index}
    if not parts:
        raise ContractError(
            "unsafe_root",
            "The current-user home itself is too broad; choose a specific work folder.",
            details,
        )

    if parts[0].casefold() == "library":
        if (
            len(parts) < 4
            or parts[1].casefold() != "cloudstorage"
            or not _supported_cloud_provider(parts[2])
        ):
            raise ContractError(
                "unsafe_root",
                "Library roots are forbidden except for a specific descendant of a supported CloudStorage provider.",
                details,
            )
        selected_parts = parts[3:]
        if any(_root_component_is_forbidden(part) for part in selected_parts):
            raise ContractError(
                "unsafe_root",
                "The selected CloudStorage root contains a hidden, excluded, or sensitive component.",
                details,
            )
        if _matches_glob("/".join(selected_parts), patterns):
            raise ContractError(
                "unsafe_root",
                "The selected root is excluded by the request's exclusion policy.",
                details,
            )
        return "cloudstorage"

    if any(_root_component_is_forbidden(part) for part in parts):
        raise ContractError(
            "unsafe_root",
            "The selected root contains a hidden, excluded, Trash, or sensitive component.",
            details,
        )
    if _matches_glob("/".join(parts), patterns):
        raise ContractError(
            "unsafe_root",
            "The selected root is excluded by the request's exclusion policy.",
            details,
        )
    return "local"


def _current_user_home() -> str:
    """Return the account database home, not an attacker-controlled HOME value."""

    try:
        home = os.path.normpath(pwd.getpwuid(os.getuid()).pw_dir)
    except (KeyError, OSError) as exc:
        raise ContractError(
            "unsupported_environment",
            "The current user's home directory could not be established safely.",
        ) from exc
    if not os.path.isabs(home) or home == os.sep:
        raise ContractError(
            "unsupported_environment",
            "The current user's home directory is not a safe absolute descendant.",
        )
    return home


def _directory_open_flags() -> int:
    no_follow = int(getattr(os, "O_NOFOLLOW", 0))
    directory = int(getattr(os, "O_DIRECTORY", 0))
    if (
        not no_follow
        or not directory
        or not FD_RELATIVE_PRIMITIVES_SUPPORTED
    ):
        raise ContractError(
            "unsupported_environment",
            "This platform lacks the descriptor-relative no-follow primitives required for a safe scan.",
        )
    return (
        os.O_RDONLY
        | no_follow
        | directory
        | int(getattr(os, "O_CLOEXEC", 0))
    )


def _check_deadline(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise ContractError(
            "deadline_exceeded",
            "The request deadline expired before safe root validation completed.",
        )


def _open_absolute_directory(path: str, deadline: float) -> int:
    """Open every component without following a symlink and return the final FD."""

    flags = _directory_open_flags()
    descriptor = -1
    try:
        _check_deadline(deadline)
        descriptor = os.open(os.sep, flags)
        for component in Path(path).parts[1:]:
            _check_deadline(deadline)
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
            opened = os.fstat(descriptor)
            if not stat.S_ISDIR(opened.st_mode):
                raise OSError(errno.ENOTDIR, "not a directory")
        _check_deadline(deadline)
        return descriptor
    except ContractError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        raise ContractError(
            "unsupported_environment",
            "The current user's home could not be opened without following symlinks.",
        ) from exc


def _root_error(root_index: int, message: str) -> ContractError:
    return ContractError(
        "unsafe_root",
        message,
        {"root_alias": f"root_{root_index}", "root_index": root_index},
    )


def _validate_root(
    raw_root: Any,
    root_index: int,
    home: str,
    home_fd: int,
    home_stat: os.stat_result,
    patterns: Sequence[str],
    deadline: float,
) -> Dict[str, Any]:
    if not isinstance(raw_root, str) or not raw_root or "\x00" in raw_root:
        raise ContractError(
            "validation_error",
            "Each roots entry must be a non-empty path string.",
            {"root_alias": f"root_{root_index}", "root_index": root_index},
        )
    if not os.path.isabs(raw_root):
        raise _root_error(root_index, "Every allowlisted root must be an absolute path.")

    normalized = os.path.normpath(raw_root)
    if normalized != raw_root:
        raise _root_error(
            root_index,
            "An allowlisted root must already be lexically normalized.",
        )
    if normalized == home or not _is_within(normalized, home):
        raise _root_error(
            root_index,
            "A root must be a specific safe descendant of the current user's home; system roots, other users, external volumes, and the broad home are forbidden.",
        )

    relative = os.path.relpath(normalized, home)
    parts = Path(relative).parts
    kind = _classify_root_parts(parts, patterns, root_index)

    descriptor = os.dup(home_fd)
    try:
        flags = _directory_open_flags()
        for component in parts:
            _check_deadline(deadline)
            try:
                next_descriptor = os.open(component, flags, dir_fd=descriptor)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    message = "An allowlisted root must contain no symlink components."
                elif exc.errno == errno.ENOENT:
                    message = "An allowlisted root does not exist."
                else:
                    message = "An allowlisted root cannot be opened safely."
                raise _root_error(root_index, message) from exc
            os.close(descriptor)
            descriptor = next_descriptor
            opened = os.fstat(descriptor)
            if not stat.S_ISDIR(opened.st_mode):
                raise _root_error(root_index, "Every allowlisted root must be a real directory.")
            if opened.st_dev != home_stat.st_dev:
                raise _root_error(
                    root_index,
                    "Mounted or cross-device roots are not supported; choose a local work folder.",
                )
        _check_deadline(deadline)
        root_stat = os.fstat(descriptor)
        return {
            "alias": f"root_{root_index}",
            "fd": descriptor,
            "index": root_index,
            "kind": kind,
            "path": normalized,
            "st_dev": root_stat.st_dev,
            "st_ino": root_stat.st_ino,
        }
    except Exception:
        os.close(descriptor)
        raise


def _validate_non_overlapping_roots(roots: Sequence[Dict[str, Any]]) -> None:
    ordered = sorted(
        roots,
        key=lambda item: (len(Path(item["path"]).parts), item["path"]),
    )
    seen_identities: Dict[Tuple[int, int], int] = {}
    for root in ordered:
        identity = (root["st_dev"], root["st_ino"])
        if identity in seen_identities:
            raise ContractError(
                "unsafe_root",
                "Allowlisted roots must identify distinct directories.",
                {"root_indices": sorted([seen_identities[identity], root["index"]])},
            )
        seen_identities[identity] = root["index"]

    for index, parent in enumerate(ordered):
        for child in ordered[index + 1 :]:
            if _is_within(child["path"], parent["path"]):
                raise ContractError(
                    "unsafe_root",
                    "Allowlisted roots must be distinct and non-overlapping.",
                    {"root_indices": sorted([parent["index"], child["index"]])},
                )


def _parse_options(raw: Any) -> Dict[str, Any]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ContractError("validation_error", "options must be an object.")
    _ensure_keys(
        raw,
        {
            "deadline_ms",
            "exclude_globs",
            "hash_algorithm",
            "hash_max_bytes",
            "hash_total_max_bytes",
            "include_birthtime",
            "include_hash",
            "include_mtime",
            "include_size",
            "include_type",
            "max_candidates",
            "max_output_bytes",
            "max_visited_entries",
        },
        "options",
    )

    include_birthtime = _require_bool(raw, "include_birthtime", True)
    include_mtime = _require_bool(raw, "include_mtime", True)
    if not include_birthtime and not include_mtime:
        raise ContractError(
            "validation_error",
            "At least one of options.include_birthtime and options.include_mtime must be true.",
        )

    exclude_globs = raw.get("exclude_globs", [])
    if (
        not isinstance(exclude_globs, list)
        or len(exclude_globs) > 100
        or any(
            not isinstance(pattern, str)
            or not pattern
            or len(pattern.encode("utf-8")) > 256
            for pattern in exclude_globs
        )
    ):
        raise ContractError(
            "validation_error",
            "options.exclude_globs must be an array of at most 100 non-empty strings, each at most 256 UTF-8 bytes.",
        )

    hash_algorithm = raw.get("hash_algorithm", "sha256")
    if hash_algorithm != "sha256":
        raise ContractError(
            "validation_error", "options.hash_algorithm currently supports only 'sha256'."
        )

    return {
        "deadline_ms": _require_int(
            raw, "deadline_ms", DEFAULT_DEADLINE_MS, 1, 60_000
        ),
        "exclude_globs": exclude_globs,
        "hash_algorithm": hash_algorithm,
        "hash_max_bytes": _require_int(
            raw, "hash_max_bytes", DEFAULT_HASH_MAX_BYTES, 0, 1_073_741_824
        ),
        "hash_total_max_bytes": _require_int(
            raw,
            "hash_total_max_bytes",
            DEFAULT_HASH_TOTAL_MAX_BYTES,
            0,
            1_073_741_824,
        ),
        "include_birthtime": include_birthtime,
        "include_hash": _require_bool(raw, "include_hash", False),
        "include_mtime": include_mtime,
        "include_size": _require_bool(raw, "include_size", True),
        "include_type": _require_bool(raw, "include_type", True),
        "max_candidates": _require_int(
            raw, "max_candidates", DEFAULT_MAX_CANDIDATES, 1, 100_000
        ),
        "max_output_bytes": _require_int(
            raw,
            "max_output_bytes",
            DEFAULT_MAX_OUTPUT_BYTES,
            MIN_MAX_OUTPUT_BYTES,
            MAX_MAX_OUTPUT_BYTES,
        ),
        "max_visited_entries": _require_int(
            raw,
            "max_visited_entries",
            DEFAULT_MAX_VISITED_ENTRIES,
            1,
            1_000_000,
        ),
    }


def _validate_exact_relative_path(value: Any, item_index: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or len(value.encode("utf-8")) > 4_096
        or os.path.isabs(value)
        or os.path.normpath(value) != value
        or value == "."
        or any(part in {"", ".", ".."} for part in Path(value).parts)
    ):
        raise ContractError(
            "validation_error",
            "Each cloud_hash_allowlist relative_path must be one exact normalized relative file path.",
            {"allowlist_index": item_index},
        )
    return value


def _parse_cloud_hash_allowlist(
    raw: Any, roots: Sequence[Dict[str, Any]], include_hash: bool
) -> Set[Tuple[int, str]]:
    if raw is None:
        raw = []
    if not isinstance(raw, list) or len(raw) > 1_000:
        raise ContractError(
            "validation_error",
            "cloud_hash_allowlist must be an array of at most 1000 exact-file entries.",
        )
    if raw and not include_hash:
        raise ContractError(
            "validation_error",
            "cloud_hash_allowlist requires options.include_hash=true.",
        )

    result: Set[Tuple[int, str]] = set()
    for item_index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ContractError(
                "validation_error",
                "Each cloud_hash_allowlist entry must be an object.",
                {"allowlist_index": item_index},
            )
        _ensure_keys(item, {"relative_path", "root_index"}, "cloud_hash_allowlist entry")
        root_index = item.get("root_index")
        if type(root_index) is not int or not 0 <= root_index < len(roots):
            raise ContractError(
                "validation_error",
                "Each cloud_hash_allowlist root_index must select a request root.",
                {"allowlist_index": item_index},
            )
        if roots[root_index]["kind"] != "cloudstorage":
            raise ContractError(
                "validation_error",
                "cloud_hash_allowlist entries may reference only CloudStorage roots.",
                {"allowlist_index": item_index, "root_index": root_index},
            )
        relative_path = _validate_exact_relative_path(item.get("relative_path"), item_index)
        key = (root_index, relative_path)
        if key in result:
            raise ContractError(
                "validation_error",
                "cloud_hash_allowlist must not contain duplicate exact-file entries.",
                {"allowlist_index": item_index, "root_index": root_index},
            )
        result.add(key)
    return result


def _parse_request(request: Dict[str, Any]) -> Dict[str, Any]:
    started = time.monotonic()
    _ensure_keys(
        request,
        {
            "cloud_hash_allowlist",
            "operation",
            "options",
            "protocol_version",
            "request_id",
            "roots",
            "window",
        },
        "request",
    )
    if request.get("protocol_version") != PROTOCOL_VERSION:
        raise ContractError(
            "unsupported_schema", f"protocol_version must be {PROTOCOL_VERSION}."
        )
    if request.get("operation") != OPERATION:
        raise ContractError("validation_error", f"operation must be '{OPERATION}'.")

    request_id = request.get("request_id")
    if request_id is not None and (
        not isinstance(request_id, str)
        or not request_id
        or len(request_id.encode("utf-8")) > 128
    ):
        raise ContractError(
            "validation_error",
            "request_id, when present, must be a non-empty string of at most 128 UTF-8 bytes.",
        )

    options = _parse_options(request.get("options"))
    deadline = started + options["deadline_ms"] / 1_000

    window = request.get("window")
    if not isinstance(window, dict):
        raise ContractError("validation_error", "window must be an object.")
    _ensure_keys(window, {"end", "start"}, "window")
    start = _parse_timestamp(window.get("start"), "start")
    end = _parse_timestamp(window.get("end"), "end")
    if end <= start:
        raise ContractError("validation_error", "window.end must be later than window.start.")
    if (end - start).total_seconds() > MAX_WINDOW_SECONDS:
        raise ContractError(
            "validation_error", "The half-open review window must not exceed eight days."
        )

    raw_roots = request.get("roots")
    if not isinstance(raw_roots, list) or not 1 <= len(raw_roots) <= 32:
        raise ContractError(
            "validation_error", "roots must contain from 1 through 32 allowlisted directories."
        )

    home = _current_user_home()
    home_fd = _open_absolute_directory(home, deadline)
    roots: List[Dict[str, Any]] = []
    try:
        home_stat = os.fstat(home_fd)
        for root_index, item in enumerate(raw_roots):
            roots.append(
                _validate_root(
                    item,
                    root_index,
                    home,
                    home_fd,
                    home_stat,
                    options["exclude_globs"],
                    deadline,
                )
            )
        _validate_non_overlapping_roots(roots)
        cloud_hash_allowlist = _parse_cloud_hash_allowlist(
            request.get("cloud_hash_allowlist"), roots, options["include_hash"]
        )
        _check_deadline(deadline)
    except Exception:
        for root in roots:
            os.close(root["fd"])
        raise
    finally:
        os.close(home_fd)

    return {
        "cloud_hash_allowlist": cloud_hash_allowlist,
        "deadline": deadline,
        "end": end,
        "options": options,
        "request_id": request_id,
        "roots": roots,
        "start": start,
        "started": started,
    }


def _close_parsed_roots(parsed: Optional[Dict[str, Any]]) -> None:
    if not parsed:
        return
    for root in parsed.get("roots", []):
        descriptor = root.get("fd", -1)
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
            root["fd"] = -1


def _cloud_placeholder_reason(name: str, file_stat: os.stat_result) -> Optional[str]:
    if name.startswith(".") and name.casefold().endswith(".icloud"):
        return "cloud_placeholder_stub"
    flags = int(getattr(file_stat, "st_flags", 0))
    if flags & SF_DATALESS:
        return "dataless_flag"
    attributes = int(getattr(file_stat, "st_file_attributes", 0))
    offline_flag = int(getattr(stat, "FILE_ATTRIBUTE_OFFLINE", 0))
    if offline_flag and attributes & offline_flag:
        return "offline_flag"
    blocks = getattr(file_stat, "st_blocks", None)
    if file_stat.st_size > 0 and blocks == 0:
        return "possible_dataless_zero_blocks"
    return None


def _cloud_materialization_proven(
    _file_stat: os.stat_result,
) -> Tuple[bool, str]:
    """Fail closed because portable stat flags cannot prove materialization."""

    return False, "cloud_materialization_proof_unavailable"


def _timestamp_seconds(file_stat: os.stat_result, field: str) -> Optional[float]:
    if field == "birthtime":
        value = getattr(file_stat, "st_birthtime", None)
        return float(value) if value is not None else None
    return float(file_stat.st_mtime_ns) / 1_000_000_000


def _format_timestamp(value: float, timezone: dt.tzinfo) -> str:
    return dt.datetime.fromtimestamp(value, timezone).isoformat(timespec="microseconds")


def _file_identity(file_stat: os.stat_result) -> Tuple[int, int, int, int, int, int]:
    return (
        file_stat.st_dev,
        file_stat.st_ino,
        stat.S_IFMT(file_stat.st_mode),
        file_stat.st_size,
        file_stat.st_mtime_ns,
        file_stat.st_ctime_ns,
    )


class Scanner:
    def __init__(self, parsed: Dict[str, Any]) -> None:
        self.start = parsed["start"]
        self.end = parsed["end"]
        self.start_seconds = self.start.timestamp()
        self.end_seconds = self.end.timestamp()
        self.started = parsed["started"]
        self.deadline = parsed["deadline"]
        self.options = parsed["options"]
        self.cloud_hash_allowlist = parsed["cloud_hash_allowlist"]
        self.observations: List[Dict[str, Any]] = []
        self.skipped: Counter[str] = Counter()
        self.visited_entries = 0
        self.truncation_reasons: List[str] = []
        self.hash_requested = 0
        self.hash_computed = 0
        self.hash_bytes_read = 0
        self.hash_skipped: Counter[str] = Counter()
        self.birthtime_supported = all(
            hasattr(os.fstat(root["fd"]), "st_birthtime") for root in parsed["roots"]
        )

    @property
    def truncated(self) -> bool:
        return bool(self.truncation_reasons)

    def _stop(self, reason: str) -> None:
        if reason not in self.truncation_reasons:
            self.truncation_reasons.append(reason)

    def _deadline_exceeded(self) -> bool:
        if time.monotonic() >= self.deadline:
            self._stop("deadline_exceeded")
            return True
        return False

    def _skip_hash(self, reason: str) -> Dict[str, Any]:
        self.hash_skipped[reason] += 1
        return {"status": "skipped", "reason": reason}

    def _hash_file(
        self,
        root: Dict[str, Any],
        directory_fd: int,
        name: str,
        relative_path: str,
        expected: os.stat_result,
    ) -> Dict[str, Any]:
        self.hash_requested += 1
        if root["kind"] == "cloudstorage":
            if (root["index"], relative_path) not in self.cloud_hash_allowlist:
                return self._skip_hash("cloud_hash_not_allowlisted")
            materialized, reason = _cloud_materialization_proven(expected)
            if not materialized:
                return self._skip_hash(reason)

        if expected.st_size > self.options["hash_max_bytes"]:
            return self._skip_hash("size_limit")
        remaining_total = self.options["hash_total_max_bytes"] - self.hash_bytes_read
        if expected.st_size > remaining_total:
            return self._skip_hash("aggregate_hash_byte_limit")
        if self._deadline_exceeded():
            return self._skip_hash("deadline_exceeded")

        no_follow = int(getattr(os, "O_NOFOLLOW", 0))
        if not no_follow:
            return self._skip_hash("no_nofollow_support")
        flags = (
            os.O_RDONLY
            | no_follow
            | int(getattr(os, "O_CLOEXEC", 0))
            | int(getattr(os, "O_NONBLOCK", 0))
        )
        try:
            descriptor = os.open(name, flags, dir_fd=directory_fd)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EPERM}:
                reason = "permission_denied"
            elif exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                reason = "symlink_race"
            else:
                reason = "open_failed"
            return self._skip_hash(reason)

        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_dev != root["st_dev"]
                or _file_identity(before) != _file_identity(expected)
            ):
                return self._skip_hash("changed_during_scan")

            digest = hashlib.sha256()
            total = 0
            while total < before.st_size:
                if self._deadline_exceeded():
                    return self._skip_hash("deadline_exceeded")
                remaining_file = before.st_size - total
                remaining_total = self.options["hash_total_max_bytes"] - self.hash_bytes_read
                if remaining_total <= 0:
                    return self._skip_hash("aggregate_hash_byte_limit")
                read_size = min(HASH_CHUNK_BYTES, remaining_file, remaining_total)
                chunk = os.read(descriptor, read_size)
                if not chunk:
                    return self._skip_hash("changed_during_scan")
                self.hash_bytes_read += len(chunk)
                total += len(chunk)
                digest.update(chunk)
                if self._deadline_exceeded():
                    return self._skip_hash("deadline_exceeded")

            after = os.fstat(descriptor)
            if _file_identity(after) != _file_identity(before):
                return self._skip_hash("changed_during_scan")
            self.hash_computed += 1
            return {
                "algorithm": self.options["hash_algorithm"],
                "status": "computed",
                "value": digest.hexdigest(),
            }
        except OSError:
            return self._skip_hash("read_failed")
        finally:
            os.close(descriptor)

    def _record_file(
        self,
        root: Dict[str, Any],
        directory_fd: int,
        name: str,
        relative_path: str,
        file_stat: os.stat_result,
        cloud_reason: Optional[str],
    ) -> None:
        timestamps: Dict[str, Optional[float]] = {}
        activity: List[str] = []

        if self.options["include_birthtime"]:
            birthtime = _timestamp_seconds(file_stat, "birthtime")
            timestamps["birthtime"] = birthtime
            if birthtime is None:
                self.birthtime_supported = False
            elif self.start_seconds <= birthtime < self.end_seconds:
                activity.append("created")

        if self.options["include_mtime"]:
            mtime = _timestamp_seconds(file_stat, "mtime")
            timestamps["mtime"] = mtime
            if mtime is not None and self.start_seconds <= mtime < self.end_seconds:
                activity.append("modified")

        if not activity:
            return
        if len(self.observations) >= self.options["max_candidates"]:
            self._stop("max_candidates")
            return

        absolute_path = os.path.join(root["path"], relative_path)
        observation: Dict[str, Any] = {
            "absolute_path": absolute_path,
            "activity": activity,
            "relative_path": relative_path,
            "root": root["path"],
            "root_index": root["index"],
        }
        if self.options["include_birthtime"]:
            value = timestamps.get("birthtime")
            observation["birthtime"] = (
                _format_timestamp(value, self.start.tzinfo) if value is not None else None
            )
        if self.options["include_mtime"]:
            value = timestamps.get("mtime")
            observation["mtime"] = (
                _format_timestamp(value, self.start.tzinfo) if value is not None else None
            )
        if self.options["include_size"]:
            observation["size_bytes"] = file_stat.st_size
        if self.options["include_type"]:
            mime, encoding = mimetypes.guess_type(relative_path, strict=False)
            observation["type"] = {
                "encoding": encoding,
                "extension": Path(relative_path).suffix.casefold() or None,
                "mime": mime,
            }
        if root["kind"] == "cloudstorage":
            observation["storage"] = {
                "kind": "cloudstorage",
                "materialization": cloud_reason or "not_proven",
            }
        if self.options["include_hash"]:
            observation["hash"] = self._hash_file(
                root, directory_fd, name, relative_path, file_stat
            )
        self.observations.append(observation)

    def _open_child_directory(
        self,
        root: Dict[str, Any],
        directory_fd: int,
        name: str,
        expected: os.stat_result,
    ) -> Optional[int]:
        if self._deadline_exceeded():
            return None
        flags = _directory_open_flags()
        try:
            descriptor = os.open(name, flags, dir_fd=directory_fd)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EPERM}:
                self.skipped["permission_denied"] += 1
            elif exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                self.skipped["directory_changed_or_symlink_race"] += 1
            elif exc.errno == errno.ENOENT:
                self.skipped["disappeared"] += 1
            else:
                self.skipped["directory_open_error"] += 1
            return None

        opened = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or opened.st_dev != expected.st_dev
            or opened.st_ino != expected.st_ino
        ):
            os.close(descriptor)
            self.skipped["directory_changed_during_scan"] += 1
            return None
        if opened.st_dev != root["st_dev"]:
            os.close(descriptor)
            self.skipped["nested_mount"] += 1
            return None
        return descriptor

    def _scan_directory(
        self,
        root: Dict[str, Any],
        directory_fd: int,
        current_relative: str,
    ) -> List[Tuple[int, str]]:
        directories: List[Tuple[int, str]] = []
        try:
            try:
                with os.scandir(directory_fd) as iterator:
                    while True:
                        if self._deadline_exceeded():
                            break
                        if self.visited_entries >= self.options["max_visited_entries"]:
                            self._stop("max_visited_entries")
                            break
                        try:
                            entry = next(iterator)
                        except StopIteration:
                            break
                        self.visited_entries += 1
                        try:
                            entry_stat = os.stat(
                                entry.name,
                                dir_fd=directory_fd,
                                follow_symlinks=False,
                            )
                        except PermissionError:
                            self.skipped["permission_denied"] += 1
                            continue
                        except FileNotFoundError:
                            self.skipped["disappeared"] += 1
                            continue
                        except OSError:
                            self.skipped["stat_error"] += 1
                            continue

                        name = entry.name
                        relative = (
                            f"{current_relative}/{name}"
                            if current_relative
                            else name
                        )
                        mode = entry_stat.st_mode
                        if stat.S_ISLNK(mode):
                            self.skipped["symlink"] += 1
                        elif stat.S_ISDIR(mode):
                            reason = _directory_exclusion(
                                name, relative, self.options["exclude_globs"]
                            )
                            if reason:
                                self.skipped[reason] += 1
                            else:
                                child_fd = self._open_child_directory(
                                    root, directory_fd, name, entry_stat
                                )
                                if child_fd is not None:
                                    directories.append((child_fd, relative))
                        elif not stat.S_ISREG(mode):
                            self.skipped["special_file"] += 1
                        else:
                            cloud_reason = _cloud_placeholder_reason(name, entry_stat)
                            if cloud_reason == "cloud_placeholder_stub":
                                self.skipped[cloud_reason] += 1
                            else:
                                reason = _file_exclusion(
                                    name,
                                    relative,
                                    self.options["exclude_globs"],
                                )
                                if reason:
                                    self.skipped[reason] += 1
                                elif cloud_reason and root["kind"] != "cloudstorage":
                                    self.skipped[cloud_reason] += 1
                                else:
                                    self._record_file(
                                        root,
                                        directory_fd,
                                        name,
                                        relative,
                                        entry_stat,
                                        cloud_reason,
                                    )

                        if self.truncated:
                            break
                        if self.visited_entries >= self.options["max_visited_entries"]:
                            # Do not pull a look-ahead entry merely to learn
                            # whether this exact boundary was also end-of-dir.
                            self._stop("max_visited_entries")
                            break
            except PermissionError:
                self.skipped["permission_denied"] += 1
            except FileNotFoundError:
                self.skipped["disappeared"] += 1
            except OSError:
                self.skipped["scan_error"] += 1
            if self.truncated:
                for child_fd, _relative in directories:
                    os.close(child_fd)
                return []
            return directories
        finally:
            os.close(directory_fd)

    def scan_root(self, root: Dict[str, Any]) -> None:
        try:
            initial_fd = os.dup(root["fd"])
        except OSError:
            self.skipped["root_descriptor_error"] += 1
            return
        stack: List[Tuple[int, str]] = [(initial_fd, "")]
        try:
            while stack and not self.truncated:
                directory_fd, current_relative = stack.pop()
                children = self._scan_directory(root, directory_fd, current_relative)
                stack.extend(reversed(children))
        finally:
            for directory_fd, _relative in stack:
                try:
                    os.close(directory_fd)
                except OSError:
                    pass


def _capabilities(scanner: Scanner) -> Dict[str, Any]:
    return {
        "birthtime": {
            "status": "supported" if scanner.birthtime_supported else "unavailable",
            "limitation": (
                None
                if scanner.birthtime_supported
                else "This filesystem or platform did not expose st_birthtime; mtime remains available."
            ),
        },
        "cloud_hashing": {
            "status": "metadata_only_fail_closed",
            "limitation": (
                "An exact-file allowlist is necessary but not sufficient: this runtime has no reliable, "
                "side-effect-free proof of local File Provider materialization, so CloudStorage content is not opened."
            ),
        },
        "cloud_placeholder_detection": {
            "status": "best_effort",
            "methods": [
                "SF_DATALESS",
                "FILE_ATTRIBUTE_OFFLINE",
                ".icloud stub name",
                "nonzero logical size with zero allocated blocks (conservative)",
            ],
        },
        "descriptor_relative_traversal": True,
        "hash_open_no_follow": bool(getattr(os, "O_NOFOLLOW", 0)),
        "symlink_policy": "never_follow",
    }


def _fit_output_payload(payload: Dict[str, Any], max_bytes: int) -> Dict[str, Any]:
    if len(_encoded_payload(payload)) <= max_bytes:
        return payload

    observations = list(payload.get("observations", []))
    total = len(observations)
    summary = payload["summary"]
    original_reasons = list(summary.get("truncation_reasons", []))
    if "max_output_bytes" not in original_reasons:
        original_reasons.append("max_output_bytes")

    def configure(count: int) -> None:
        payload["observations"] = observations[:count]
        summary["candidate_count"] = count
        summary["matched_candidate_count"] = total
        summary["candidates_omitted_for_output"] = total - count
        summary["partial"] = True
        summary["truncated"] = True
        summary["truncation_reasons"] = original_reasons
        summary["truncation_reason"] = original_reasons[0]

    low = 0
    high = total
    while low < high:
        middle = (low + high + 1) // 2
        configure(middle)
        if len(_encoded_payload(payload)) <= max_bytes:
            low = middle
        else:
            high = middle - 1
    configure(low)
    if len(_encoded_payload(payload)) <= max_bytes:
        return payload

    compact = _base_output(True)
    compact.update(
        {
            "mode": payload.get("mode", "metadata_only"),
            "observations": [],
            "operation": OPERATION,
            "state_written": False,
            "summary": {
                "candidate_count": 0,
                "candidates_omitted_for_output": total,
                "matched_candidate_count": total,
                "max_output_bytes": max_bytes,
                "partial": True,
                "truncated": True,
                "truncation_reason": "max_output_bytes",
                "truncation_reasons": ["max_output_bytes"],
                "visited_entries": summary.get("visited_entries", 0),
            },
            "window": payload.get("window"),
        }
    )
    if "request_id" in payload:
        compact["request_id"] = payload["request_id"]
    if len(_encoded_payload(compact)) <= max_bytes:
        return compact
    compact.pop("request_id", None)
    if len(_encoded_payload(compact)) <= max_bytes:
        return compact

    return {
        **_base_output(True),
        "observations": [],
        "operation": OPERATION,
        "state_written": False,
        "summary": {
            "candidate_count": 0,
            "partial": True,
            "truncated": True,
            "truncation_reason": "max_output_bytes",
        },
    }


def _scan(parsed: Dict[str, Any]) -> Dict[str, Any]:
    scanner = Scanner(parsed)
    for root in parsed["roots"]:
        if scanner.truncated:
            break
        scanner.scan_root(root)
    scanner.observations.sort(
        key=lambda item: (
            item["root_index"],
            item["relative_path"].casefold(),
            item["relative_path"],
        )
    )

    hash_partial = scanner.hash_requested != scanner.hash_computed
    partial_reasons = list(scanner.truncation_reasons)
    if hash_partial:
        partial_reasons.append("hashes_skipped")

    payload = _base_output(True)
    payload.update(
        {
            "capabilities": _capabilities(scanner),
            "content_diff": {
                "status": "not_computed",
                "reason": "No prior snapshot or version-control baseline is accepted by this collector.",
            },
            "mode": (
                "metadata_plus_explicit_hash"
                if parsed["options"]["include_hash"]
                else "metadata_only"
            ),
            "observations": scanner.observations,
            "operation": OPERATION,
            "options_applied": {
                **parsed["options"],
                "cloud_hash_allowlist_count": len(parsed["cloud_hash_allowlist"]),
            },
            "roots": [
                {
                    "alias": root["alias"],
                    "kind": root["kind"],
                    "path": root["path"],
                    "root_index": root["index"],
                }
                for root in parsed["roots"]
            ],
            "state_written": False,
            "summary": {
                "candidate_count": len(scanner.observations),
                "deadline_ms": parsed["options"]["deadline_ms"],
                "elapsed_ms": max(0, int((time.monotonic() - parsed["started"]) * 1_000)),
                "hashes": {
                    "bytes_read": scanner.hash_bytes_read,
                    "computed": scanner.hash_computed,
                    "partial": hash_partial,
                    "requested": scanner.hash_requested,
                    "skipped_by_reason": dict(sorted(scanner.hash_skipped.items())),
                    "total_byte_limit": parsed["options"]["hash_total_max_bytes"],
                },
                "partial": bool(partial_reasons),
                "partial_reasons": partial_reasons,
                "skipped_by_reason": dict(sorted(scanner.skipped.items())),
                "truncated": scanner.truncated,
                "truncation_reason": (
                    scanner.truncation_reasons[0] if scanner.truncation_reasons else None
                ),
                "truncation_reasons": list(scanner.truncation_reasons),
                "visited_entries": scanner.visited_entries,
            },
            "window": {
                "end": parsed["end"].isoformat(),
                "semantics": "[start,end)",
                "start": parsed["start"].isoformat(),
            },
        }
    )
    if parsed["request_id"] is not None:
        payload["request_id"] = parsed["request_id"]
    return _fit_output_payload(payload, parsed["options"]["max_output_bytes"])


def main() -> int:
    parsed: Optional[Dict[str, Any]] = None
    try:
        if len(sys.argv) != 1:
            raise ContractError(
                "validation_error", "This collector accepts JSON on stdin and no arguments."
            )
        request = _read_request()
        parsed = _parse_request(request)
        payload = _scan(parsed)
        _emit(payload, parsed["options"]["max_output_bytes"])
        return 0
    except ContractError as failure:
        _emit_failure(failure)
        return 2
    except Exception:
        _emit_failure(
            ContractError(
                "internal_error",
                "The collector failed unexpectedly without writing any state.",
            )
        )
        return 1
    finally:
        _close_parsed_roots(parsed)


if __name__ == "__main__":
    raise SystemExit(main())
