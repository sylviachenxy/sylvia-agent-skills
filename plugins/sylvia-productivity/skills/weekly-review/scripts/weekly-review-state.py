#!/usr/bin/env python3
"""Private config and baseline state manager for weekly-review.

The process accepts one JSON object on stdin and emits one JSON object on
stdout. Production storage is fixed under the current user's Application
Support directory. A guarded temporary-directory override exists only for the
offline test suite.
"""

from __future__ import annotations

import contextlib
import ctypes
import datetime as dt
import difflib
import errno
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


MANAGER_VERSION = "1.0.0"
PROTOCOL_VERSION = 1
SCHEMA_VERSION = 1
MAX_STDIN_BYTES = 4 * 1024 * 1024
MAX_STATE_BYTES = 8 * 1024 * 1024
MAX_CONFIG_BYTES = 2 * 1024 * 1024
MAX_REPORT_FILES = 10_000
MAX_RECEIPTS = 10_000

RENAME_EXCL = 0x00000004

APP_DIR_NAME = "io.github.sylviachenxy.sylvia-agent-skills.weekly-review"
CONFIG_NAME = "config-v1.json"
STATE_NAME = "state-v1.json"
SNAPSHOTS_NAME = "snapshots"
LOCK_NAME = ".weekly-review-state.lock"

TEST_ALLOW_ENV = "WEEKLY_REVIEW_STATE_ALLOW_TEST_OVERRIDE"
TEST_ROOT_ENV = "WEEKLY_REVIEW_STATE_TEST_ROOT"
TEST_HOME_ENV = "WEEKLY_REVIEW_STATE_TEST_HOME"
TEST_FAILPOINT_ENV = "WEEKLY_REVIEW_STATE_TEST_FAILPOINT"
TEST_MAX_STATE_BYTES_ENV = "WEEKLY_REVIEW_STATE_TEST_MAX_STATE_BYTES"
TEST_MAX_CONFIG_BYTES_ENV = "WEEKLY_REVIEW_STATE_TEST_MAX_CONFIG_BYTES"

ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
REVIEW_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
WEEK_ID_RE = re.compile(r"^(\d{4})-W(\d{2})$")
SNAPSHOT_NAME_RE = re.compile(r"^[0-9a-f]{64}--[0-9a-f]{64}\.txt$")

SOURCE_KINDS = frozenset(
    {"file", "git", "goal", "reminder", "calendar", "note", "mail"}
)
COVERAGE_STATUSES = frozenset(
    {"complete", "partial", "unavailable", "declined", "not_configured"}
)
PLAINTEXT_SUFFIXES = frozenset(
    {
        ".bash",
        ".c",
        ".cc",
        ".cpp",
        ".css",
        ".csv",
        ".fish",
        ".go",
        ".h",
        ".hpp",
        ".html",
        ".java",
        ".js",
        ".json",
        ".jsx",
        ".kt",
        ".markdown",
        ".md",
        ".py",
        ".rs",
        ".rst",
        ".scss",
        ".sh",
        ".sql",
        ".swift",
        ".tex",
        ".toml",
        ".ts",
        ".tsv",
        ".tsx",
        ".txt",
        ".yaml",
        ".yml",
        ".zsh",
    }
)
SENSITIVE_NAMES = frozenset(
    {
        ".env",
        "client_secret.json",
        "credentials.json",
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
        ".p12",
        ".pem",
        ".pfx",
        ".ppk",
    }
)


class ContractError(Exception):
    """Stable, sanitized request or state failure."""

    def __init__(
        self, code: str, message: str, details: Optional[Dict[str, Any]] = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def _emit(payload: Dict[str, Any]) -> None:
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            separators=(",", ": "),
        )
    )


def _base_output(ok: bool, operation: Optional[str], request_id: Optional[str]) -> Dict[str, Any]:
    output: Dict[str, Any] = {
        "manager_version": MANAGER_VERSION,
        "ok": ok,
        "protocol_version": PROTOCOL_VERSION,
    }
    if operation is not None:
        output["operation"] = operation
    if request_id is not None:
        output["request_id"] = request_id
    return output


def _emit_failure(
    failure: ContractError, operation: Optional[str], request_id: Optional[str]
) -> None:
    output = _base_output(False, operation, request_id)
    error: Dict[str, Any] = {"code": failure.code, "message": failure.message}
    if failure.details:
        error["details"] = failure.details
    output["error"] = error
    _emit(output)


def _ensure_keys(value: Dict[str, Any], allowed: Iterable[str], context: str) -> None:
    unknown = sorted(set(value).difference(allowed))
    if unknown:
        raise ContractError(
            "validation_error",
            f"Unknown key(s) in {context}.",
            {"keys": unknown},
        )


def _require_keys(value: Dict[str, Any], required: Iterable[str], context: str) -> None:
    missing = sorted(set(required).difference(value))
    if missing:
        raise ContractError(
            "validation_error",
            f"Missing required key(s) in {context}.",
            {"keys": missing},
        )


def _require_object(value: Any, field: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError("validation_error", f"{field} must be an object.")
    return value


def _require_bool(value: Any, field: str) -> bool:
    if type(value) is not bool:
        raise ContractError("validation_error", f"{field} must be a boolean.")
    return value


def _require_int(value: Any, field: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ContractError(
            "validation_error",
            f"{field} must be an integer from {minimum} through {maximum}.",
        )
    return value


def _require_string(value: Any, field: str, maximum: int = 1024) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise ContractError(
            "validation_error", f"{field} must be a non-empty safe string."
        )
    return value


def _require_utf8_string(value: Any, field: str, maximum_bytes: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or any(ord(character) < 32 for character in value)
    ):
        raise ContractError(
            "validation_error", f"{field} must be a non-empty safe string."
        )
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ContractError(
            "validation_error", f"{field} must be valid UTF-8 text."
        ) from exc
    if len(encoded) > maximum_bytes:
        raise ContractError(
            "validation_error",
            f"{field} exceeds its UTF-8 byte limit.",
            {"maximum_utf8_bytes": maximum_bytes},
        )
    return value


def _require_scope_id(value: Any, field: str) -> str:
    result = _require_string(value, field, 64)
    if not ID_RE.fullmatch(result):
        raise ContractError(
            "validation_error",
            f"{field} must use lowercase letters, digits, underscores, or hyphens.",
        )
    return result


def _require_sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ContractError(
            "validation_error", f"{field} must be a lowercase SHA-256 digest."
        )
    return value


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


def _parse_common(request: Dict[str, Any]) -> Tuple[str, str]:
    operation = _require_string(request.get("operation"), "operation", 64)
    request_id = _require_string(request.get("request_id"), "request_id", 128)
    if request.get("protocol_version") != PROTOCOL_VERSION:
        raise ContractError(
            "unsupported_protocol",
            f"protocol_version must be {PROTOCOL_VERSION}.",
        )
    return operation, request_id


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _same_or_within_casefold(path: Path, parent: Path) -> bool:
    """Conservatively protect reserved Mac paths on case-insensitive volumes."""

    path_parts = tuple(part.casefold() for part in path.parts)
    parent_parts = tuple(part.casefold() for part in parent.parts)
    return (
        len(path_parts) >= len(parent_parts)
        and path_parts[: len(parent_parts)] == parent_parts
    )


def _validated_temp_override(raw: str, field: str) -> Path:
    if not raw or not os.path.isabs(raw) or "\x00" in raw:
        raise ContractError("unsafe_test_override", f"{field} must be absolute.")
    normalized = Path(os.path.normpath(raw))
    path = Path(os.path.realpath(normalized))
    if path != normalized:
        raise ContractError(
            "unsafe_test_override", f"{field} must not contain symlink components."
        )
    temporary_root = Path(os.path.realpath(tempfile.gettempdir()))
    if path == temporary_root or not _is_within(path, temporary_root):
        raise ContractError(
            "unsafe_test_override",
            f"{field} must be a specific descendant of the system temporary directory.",
        )
    return path


def _test_override_enabled() -> bool:
    return os.environ.get(TEST_ALLOW_ENV) == "1" and bool(
        os.environ.get(TEST_ROOT_ENV)
    )


def _effective_home() -> Path:
    raw = os.environ.get(TEST_HOME_ENV)
    if raw is not None:
        if not _test_override_enabled():
            raise ContractError(
                "unsafe_test_override",
                f"{TEST_HOME_ENV} is accepted only with the guarded test override.",
            )
        return _validated_temp_override(raw, TEST_HOME_ENV)
    return Path.home().resolve()


def _storage_root() -> Tuple[Path, bool]:
    raw = os.environ.get(TEST_ROOT_ENV)
    if raw is not None:
        if os.environ.get(TEST_ALLOW_ENV) != "1":
            raise ContractError(
                "unsafe_test_override",
                f"{TEST_ROOT_ENV} is accepted only when {TEST_ALLOW_ENV}=1.",
            )
        return _validated_temp_override(raw, TEST_ROOT_ENV), True
    return (
        Path.home()
        / "Library"
        / "Application Support"
        / APP_DIR_NAME,
        False,
    )


def _assert_no_symlink_components(path: Path, field: str) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current = current / component
        try:
            item_stat = os.lstat(current)
        except FileNotFoundError:
            raise ContractError(
                "unsafe_path", f"{field} must refer to an existing path."
            )
        except OSError as exc:
            raise ContractError("unsafe_path", f"{field} cannot be inspected.") from exc
        if stat.S_ISLNK(item_stat.st_mode):
            raise ContractError(
                "unsafe_path", f"{field} must not contain symlink components."
            )


def _cloudstorage_specific_exception(path: Path, home: Path) -> bool:
    cloud_root = home / "Library" / "CloudStorage"
    if not _is_within(path, cloud_root):
        return False
    relative = path.relative_to(cloud_root)
    # Reject CloudStorage itself and a provider root. At least one explicit
    # folder below the provider is required, e.g. GoogleDrive-.../My Drive.
    return len(relative.parts) >= 2


def _validate_directory_path(
    raw: Any, field: str, writable: bool = False, live: bool = True
) -> str:
    value = _require_string(raw, field, 4096)
    if not os.path.isabs(value):
        raise ContractError("unsafe_path", f"{field} must be an absolute path.")
    normalized = os.path.normpath(value)
    if normalized != value:
        raise ContractError(
            "unsafe_path", f"{field} must be a canonical path without dot segments."
        )
    path = Path(normalized)
    if live:
        if Path(os.path.realpath(path)) != path:
            raise ContractError(
                "unsafe_path", f"{field} must not contain symlink components."
            )
        _assert_no_symlink_components(path, field)
        try:
            item_stat = os.lstat(path)
        except OSError as exc:
            raise ContractError(
                "unsafe_path", f"{field} cannot be inspected."
            ) from exc
        if not stat.S_ISDIR(item_stat.st_mode):
            raise ContractError("unsafe_path", f"{field} must be a real directory.")

    home = _effective_home()
    home_library = home / "Library"
    forbidden_exact = {
        Path(os.sep),
        home,
        home_library,
        home_library / "CloudStorage",
        Path("/Applications"),
        Path("/Library"),
        Path("/System"),
        Path("/Users"),
        Path("/Volumes"),
    }
    system_trees = (Path("/Applications"), Path("/Library"), Path("/System"))
    is_user_home_root = (
        len(path.parts) == 3 and path.parts[0] == os.sep and path.parts[1] == "Users"
    )
    if (
        path in forbidden_exact
        or is_user_home_root
        or any(_is_within(path, system_root) for system_root in system_trees)
    ):
        raise ContractError(
            "unsafe_scope",
            f"{field} is too broad; choose a specific user-controlled folder.",
        )
    if _is_within(path, home_library) and not _cloudstorage_specific_exception(path, home):
        raise ContractError(
            "unsafe_scope",
            f"{field} cannot select Library data or a CloudStorage provider root.",
        )

    if live:
        access = os.R_OK | os.X_OK | (os.W_OK if writable else 0)
        if not os.access(path, access):
            raise ContractError(
                "unsafe_path",
                f"{field} does not have the required filesystem access.",
            )
    return str(path)


def _validate_relative_path(raw: Any, field: str, maximum: int = 1024) -> str:
    value = _require_string(raw, field, maximum)
    if "\\" in value:
        raise ContractError(
            "validation_error", f"{field} must use vault-relative POSIX separators."
        )
    path = PurePosixPath(value)
    if path.is_absolute() or value in {".", ".."} or any(
        part in {"", ".", ".."} for part in path.parts
    ):
        raise ContractError(
            "validation_error", f"{field} must be a safe relative path."
        )
    return path.as_posix()


def _validate_scope_entries(
    raw: Any,
    context: str,
    locator_key: str,
    locator_maximum: int = 512,
    require_mail_semantics: bool = False,
) -> List[Dict[str, Any]]:
    if not isinstance(raw, list) or len(raw) > 64:
        raise ContractError(
            "validation_error", f"{context} must be an array with at most 64 entries."
        )
    output: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for index, entry_raw in enumerate(raw):
        field = f"{context}[{index}]"
        entry = _require_object(entry_raw, field)
        keys = {"id", "account_id", locator_key, "alias", "content_access"}
        if require_mail_semantics:
            keys.update({"scope_kind", "date_field"})
        _ensure_keys(
            entry,
            keys,
            field,
        )
        _require_keys(
            entry,
            keys,
            field,
        )
        scope_id = _require_scope_id(entry["id"], f"{field}.id")
        if scope_id in seen:
            raise ContractError("validation_error", f"Duplicate scope id in {context}.")
        seen.add(scope_id)
        account_id = _require_string(entry["account_id"], f"{field}.account_id", 512)
        locator = _require_string(
            entry[locator_key], f"{field}.{locator_key}", locator_maximum
        )
        alias = _require_string(entry["alias"], f"{field}.alias", 128)
        content_access = entry["content_access"]
        if content_access not in {"metadata", "plaintext"}:
            raise ContractError(
                "validation_error",
                f"{field}.content_access must be 'metadata' or 'plaintext'.",
            )
        normalized = {
            "account_id": account_id,
            "alias": alias,
            "content_access": content_access,
            "id": scope_id,
            locator_key: locator,
        }
        if require_mail_semantics:
            scope_kind = _require_string(
                entry["scope_kind"], f"{field}.scope_kind", 32
            )
            date_field = _require_string(
                entry["date_field"], f"{field}.date_field", 16
            )
            if scope_kind == "sent":
                if date_field != "sent":
                    raise ContractError(
                        "validation_error",
                        f"{field}.date_field must be 'sent' for a sent scope.",
                    )
            elif scope_kind == "weekly_review_label":
                if date_field not in {"sent", "received"}:
                    raise ContractError(
                        "validation_error",
                        f"{field}.date_field must be 'sent' or 'received' for a weekly_review_label scope.",
                    )
            else:
                raise ContractError(
                    "validation_error",
                    f"{field}.scope_kind must be 'sent' or 'weekly_review_label'.",
                )
            normalized["date_field"] = date_field
            normalized["scope_kind"] = scope_kind
        output.append(normalized)
    return output


def _validate_string_id_list(raw: Any, field: str) -> List[str]:
    if not isinstance(raw, list) or len(raw) > 128:
        raise ContractError(
            "validation_error", f"{field} must be an array with at most 128 entries."
        )
    output: List[str] = []
    seen: Set[str] = set()
    for index, item in enumerate(raw):
        value = _require_utf8_string(item, f"{field}[{index}]", 4096)
        if value in seen:
            raise ContractError("validation_error", f"{field} contains a duplicate ID.")
        seen.add(value)
        output.append(value)
    return output


def _validate_root_entries(
    raw: Any, context: str, snapshot_capable: bool, live: bool = True
) -> List[Dict[str, Any]]:
    if not isinstance(raw, list) or len(raw) > 64:
        raise ContractError(
            "validation_error", f"{context} must be an array with at most 64 entries."
        )
    output: List[Dict[str, Any]] = []
    ids: Set[str] = set()
    paths: Set[str] = set()
    for index, entry_raw in enumerate(raw):
        field = f"{context}[{index}]"
        entry = _require_object(entry_raw, field)
        allowed = {"id", "path", "snapshot_text"} if snapshot_capable else {"id", "path"}
        _ensure_keys(entry, allowed, field)
        _require_keys(entry, allowed, field)
        scope_id = _require_scope_id(entry["id"], f"{field}.id")
        path = _validate_directory_path(
            entry["path"], f"{field}.path", live=live
        )
        if scope_id in ids or path in paths:
            raise ContractError(
                "validation_error", f"{context} contains a duplicate id or path."
            )
        ids.add(scope_id)
        paths.add(path)
        normalized: Dict[str, Any] = {"id": scope_id, "path": path}
        if snapshot_capable:
            normalized["snapshot_text"] = _require_bool(
                entry["snapshot_text"], f"{field}.snapshot_text"
            )
        output.append(normalized)
    return output


def _validate_author_emails(raw: Any, field: str) -> List[str]:
    if not isinstance(raw, list) or len(raw) > 16:
        raise ContractError(
            "validation_error",
            f"{field} must be an array with at most 16 entries.",
        )
    output: List[str] = []
    seen: Set[str] = set()
    for index, item in enumerate(raw):
        item_field = f"{field}[{index}]"
        if (
            not isinstance(item, str)
            or not item
            or not item.isascii()
            or len(item) > 320
            or any(character in item for character in "<>\r\n\x00")
        ):
            raise ContractError(
                "validation_error",
                f"{item_field} must be a non-empty safe ASCII email string.",
            )
        folded = item.casefold()
        if folded in seen:
            raise ContractError(
                "validation_error",
                f"{field} must not contain case-insensitive duplicates.",
            )
        seen.add(folded)
        output.append(item)
    return output


def _validate_git_entries(raw: Any, live: bool = True) -> List[Dict[str, Any]]:
    context = "config.git.repositories"
    if not isinstance(raw, list) or len(raw) > 64:
        raise ContractError(
            "validation_error", f"{context} must be an array with at most 64 entries."
        )
    entries: List[Dict[str, Any]] = []
    ids: Set[str] = set()
    paths: Set[str] = set()
    for index, entry_raw in enumerate(raw):
        field = f"{context}[{index}]"
        entry = _require_object(entry_raw, field)
        keys = {"id", "path", "author_emails"}
        _ensure_keys(entry, keys, field)
        _require_keys(entry, keys, field)
        scope_id = _require_scope_id(entry["id"], f"{field}.id")
        path = _validate_directory_path(
            entry["path"], f"{field}.path", live=live
        )
        if scope_id in ids or path in paths:
            raise ContractError(
                "validation_error", f"{context} contains a duplicate id or path."
            )
        ids.add(scope_id)
        paths.add(path)
        normalized = {
            "author_emails": _validate_author_emails(
                entry["author_emails"], f"{field}.author_emails"
            ),
            "id": scope_id,
            "path": path,
        }
        entries.append(normalized)

    if not live:
        return entries

    for index, entry in enumerate(entries):
        marker = Path(entry["path"]) / ".git"
        try:
            marker_stat = os.lstat(marker)
        except FileNotFoundError as exc:
            raise ContractError(
                "validation_error",
                f"config.git.repositories[{index}].path is not a Git worktree.",
            ) from exc
        except OSError as exc:
            raise ContractError(
                "unsafe_path",
                f"config.git.repositories[{index}].path Git metadata cannot be inspected.",
            ) from exc
        if stat.S_ISLNK(marker_stat.st_mode) or not stat.S_ISDIR(marker_stat.st_mode):
            raise ContractError(
                "unsafe_path",
                f"config.git.repositories[{index}].path must have a real .git directory.",
            )
        try:
            os.lstat(marker / "worktrees")
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise ContractError(
                "unsafe_path",
                f"config.git.repositories[{index}].path has Git metadata that cannot be inspected.",
            ) from exc
        else:
            raise ContractError(
                "unsafe_path",
                f"config.git.repositories[{index}].path manages linked worktrees, which are unsupported.",
            )
    return entries


def _validate_limits(raw: Any) -> Dict[str, int]:
    limits = _require_object(raw, "config.limits")
    keys = {
        "max_baseline_entries",
        "max_candidates_per_source",
        "max_content_chars",
        "max_diff_lines",
        "max_report_bytes",
        "snapshot_max_file_bytes",
        "snapshot_max_total_bytes",
    }
    _ensure_keys(limits, keys, "config.limits")
    _require_keys(limits, keys, "config.limits")
    normalized = {
        "max_baseline_entries": _require_int(
            limits["max_baseline_entries"],
            "config.limits.max_baseline_entries",
            1,
            100_000,
        ),
        "max_candidates_per_source": _require_int(
            limits["max_candidates_per_source"],
            "config.limits.max_candidates_per_source",
            1,
            5_000,
        ),
        "max_content_chars": _require_int(
            limits["max_content_chars"],
            "config.limits.max_content_chars",
            256,
            500_000,
        ),
        "max_diff_lines": _require_int(
            limits["max_diff_lines"],
            "config.limits.max_diff_lines",
            20,
            5_000,
        ),
        "max_report_bytes": _require_int(
            limits["max_report_bytes"],
            "config.limits.max_report_bytes",
            1_024,
            20 * 1024 * 1024,
        ),
        "snapshot_max_file_bytes": _require_int(
            limits["snapshot_max_file_bytes"],
            "config.limits.snapshot_max_file_bytes",
            256,
            2 * 1024 * 1024,
        ),
        "snapshot_max_total_bytes": _require_int(
            limits["snapshot_max_total_bytes"],
            "config.limits.snapshot_max_total_bytes",
            256,
            20 * 1024 * 1024,
        ),
    }
    if normalized["snapshot_max_total_bytes"] < normalized["snapshot_max_file_bytes"]:
        raise ContractError(
            "validation_error",
            "snapshot_max_total_bytes must be at least snapshot_max_file_bytes.",
        )
    return normalized


def _validate_file_root_separation(
    vault_path: str,
    output_root: str,
    discovery: Sequence[Dict[str, Any]],
    content: Sequence[Dict[str, Any]],
    exclude_globs: Sequence[str],
) -> None:
    vault = Path(vault_path)
    weekly_output = vault.joinpath(*PurePosixPath(output_root).parts)
    goals_root = vault / "Goals"
    configured_globs = set(exclude_globs)
    for entry in list(discovery) + list(content):
        root = Path(entry["path"])
        if _same_or_within_casefold(root, weekly_output):
            raise ContractError(
                "unsafe_scope",
                "A file root cannot equal or sit inside the weekly output root.",
            )
        if _same_or_within_casefold(root, goals_root):
            raise ContractError(
                "unsafe_scope",
                "Structured Goals cannot be configured as an ordinary file root.",
            )
        if _same_or_within_casefold(weekly_output, root):
            required: Set[str] = {
                PurePosixPath(os.path.relpath(weekly_output, root)).as_posix()
                + "/**"
            }
            if _same_or_within_casefold(goals_root, root):
                required.add(
                    PurePosixPath(os.path.relpath(goals_root, root)).as_posix()
                    + "/**"
                )
            missing = sorted(required.difference(configured_globs))
            if missing:
                raise ContractError(
                    "unsafe_scope",
                    "A broad file root must explicitly exclude weekly output and structured Goals.",
                    {"required_exclude_globs": missing},
                )


def _validate_vault_path_live(raw: Any) -> str:
    vault_path = _validate_directory_path(
        raw, "config.vault.path", writable=True, live=True
    )
    obsidian = Path(vault_path) / ".obsidian"
    try:
        obsidian_stat = os.lstat(obsidian)
    except FileNotFoundError as exc:
        raise ContractError(
            "validation_error", "config.vault.path must contain a .obsidian directory."
        ) from exc
    except OSError as exc:
        raise ContractError(
            "unsafe_path", "config.vault.path .obsidian entry cannot be inspected."
        ) from exc
    if stat.S_ISLNK(obsidian_stat.st_mode) or not stat.S_ISDIR(obsidian_stat.st_mode):
        raise ContractError(
            "unsafe_path", "config.vault.path has an unsafe .obsidian entry."
        )
    return vault_path


def _validate_config(raw: Any, live: bool = True) -> Dict[str, Any]:
    config = _require_object(raw, "config")
    keys = {
        "schema_version",
        "timezone",
        "week_start",
        "vault",
        "files",
        "git",
        "eventkit",
        "notes",
        "mail",
        "limits",
    }
    _ensure_keys(config, keys, "config")
    _require_keys(config, keys, "config")
    if config["schema_version"] != SCHEMA_VERSION:
        raise ContractError(
            "unsupported_schema", f"config.schema_version must be {SCHEMA_VERSION}."
        )

    timezone = _require_string(config["timezone"], "config.timezone", 128)
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ContractError(
            "validation_error", "config.timezone must be a known IANA timezone."
        ) from exc
    if config["week_start"] != "monday":
        raise ContractError(
            "validation_error", "config.week_start must be 'monday'."
        )

    vault_raw = _require_object(config["vault"], "config.vault")
    _ensure_keys(vault_raw, {"path", "output_root", "goals_read"}, "config.vault")
    _require_keys(vault_raw, {"path", "output_root", "goals_read"}, "config.vault")
    if live:
        vault_path = _validate_vault_path_live(vault_raw["path"])
    else:
        vault_path = _validate_directory_path(
            vault_raw["path"], "config.vault.path", writable=True, live=False
        )
    output_root = _validate_relative_path(
        vault_raw["output_root"], "config.vault.output_root", 512
    )
    if PurePosixPath(output_root).parts[0].startswith("."):
        raise ContractError(
            "unsafe_scope", "config.vault.output_root cannot use a hidden vault directory."
        )
    if PurePosixPath(output_root).parts[0].casefold() == "goals":
        raise ContractError(
            "unsafe_scope",
            "config.vault.output_root must stay outside the structured Goals root.",
        )
    goals_read = _require_bool(vault_raw["goals_read"], "config.vault.goals_read")

    files_raw = _require_object(config["files"], "config.files")
    _ensure_keys(
        files_raw,
        {"discovery_roots", "content_roots", "exclude_globs"},
        "config.files",
    )
    _require_keys(
        files_raw,
        {"discovery_roots", "content_roots", "exclude_globs"},
        "config.files",
    )
    discovery = _validate_root_entries(
        files_raw["discovery_roots"],
        "config.files.discovery_roots",
        False,
        live=live,
    )
    content = _validate_root_entries(
        files_raw["content_roots"],
        "config.files.content_roots",
        True,
        live=live,
    )
    all_file_ids = [entry["id"] for entry in discovery + content]
    if len(all_file_ids) != len(set(all_file_ids)):
        raise ContractError(
            "validation_error",
            "Discovery and content roots must not reuse the same id.",
        )
    exclude_raw = files_raw["exclude_globs"]
    if not isinstance(exclude_raw, list) or len(exclude_raw) > 128:
        raise ContractError(
            "validation_error",
            "config.files.exclude_globs must be an array with at most 128 entries.",
        )
    exclude_globs: List[str] = []
    for index, glob_raw in enumerate(exclude_raw):
        glob = _require_string(
            glob_raw, f"config.files.exclude_globs[{index}]", 256
        )
        if glob.startswith(("/", "~")) or ".." in PurePosixPath(glob).parts:
            raise ContractError(
                "validation_error", "exclude globs must be relative patterns."
            )
        exclude_globs.append(glob)
    _validate_file_root_separation(
        vault_path,
        output_root,
        discovery,
        content,
        exclude_globs,
    )

    git_raw = _require_object(config["git"], "config.git")
    _ensure_keys(git_raw, {"repositories"}, "config.git")
    _require_keys(git_raw, {"repositories"}, "config.git")
    repositories = _validate_git_entries(git_raw["repositories"], live=live)

    eventkit_raw = _require_object(config["eventkit"], "config.eventkit")
    _ensure_keys(
        eventkit_raw, {"calendar_ids", "reminder_list_ids"}, "config.eventkit"
    )
    _require_keys(
        eventkit_raw, {"calendar_ids", "reminder_list_ids"}, "config.eventkit"
    )

    notes_raw = _require_object(config["notes"], "config.notes")
    _ensure_keys(notes_raw, {"scopes"}, "config.notes")
    _require_keys(notes_raw, {"scopes"}, "config.notes")
    mail_raw = _require_object(config["mail"], "config.mail")
    _ensure_keys(mail_raw, {"scopes"}, "config.mail")
    _require_keys(mail_raw, {"scopes"}, "config.mail")

    return {
        "eventkit": {
            "calendar_ids": _validate_string_id_list(
                eventkit_raw["calendar_ids"], "config.eventkit.calendar_ids"
            ),
            "reminder_list_ids": _validate_string_id_list(
                eventkit_raw["reminder_list_ids"],
                "config.eventkit.reminder_list_ids",
            ),
        },
        "files": {
            "content_roots": content,
            "discovery_roots": discovery,
            "exclude_globs": exclude_globs,
        },
        "git": {"repositories": repositories},
        "limits": _validate_limits(config["limits"]),
        "mail": {
            "scopes": _validate_scope_entries(
                mail_raw["scopes"],
                "config.mail.scopes",
                "mailbox_id",
                locator_maximum=2048,
                require_mail_semantics=True,
            )
        },
        "notes": {
            "scopes": _validate_scope_entries(
                notes_raw["scopes"], "config.notes.scopes", "folder_id"
            )
        },
        "schema_version": SCHEMA_VERSION,
        "timezone": timezone,
        "vault": {
            "goals_read": goals_read,
            "output_root": output_root,
            "path": vault_path,
        },
        "week_start": "monday",
    }


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def _assert_storage_ancestry(path: Path) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current = current / component
        try:
            item_stat = os.lstat(current)
        except FileNotFoundError:
            break
        except OSError as exc:
            raise ContractError(
                "unsafe_storage", "Private state path ancestry cannot be inspected."
            ) from exc
        if stat.S_ISLNK(item_stat.st_mode):
            raise ContractError(
                "unsafe_storage", "Private state path must not contain symlink components."
            )


def _ensure_secure_directory(path: Path, create: bool) -> None:
    _assert_storage_ancestry(path)
    if path.exists() or path.is_symlink():
        item_stat = os.lstat(path)
        if stat.S_ISLNK(item_stat.st_mode) or not stat.S_ISDIR(item_stat.st_mode):
            raise ContractError("unsafe_storage", "Private state path is not a real directory.")
    elif create:
        try:
            path.mkdir(parents=True, mode=0o700)
            os.chmod(path, 0o700, follow_symlinks=False)
        except OSError as exc:
            raise ContractError("storage_error", "Private state directory cannot be created.") from exc
    else:
        raise ContractError("storage_error", "Private state directory does not exist.")
    _assert_storage_ancestry(path)
    item_stat = os.lstat(path)
    if item_stat.st_uid != os.geteuid():
        raise ContractError("unsafe_owner", "Private state directory has the wrong owner.")
    if stat.S_IMODE(item_stat.st_mode) != 0o700:
        raise ContractError(
            "unsafe_permissions", "Private state directory permissions must be 0700."
        )


def _ensure_secure_file_stat(file_stat: os.stat_result, context: str) -> None:
    if not stat.S_ISREG(file_stat.st_mode):
        raise ContractError("unsafe_storage", f"{context} must be a regular file.")
    if file_stat.st_uid != os.geteuid():
        raise ContractError("unsafe_owner", f"{context} has the wrong owner.")
    if stat.S_IMODE(file_stat.st_mode) != 0o600:
        raise ContractError(
            "unsafe_permissions", f"{context} permissions must be 0600."
        )


def _read_private_bytes(path: Path, maximum: int, context: str) -> bytes:
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        raise
    if stat.S_ISLNK(before.st_mode):
        raise ContractError("unsafe_storage", f"{context} must not be a symlink.")
    _ensure_secure_file_stat(before, context)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ContractError("storage_error", f"{context} cannot be opened.") from exc
    try:
        opened = os.fstat(descriptor)
        _ensure_secure_file_stat(opened, context)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ContractError("unsafe_storage", f"{context} changed while opening.")
        chunks: List[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65_536, maximum + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > maximum:
                raise ContractError("state_too_large", f"{context} exceeds its size limit.")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _json_from_private_file(path: Path, maximum: int, context: str) -> Dict[str, Any]:
    raw = _read_private_bytes(path, maximum, context)
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ContractError("corrupt_state", f"{context} is not valid JSON.") from exc
    if not isinstance(decoded, dict):
        raise ContractError("corrupt_state", f"{context} must contain a JSON object.")
    return decoded


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _maybe_failpoint(name: str, test_mode: bool) -> None:
    if test_mode and os.environ.get(TEST_FAILPOINT_ENV) == name:
        raise ContractError("test_injected_failure", f"Injected test failure: {name}.")


def _write_size_limit(default: int, environment_name: str, test_mode: bool) -> int:
    if not test_mode:
        return default
    raw = os.environ.get(environment_name)
    if raw is None:
        return default
    try:
        limit = int(raw)
    except ValueError as exc:
        raise ContractError(
            "unsafe_test_override", "A test-only size limit is not an integer."
        ) from exc
    if not 256 <= limit <= default:
        raise ContractError(
            "unsafe_test_override",
            "A test-only size limit must be between 256 and its production limit.",
        )
    return limit


def _atomic_write_bytes(path: Path, payload: bytes, test_mode: bool, failpoint: str) -> None:
    try:
        existing = os.lstat(path)
    except FileNotFoundError:
        existing = None
    if existing is not None:
        if stat.S_ISLNK(existing.st_mode):
            raise ContractError("unsafe_storage", "Atomic write target must not be a symlink.")
        _ensure_secure_file_stat(existing, "Atomic write target")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.tmp-", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _maybe_failpoint(failpoint, test_mode)
        os.replace(temporary, path)
        os.chmod(path, 0o600, follow_symlinks=False)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _encode_json_document(
    payload: Dict[str, Any], maximum: int, context: str
) -> bytes:
    encoded = (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            separators=(",", ": "),
        )
        + "\n"
    ).encode("utf-8")
    if len(encoded) > maximum:
        raise ContractError(
            "state_too_large",
            f"{context} exceeds its encoded size limit; no file was replaced.",
            {"encoded_bytes": len(encoded), "maximum_bytes": maximum},
        )
    return encoded


def _atomic_write_json(
    path: Path,
    payload: Dict[str, Any],
    test_mode: bool,
    failpoint: str,
    maximum: int,
) -> None:
    encoded = _encode_json_document(payload, maximum, path.name)
    _atomic_write_bytes(path, encoded, test_mode, failpoint)


@contextlib.contextmanager
def _locked_storage(create: bool = True) -> Iterator[Tuple[Path, bool]]:
    root, test_mode = _storage_root()
    _ensure_secure_directory(root, create=create)
    lock_path = root / LOCK_NAME
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise ContractError("storage_error", "Private state lock cannot be opened.") from exc
    try:
        lock_stat = os.fstat(descriptor)
        _ensure_secure_file_stat(lock_stat, "Private state lock")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield root, test_mode
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _cleanup_atomic_temps(root: Path) -> None:
    prefixes = (f".{CONFIG_NAME}.tmp-", f".{STATE_NAME}.tmp-")
    for child in root.iterdir():
        if not child.name.startswith(prefixes):
            continue
        item_stat = os.lstat(child)
        _ensure_secure_file_stat(item_stat, "Abandoned atomic temporary file")
        child.unlink()


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _parse_config_document(document: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    _ensure_keys(document, {"schema_version", "revision", "updated_at", "config"}, CONFIG_NAME)
    _require_keys(document, {"schema_version", "revision", "updated_at", "config"}, CONFIG_NAME)
    if document["schema_version"] != SCHEMA_VERSION:
        raise ContractError("unsupported_schema", f"{CONFIG_NAME} has an unknown schema.")
    revision = _require_int(document["revision"], f"{CONFIG_NAME}.revision", 1, 2**63 - 1)
    _require_string(document["updated_at"], f"{CONFIG_NAME}.updated_at", 64)
    # Stored config remains recoverable when an approved external path later
    # moves, disappears, or becomes unsafe. Operations validate only the live
    # paths they are about to use; config.set validates every proposed path.
    return revision, _validate_config(document["config"], live=False)


def _read_config(root: Path) -> Tuple[int, Optional[Dict[str, Any]]]:
    path = root / CONFIG_NAME
    try:
        document = _json_from_private_file(path, MAX_CONFIG_BYTES, CONFIG_NAME)
    except FileNotFoundError:
        return 0, None
    return _parse_config_document(document)


def _config_live_path_diagnostics(config: Dict[str, Any]) -> List[Dict[str, str]]:
    diagnostics: List[Dict[str, str]] = []

    def inspect(
        field: str, validator: Any, scope_id: Optional[str] = None
    ) -> None:
        diagnostic = {"field": field, "status": "available"}
        if scope_id is not None:
            diagnostic["id"] = scope_id
        try:
            validator()
        except ContractError as exc:
            diagnostic["code"] = exc.code
            diagnostic["status"] = "unavailable"
        except OSError:
            diagnostic["code"] = "unsafe_path"
            diagnostic["status"] = "unavailable"
        diagnostics.append(diagnostic)

    inspect(
        "config.vault.path",
        lambda: _validate_vault_path_live(config["vault"]["path"]),
    )
    for root_kind in ("discovery_roots", "content_roots"):
        for index, entry in enumerate(config["files"][root_kind]):
            inspect(
                f"config.files.{root_kind}[{index}].path",
                lambda entry=entry, root_kind=root_kind, index=index: _validate_directory_path(
                    entry["path"],
                    f"config.files.{root_kind}[{index}].path",
                    live=True,
                ),
                entry["id"],
            )
    for index, entry in enumerate(config["git"]["repositories"]):
        inspect(
            f"config.git.repositories[{index}].path",
            lambda entry=entry: _validate_git_entries([entry], live=True),
            entry["id"],
        )
    return diagnostics


def _validate_week_id(value: Any, field: str = "week_id") -> str:
    week_id = _require_string(value, field, 8)
    match = WEEK_ID_RE.fullmatch(week_id)
    if not match:
        raise ContractError("validation_error", f"{field} must use ISO YYYY-Www format.")
    year = int(match.group(1))
    week = int(match.group(2))
    try:
        dt.date.fromisocalendar(year, week, 1)
    except ValueError as exc:
        raise ContractError("validation_error", f"{field} is not a real ISO week.") from exc
    return week_id


def _validate_review_id(value: Any, field: str = "review_id") -> str:
    review_id = _require_string(value, field, 128)
    if not REVIEW_ID_RE.fullmatch(review_id):
        raise ContractError(
            "validation_error",
            f"{field} must use letters, digits, dots, underscores, or hyphens.",
        )
    return review_id


def _parse_aware_timestamp(value: Any, field: str) -> dt.datetime:
    raw = _require_string(value, field, 64)
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ContractError(
            "validation_error", f"{field} must be an ISO 8601 timestamp."
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(
            "validation_error", f"{field} must include an explicit UTC offset."
        )
    return parsed


def _validate_review_window(
    raw: Any, week_id: str, expected_timezone: Optional[str] = None
) -> Dict[str, str]:
    window = _require_object(raw, "window")
    keys = {"start", "end_exclusive", "collected_through", "timezone"}
    _ensure_keys(window, keys, "window")
    _require_keys(window, keys, "window")
    timezone = _require_string(window["timezone"], "window.timezone", 128)
    if expected_timezone is not None and timezone != expected_timezone:
        raise ContractError(
            "validation_error", "window.timezone must match the confirmed config."
        )
    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ContractError(
            "validation_error", "window.timezone must be a known IANA timezone."
        ) from exc

    start = _parse_aware_timestamp(window["start"], "window.start")
    end = _parse_aware_timestamp(window["end_exclusive"], "window.end_exclusive")
    collected = _parse_aware_timestamp(
        window["collected_through"], "window.collected_through"
    )
    match = WEEK_ID_RE.fullmatch(week_id)
    assert match is not None
    monday = dt.date.fromisocalendar(int(match.group(1)), int(match.group(2)), 1)
    expected_start = dt.datetime.combine(monday, dt.time.min, tzinfo=zone)
    expected_end = dt.datetime.combine(
        monday + dt.timedelta(days=7), dt.time.min, tzinfo=zone
    )
    if (
        start.isoformat() != expected_start.isoformat()
        or end.isoformat() != expected_end.isoformat()
    ):
        raise ContractError(
            "validation_error",
            "window start/end_exclusive must be the local ISO-week boundaries.",
        )
    localized_collected = collected.astimezone(zone)
    if (
        localized_collected.replace(tzinfo=None) != collected.replace(tzinfo=None)
        or localized_collected.utcoffset() != collected.utcoffset()
    ):
        raise ContractError(
            "validation_error",
            "window.collected_through offset must match window.timezone.",
        )
    if not start <= collected <= end:
        raise ContractError(
            "validation_error",
            "window.collected_through must fall inside the staged week, inclusive of end_exclusive.",
        )
    return {
        "collected_through": collected.isoformat(),
        "end_exclusive": end.isoformat(),
        "start": start.isoformat(),
        "timezone": timezone,
    }


def _validate_coverage(raw: Any) -> Dict[str, str]:
    coverage = _require_object(raw, "coverage")
    if not 1 <= len(coverage) <= 128:
        raise ContractError(
            "validation_error", "coverage must contain from 1 through 128 logical sources."
        )
    normalized: Dict[str, str] = {}
    for logical_source, status_value in coverage.items():
        source_id = _require_scope_id(logical_source, "coverage source id")
        if status_value not in COVERAGE_STATUSES:
            raise ContractError(
                "validation_error", "coverage values must use a supported status."
            )
        normalized[source_id] = status_value
    return dict(sorted(normalized.items()))


def _default_state() -> Dict[str, Any]:
    return {
        "checkpoint": None,
        "pending_review": None,
        "receipts": {},
        "revision": 0,
        "schema_version": SCHEMA_VERSION,
    }


def _parse_digest_entries(raw: Any, context: str) -> Dict[str, Dict[str, str]]:
    entries_raw = _require_object(raw, context)
    entries: Dict[str, Dict[str, str]] = {}
    for source_digest, entry_raw in entries_raw.items():
        if not SHA256_RE.fullmatch(source_digest):
            raise ContractError("corrupt_state", f"{context} has an invalid source digest.")
        entry = _require_object(entry_raw, f"{context} entry")
        _ensure_keys(
            entry, {"content_sha256", "snapshot_name"}, f"{context} entry"
        )
        _require_keys(entry, {"content_sha256"}, f"{context} entry")
        normalized_entry = {
            "content_sha256": _require_sha256(
                entry["content_sha256"], f"{context}.content_sha256"
            )
        }
        if "snapshot_name" in entry:
            snapshot_name = _require_string(
                entry["snapshot_name"], f"{context}.snapshot_name", 160
            )
            if not SNAPSHOT_NAME_RE.fullmatch(snapshot_name):
                raise ContractError("corrupt_state", f"{context} snapshot name is invalid.")
            normalized_entry["snapshot_name"] = snapshot_name
        entries[source_digest] = normalized_entry
    return entries


def _parse_optional_identity(value: Any, field: str) -> Optional[str]:
    if value is None:
        return None
    return _require_sha256(value, field)


def _parse_receipts(raw: Any) -> Dict[str, Dict[str, Any]]:
    receipts_raw = _require_object(raw, f"{STATE_NAME}.receipts")
    if len(receipts_raw) > MAX_RECEIPTS:
        raise ContractError("corrupt_state", "Receipt count exceeds its durable limit.")
    receipts: Dict[str, Dict[str, Any]] = {}
    for key, raw_receipt in receipts_raw.items():
        review_id = _validate_review_id(key, f"{STATE_NAME}.receipts review_id")
        receipt = _require_object(
            raw_receipt, f"{STATE_NAME}.receipts[{review_id}]"
        )
        keys = {
            "config_revision",
            "index_sha256",
            "promoted_at",
            "report_sha256",
            "request_digest",
            "scope_fingerprint",
            "state_revision",
            "week_id",
        }
        _ensure_keys(receipt, keys, f"{STATE_NAME}.receipts[{review_id}]")
        _require_keys(receipt, keys, f"{STATE_NAME}.receipts[{review_id}]")
        receipts[review_id] = {
            "config_revision": _require_int(
                receipt["config_revision"], "receipt.config_revision", 1, 2**63 - 1
            ),
            "index_sha256": _require_sha256(
                receipt["index_sha256"], "receipt.index_sha256"
            ),
            "promoted_at": _parse_aware_timestamp(
                receipt["promoted_at"], "receipt.promoted_at"
            ).isoformat(),
            "report_sha256": _require_sha256(
                receipt["report_sha256"], "receipt.report_sha256"
            ),
            "request_digest": _require_sha256(
                receipt["request_digest"], "receipt.request_digest"
            ),
            "scope_fingerprint": _require_sha256(
                receipt["scope_fingerprint"], "receipt.scope_fingerprint"
            ),
            "state_revision": _require_int(
                receipt["state_revision"], "receipt.state_revision", 1, 2**63 - 1
            ),
            "week_id": _validate_week_id(receipt["week_id"], "receipt.week_id"),
        }
    return receipts


def _parse_state_document(document: Dict[str, Any]) -> Dict[str, Any]:
    if document.get("schema_version") != SCHEMA_VERSION:
        raise ContractError("unsupported_schema", f"{STATE_NAME} has an unknown schema.")
    state_keys = {
        "schema_version",
        "revision",
        "updated_at",
        "checkpoint",
        "pending_review",
        "receipts",
    }
    _ensure_keys(document, state_keys, STATE_NAME)
    _require_keys(document, state_keys, STATE_NAME)
    revision = _require_int(document["revision"], f"{STATE_NAME}.revision", 1, 2**63 - 1)
    _require_string(document["updated_at"], f"{STATE_NAME}.updated_at", 64)
    checkpoint_raw = document["checkpoint"]
    if checkpoint_raw is None:
        checkpoint = None
    else:
        checkpoint = _require_object(checkpoint_raw, f"{STATE_NAME}.checkpoint")
        keys = {
            "week_id",
            "review_id",
            "report_sha256",
            "index_sha256",
            "config_revision",
            "scope_fingerprint",
            "baseline",
        }
        _ensure_keys(checkpoint, keys, f"{STATE_NAME}.checkpoint")
        _require_keys(checkpoint, keys, f"{STATE_NAME}.checkpoint")
        week_id = _validate_week_id(checkpoint["week_id"], f"{STATE_NAME}.checkpoint.week_id")
        review_id = _validate_review_id(
            checkpoint["review_id"], f"{STATE_NAME}.checkpoint.review_id"
        )
        report_sha = _require_sha256(
            checkpoint["report_sha256"], f"{STATE_NAME}.checkpoint.report_sha256"
        )
        index_sha = _require_sha256(
            checkpoint["index_sha256"], f"{STATE_NAME}.checkpoint.index_sha256"
        )
        config_revision = _require_int(
            checkpoint["config_revision"],
            f"{STATE_NAME}.checkpoint.config_revision",
            1,
            2**63 - 1,
        )
        scope_fingerprint = _require_sha256(
            checkpoint["scope_fingerprint"],
            f"{STATE_NAME}.checkpoint.scope_fingerprint",
        )
        baseline = _parse_digest_entries(
            checkpoint["baseline"], f"{STATE_NAME}.checkpoint.baseline"
        )
        checkpoint = {
            "baseline": baseline,
            "config_revision": config_revision,
            "index_sha256": index_sha,
            "report_sha256": report_sha,
            "review_id": review_id,
            "scope_fingerprint": scope_fingerprint,
            "week_id": week_id,
        }
    pending_raw = document["pending_review"]
    if pending_raw is None:
        pending_review = None
    else:
        pending = _require_object(pending_raw, f"{STATE_NAME}.pending_review")
        pending_keys = {
            "review_id",
            "week_id",
            "window",
            "coverage",
            "config_revision",
            "scope_fingerprint",
            "preview_sha256",
            "report_sha256",
            "index_sha256",
            "observations",
            "staged_at",
            "attempt",
            "index_preimage",
            "output_identity",
            "phase",
            "report_preimage",
            "vault_identity",
        }
        _ensure_keys(pending, pending_keys, f"{STATE_NAME}.pending_review")
        _require_keys(pending, pending_keys, f"{STATE_NAME}.pending_review")
        pending_week = _validate_week_id(
            pending["week_id"], f"{STATE_NAME}.pending_review.week_id"
        )
        phase = pending["phase"]
        if phase not in {"staged", "writing"}:
            raise ContractError("corrupt_state", "Pending review phase is invalid.")
        attempt_raw = pending["attempt"]
        if attempt_raw is None:
            attempt = None
        else:
            attempt_object = _require_object(
                attempt_raw, f"{STATE_NAME}.pending_review.attempt"
            )
            attempt_keys = {
                "output_identity",
                "report_parent_identity",
                "request_digest",
                "started_at",
                "starting_state_revision",
            }
            _ensure_keys(
                attempt_object,
                attempt_keys,
                f"{STATE_NAME}.pending_review.attempt",
            )
            _require_keys(
                attempt_object,
                attempt_keys,
                f"{STATE_NAME}.pending_review.attempt",
            )
            attempt = {
                "output_identity": _parse_optional_identity(
                    attempt_object["output_identity"], "attempt.output_identity"
                ),
                "report_parent_identity": _parse_optional_identity(
                    attempt_object["report_parent_identity"],
                    "attempt.report_parent_identity",
                ),
                "request_digest": _require_sha256(
                    attempt_object["request_digest"], "attempt.request_digest"
                ),
                "started_at": _parse_aware_timestamp(
                    attempt_object["started_at"], "attempt.started_at"
                ).isoformat(),
                "starting_state_revision": _require_int(
                    attempt_object["starting_state_revision"],
                    "attempt.starting_state_revision",
                    1,
                    2**63 - 1,
                ),
            }
        if (phase == "staged") != (attempt is None):
            raise ContractError(
                "corrupt_state", "Pending review phase and attempt are inconsistent."
            )
        output_identity_raw = pending["output_identity"]
        if output_identity_raw == "absent":
            output_identity = "absent"
        else:
            output_identity = _require_sha256(
                output_identity_raw, f"{STATE_NAME}.pending_review.output_identity"
            )
        pending_review = {
            "attempt": attempt,
            "config_revision": _require_int(
                pending["config_revision"],
                f"{STATE_NAME}.pending_review.config_revision",
                1,
                2**63 - 1,
            ),
            "coverage": _validate_coverage(pending["coverage"]),
            "index_sha256": _require_sha256(
                pending["index_sha256"],
                f"{STATE_NAME}.pending_review.index_sha256",
            ),
            "index_preimage": _parse_expected_preimage(
                pending["index_preimage"],
                f"{STATE_NAME}.pending_review.index_preimage",
            ),
            "observations": _parse_digest_entries(
                pending["observations"],
                f"{STATE_NAME}.pending_review.observations",
            ),
            "preview_sha256": _require_sha256(
                pending["preview_sha256"],
                f"{STATE_NAME}.pending_review.preview_sha256",
            ),
            "phase": phase,
            "report_sha256": _require_sha256(
                pending["report_sha256"],
                f"{STATE_NAME}.pending_review.report_sha256",
            ),
            "report_preimage": _parse_expected_preimage(
                pending["report_preimage"],
                f"{STATE_NAME}.pending_review.report_preimage",
            ),
            "review_id": _validate_review_id(
                pending["review_id"], f"{STATE_NAME}.pending_review.review_id"
            ),
            "scope_fingerprint": _require_sha256(
                pending["scope_fingerprint"],
                f"{STATE_NAME}.pending_review.scope_fingerprint",
            ),
            "staged_at": _parse_aware_timestamp(
                pending["staged_at"], f"{STATE_NAME}.pending_review.staged_at"
            ).isoformat(),
            "output_identity": output_identity,
            "vault_identity": _require_sha256(
                pending["vault_identity"],
                f"{STATE_NAME}.pending_review.vault_identity",
            ),
            "week_id": pending_week,
            "window": _validate_review_window(pending["window"], pending_week),
        }
    return {
        "checkpoint": checkpoint,
        "pending_review": pending_review,
        "receipts": _parse_receipts(document["receipts"]),
        "revision": revision,
        "schema_version": SCHEMA_VERSION,
    }


def _read_state(root: Path) -> Dict[str, Any]:
    path = root / STATE_NAME
    try:
        document = _json_from_private_file(path, MAX_STATE_BYTES, STATE_NAME)
    except FileNotFoundError:
        return _default_state()
    return _parse_state_document(document)


def _snapshot_references(state: Dict[str, Any]) -> Set[str]:
    checkpoint = state.get("checkpoint")
    pending = state.get("pending_review")
    references: Set[str] = set()
    if checkpoint:
        references.update(
            entry["snapshot_name"]
            for entry in checkpoint["baseline"].values()
            if "snapshot_name" in entry
        )
    if pending:
        references.update(
            entry["snapshot_name"]
            for entry in pending["observations"].values()
            if "snapshot_name" in entry
        )
    return references


def _baseline_compatibility(
    state: Dict[str, Any], config: Dict[str, Any], config_revision: int
) -> Dict[str, Any]:
    checkpoint = state.get("checkpoint")
    if checkpoint is None:
        return {
            "basis": "no_checkpoint",
            "checkpoint_config_revision": None,
            "current_config_revision": config_revision,
            "status": "compatible",
        }
    current_fingerprint = _baseline_scope_fingerprint(config)
    if checkpoint["scope_fingerprint"] != current_fingerprint:
        return {
            "basis": "scope_fingerprint",
            "checkpoint_config_revision": checkpoint["config_revision"],
            "current_config_revision": config_revision,
            "status": "incompatible",
        }
    return {
        "basis": "scope_fingerprint",
        "checkpoint_config_revision": checkpoint["config_revision"],
        "current_config_revision": config_revision,
        "status": "compatible",
    }


def _require_baseline_compatible(
    state: Dict[str, Any], config: Dict[str, Any], config_revision: int
) -> Dict[str, Any]:
    compatibility = _baseline_compatibility(state, config, config_revision)
    if compatibility["status"] != "compatible":
        raise ContractError(
            "baseline_incompatible",
            "Configured source identity changed; explicitly reset the baseline before comparing or committing.",
            {
                "checkpoint_config_revision": compatibility[
                    "checkpoint_config_revision"
                ],
                "current_config_revision": compatibility["current_config_revision"],
            },
        )
    return compatibility


def _require_no_pending_review(state: Dict[str, Any], operation: str) -> None:
    pending = state.get("pending_review")
    if pending is not None:
        raise ContractError(
            "pending_review_active",
            f"{operation} cannot run while a durable weekly-review lease is active.",
            {
                "pending_review_id": pending["review_id"],
                "pending_week_id": pending["week_id"],
            },
        )


def _ensure_snapshots_directory(root: Path) -> Path:
    path = root / SNAPSHOTS_NAME
    _ensure_secure_directory(path, create=True)
    return path


def _cleanup_snapshots(root: Path, referenced: Set[str]) -> None:
    path = root / SNAPSHOTS_NAME
    if not path.exists() and not path.is_symlink():
        return
    _ensure_secure_directory(path, create=False)
    for child in path.iterdir():
        item_stat = os.lstat(child)
        if stat.S_ISLNK(item_stat.st_mode) or not stat.S_ISREG(item_stat.st_mode):
            raise ContractError("unsafe_storage", "Snapshots directory contains an unsafe entry.")
        _ensure_secure_file_stat(item_stat, "Snapshot file")
        if child.name.startswith(".snapshot.tmp-") or child.name not in referenced:
            child.unlink()


def _snapshot_inventory(
    root: Path, state: Dict[str, Any]
) -> Tuple[int, int, Set[str]]:
    referenced = _snapshot_references(state)
    total_bytes = 0
    for name in referenced:
        total_bytes += _private_snapshot_size(
            root / SNAPSHOTS_NAME / name,
            2 * 1024 * 1024,
        )
    return len(referenced), total_bytes, referenced


def _snapshot_enabled(config: Dict[str, Any], root_id: str) -> bool:
    for entry in config["files"]["content_roots"]:
        if entry["id"] == root_id:
            return bool(entry["snapshot_text"])
    return False


def _snapshot_opt_out_count(
    current: Optional[Dict[str, Any]], proposed: Dict[str, Any]
) -> int:
    if current is None:
        return 0
    current_enabled = {
        entry["id"]
        for entry in current["files"]["content_roots"]
        if entry["snapshot_text"]
    }
    proposed_enabled = {
        entry["id"]
        for entry in proposed["files"]["content_roots"]
        if entry["snapshot_text"]
    }
    return len(current_enabled.difference(proposed_enabled))


def _configured_scope_ids(config: Dict[str, Any], kind: str) -> Set[str]:
    if kind == "file":
        return {entry["id"] for entry in config["files"]["content_roots"]}
    if kind == "git":
        return {entry["id"] for entry in config["git"]["repositories"]}
    if kind == "note":
        return {entry["id"] for entry in config["notes"]["scopes"]}
    if kind == "mail":
        return {entry["id"] for entry in config["mail"]["scopes"]}
    if kind == "goal":
        return {"vault"} if config["vault"]["goals_read"] else set()
    if kind in {"calendar", "reminder"}:
        return {"eventkit"}
    return set()


def _validate_observation_locator_scope(
    config: Dict[str, Any], kind: str, scope_id: str, locator: str, field: str
) -> None:
    vault = Path(config["vault"]["path"])
    weekly_output = vault.joinpath(
        *PurePosixPath(config["vault"]["output_root"]).parts
    )
    goals_root = vault / "Goals"
    if kind == "goal":
        parts = PurePosixPath(locator).parts
        if not parts or parts[0] != "Goals":
            raise ContractError(
                "scope_not_configured",
                f"{field} must stay under the explicitly authorized Goals root.",
            )
        goal_candidate = vault.joinpath(*parts)
        if _same_or_within_casefold(goal_candidate, weekly_output):
            raise ContractError(
                "excluded_observation",
                f"{field} cannot ingest weekly output as a Goal document.",
            )
        return
    if kind != "file":
        return
    root_path: Optional[Path] = None
    for entry in config["files"]["content_roots"]:
        if entry["id"] == scope_id:
            root_path = Path(entry["path"])
            break
    if root_path is None:
        raise ContractError("scope_not_configured", f"{field} has no content root.")
    candidate = root_path.joinpath(*PurePosixPath(locator).parts)
    if _same_or_within_casefold(
        candidate, weekly_output
    ) or _same_or_within_casefold(candidate, goals_root):
        raise ContractError(
            "excluded_observation",
            f"{field} cannot ingest weekly output or structured Goal files.",
        )


def _source_digest(source: Dict[str, str]) -> str:
    canonical = json.dumps(
        source, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _baseline_scope_fingerprint(config: Dict[str, Any]) -> str:
    """Hash source identity without persisting paths or provider locators in state.

    Snapshot retention flags and numeric limits are deliberately excluded: they
    do not change which real-world source a digest identifies. Provider scope
    and filesystem identity are included, so reusing an alias for another root
    fails closed even when config revisions alone would be ambiguous.
    """

    identity = {
        "eventkit": {
            "calendar_ids": sorted(config["eventkit"]["calendar_ids"]),
            "reminder_list_ids": sorted(config["eventkit"]["reminder_list_ids"]),
        },
        "file_content_roots": sorted(
            (
                {"id": entry["id"], "path": entry["path"]}
                for entry in config["files"]["content_roots"]
            ),
            key=lambda entry: (entry["id"], entry["path"]),
        ),
        "git_repositories": sorted(
            config["git"]["repositories"],
            key=lambda entry: (entry["id"], entry["path"]),
        ),
        "mail_scopes": sorted(
            (
                {
                    "account_id": entry["account_id"],
                    "content_access": entry["content_access"],
                    "date_field": entry["date_field"],
                    "id": entry["id"],
                    "mailbox_id": entry["mailbox_id"],
                    "scope_kind": entry["scope_kind"],
                }
                for entry in config["mail"]["scopes"]
            ),
            key=lambda entry: entry["id"],
        ),
        "notes_scopes": sorted(
            (
                {
                    "account_id": entry["account_id"],
                    "content_access": entry["content_access"],
                    "folder_id": entry["folder_id"],
                    "id": entry["id"],
                }
                for entry in config["notes"]["scopes"]
            ),
            key=lambda entry: entry["id"],
        ),
        "vault": {
            "goals_read": config["vault"]["goals_read"],
            "path": config["vault"]["path"],
        },
    }
    canonical = json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _parse_observations(raw: Any, config: Dict[str, Any]) -> List[Dict[str, Any]]:
    maximum = min(5_000, config["limits"]["max_candidates_per_source"] * 16)
    if not isinstance(raw, list) or len(raw) > maximum:
        raise ContractError(
            "validation_error", f"observations must be an array with at most {maximum} entries."
        )
    parsed: List[Dict[str, Any]] = []
    item_ids: Set[str] = set()
    digests: Set[str] = set()
    total_text_bytes = 0
    for index, item_raw in enumerate(raw):
        field = f"observations[{index}]"
        item = _require_object(item_raw, field)
        _ensure_keys(item, {"item_id", "source", "sha256", "text"}, field)
        _require_keys(item, {"item_id", "source", "sha256"}, field)
        item_id = _require_string(item["item_id"], f"{field}.item_id", 128)
        if item_id in item_ids:
            raise ContractError("validation_error", "observations contains duplicate item_id values.")
        item_ids.add(item_id)
        source_raw = _require_object(item["source"], f"{field}.source")
        _ensure_keys(
            source_raw,
            {"kind", "scope_id", "locator", "container_id"},
            f"{field}.source",
        )
        _require_keys(source_raw, {"kind", "scope_id", "locator"}, f"{field}.source")
        kind = _require_string(source_raw["kind"], f"{field}.source.kind", 32)
        if kind not in SOURCE_KINDS:
            raise ContractError("validation_error", f"{field}.source.kind is unsupported.")
        scope_id = _require_scope_id(source_raw["scope_id"], f"{field}.source.scope_id")
        if scope_id not in _configured_scope_ids(config, kind):
            raise ContractError(
                "scope_not_configured",
                f"{field}.source.scope_id is not enabled for {kind}.",
            )
        if kind in {"calendar", "reminder"}:
            locator = _require_utf8_string(
                source_raw["locator"], f"{field}.source.locator", 4096
            )
        else:
            locator = _require_string(
                source_raw["locator"], f"{field}.source.locator", 2048
            )
        if kind in {"file", "goal"}:
            locator = _validate_relative_path(locator, f"{field}.source.locator", 2048)
        _validate_observation_locator_scope(
            config, kind, scope_id, locator, f"{field}.source.locator"
        )
        source = {"kind": kind, "locator": locator, "scope_id": scope_id}
        if kind in {"calendar", "reminder"}:
            _require_keys(source_raw, {"container_id"}, f"{field}.source")
            container_id = _require_utf8_string(
                source_raw["container_id"],
                f"{field}.source.container_id",
                4096,
            )
            configured_ids = (
                config["eventkit"]["calendar_ids"]
                if kind == "calendar"
                else config["eventkit"]["reminder_list_ids"]
            )
            if container_id not in configured_ids:
                raise ContractError(
                    "container_not_configured",
                    f"{field}.source.container_id is not in the configured {kind} allowlist.",
                )
            source["container_id"] = container_id
        elif "container_id" in source_raw:
            raise ContractError(
                "validation_error",
                f"{field}.source.container_id is only valid for calendar or reminder observations.",
            )
        digest = _source_digest(source)
        if digest in digests:
            raise ContractError("validation_error", "observations contains a duplicate source.")
        digests.add(digest)
        content_sha = _require_sha256(item["sha256"], f"{field}.sha256")
        normalized: Dict[str, Any] = {
            "content_sha256": content_sha,
            "item_id": item_id,
            "source": source,
            "source_digest": digest,
        }
        if "text" in item:
            if kind != "file" or not _snapshot_enabled(config, scope_id):
                raise ContractError(
                    "snapshot_not_authorized",
                    f"{field}.text requires snapshot_text=true on its file content root.",
                )
            text = item["text"]
            if not isinstance(text, str) or "\x00" in text:
                raise ContractError("validation_error", f"{field}.text must be plain UTF-8 text.")
            name = PurePosixPath(locator).name
            suffix = PurePosixPath(locator).suffix.lower()
            if (
                name.lower() in SENSITIVE_NAMES
                or suffix in SENSITIVE_SUFFIXES
                or suffix not in PLAINTEXT_SUFFIXES
            ):
                raise ContractError(
                    "snapshot_not_authorized",
                    f"{field}.text is not an allowed plaintext file type.",
                )
            encoded = text.encode("utf-8")
            if len(encoded) > config["limits"]["snapshot_max_file_bytes"]:
                raise ContractError(
                    "snapshot_limit",
                    f"{field}.text exceeds snapshot_max_file_bytes.",
                )
            if hashlib.sha256(encoded).hexdigest() != content_sha:
                raise ContractError(
                    "hash_mismatch", f"{field}.text does not match its declared sha256."
                )
            total_text_bytes += len(encoded)
            normalized["text"] = text
        parsed.append(normalized)
    if total_text_bytes > config["limits"]["snapshot_max_total_bytes"]:
        raise ContractError("snapshot_limit", "Observation text exceeds snapshot_max_total_bytes.")
    return parsed


def _read_snapshot(root: Path, name: str, maximum: int) -> Optional[str]:
    path = root / SNAPSHOTS_NAME / name
    try:
        raw = _read_private_bytes(path, maximum, "Snapshot file")
    except FileNotFoundError:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContractError("corrupt_state", "Snapshot file is not valid UTF-8.") from exc


def _bounded_diff(previous: str, current: str, maximum_lines: int) -> Dict[str, Any]:
    iterator = difflib.unified_diff(
        previous.splitlines(),
        current.splitlines(),
        fromfile="previous",
        tofile="current",
        lineterm="",
    )
    lines: List[str] = []
    truncated = False
    for line in iterator:
        if len(lines) >= maximum_lines:
            truncated = True
            break
        lines.append(line)
    return {
        "format": "unified",
        "lines": lines,
        "status": "computed",
        "truncated": truncated,
    }


def _compare_observations(
    root: Path,
    state: Dict[str, Any],
    observations: List[Dict[str, Any]],
    config: Dict[str, Any],
) -> List[Dict[str, Any]]:
    checkpoint = state.get("checkpoint")
    baseline = checkpoint["baseline"] if checkpoint else {}
    output: List[Dict[str, Any]] = []
    for item in observations:
        previous = baseline.get(item["source_digest"])
        result: Dict[str, Any] = {
            "comparison_basis": "sha256_only",
            "item_id": item["item_id"],
        }
        if previous is None:
            result["change"] = "new"
            result["content_diff"] = {
                "reason": "no_previous_baseline",
                "status": "unavailable",
            }
        elif previous["content_sha256"] == item["content_sha256"]:
            result["change"] = "unchanged"
            result["content_diff"] = {"status": "not_needed"}
        else:
            result["change"] = "modified"
            source = item["source"]
            if source["kind"] != "file" or not _snapshot_enabled(
                config, source["scope_id"]
            ):
                result["content_diff"] = {
                    "reason": "snapshot_not_enabled",
                    "status": "unavailable",
                }
            elif "text" not in item:
                result["content_diff"] = {
                    "reason": "current_text_not_supplied",
                    "status": "unavailable",
                }
            elif "snapshot_name" not in previous:
                result["content_diff"] = {
                    "reason": "no_previous_snapshot",
                    "status": "unavailable",
                }
            else:
                previous_text = _read_snapshot(
                    root,
                    previous["snapshot_name"],
                    config["limits"]["snapshot_max_file_bytes"],
                )
                if previous_text is None:
                    result["content_diff"] = {
                        "reason": "previous_snapshot_missing",
                        "status": "unavailable",
                    }
                else:
                    result["content_diff"] = _bounded_diff(
                        previous_text,
                        item["text"],
                        config["limits"]["max_diff_lines"],
                    )
        output.append(result)
    return output


def _parse_expected_preimage(raw: Any, field: str) -> Dict[str, str]:
    value = _require_object(raw, field)
    state_value = value.get("state")
    if state_value == "absent":
        _ensure_keys(value, {"state"}, field)
        return {"state": "absent"}
    if state_value == "sha256":
        _ensure_keys(value, {"state", "sha256"}, field)
        _require_keys(value, {"state", "sha256"}, field)
        return {
            "sha256": _require_sha256(value["sha256"], f"{field}.sha256"),
            "state": "sha256",
        }
    raise ContractError(
        "validation_error", f"{field}.state must be 'absent' or 'sha256'."
    )


def _identity_digest(item_stat: os.stat_result) -> str:
    canonical = (
        f"{item_stat.st_dev}:{item_stat.st_ino}:"
        f"{getattr(item_stat, 'st_gen', 0)}"
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        getattr(left, "st_gen", 0),
    ) == (
        right.st_dev,
        right.st_ino,
        getattr(right, "st_gen", 0),
    )


def _open_bound_vault(
    config: Dict[str, Any], expected_identity: Optional[str] = None
) -> Tuple[int, str]:
    vault = Path(config["vault"]["path"])
    if not vault.is_absolute():
        raise ContractError("unsafe_report", "The configured vault path is not absolute.")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor: Optional[int] = None
    try:
        descriptor = os.open(vault.anchor, flags)
        for component in vault.parts[1:]:
            if not component or component in {".", ".."} or "/" in component or "\x00" in component:
                raise ContractError("unsafe_report", "The configured vault has an unsafe component.")
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            opened_component = os.fstat(next_descriptor)
            if not stat.S_ISDIR(opened_component.st_mode):
                os.close(next_descriptor)
                raise ContractError(
                    "unsafe_report", "The configured vault ancestry is not a directory chain."
                )
            os.close(descriptor)
            descriptor = next_descriptor
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise ContractError("unsafe_report", "The configured vault cannot be bound safely.") from exc
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        raise
    assert descriptor is not None
    opened = os.fstat(descriptor)
    if not stat.S_ISDIR(opened.st_mode):
        os.close(descriptor)
        raise ContractError("unsafe_report", "The configured vault is not a directory.")
    identity = _identity_digest(opened)
    if expected_identity is not None and identity != expected_identity:
        os.close(descriptor)
        raise ContractError(
            "directory_identity_conflict",
            "The vault identity changed after the review was staged.",
        )
    return descriptor, identity


def _open_directory_chain_at(
    root_descriptor: int,
    parts: Sequence[str],
    *,
    create: bool,
) -> Optional[int]:
    current = os.dup(root_descriptor)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        for part in parts:
            if not part or part in {".", ".."} or "/" in part or "\x00" in part:
                raise ContractError("unsafe_report", "A directory component is unsafe.")
            try:
                next_descriptor = os.open(part, flags, dir_fd=current)
            except FileNotFoundError:
                if not create:
                    os.close(current)
                    return None
                try:
                    os.mkdir(part, 0o700, dir_fd=current)
                    os.fsync(current)
                    next_descriptor = os.open(part, flags, dir_fd=current)
                except OSError as exc:
                    raise ContractError(
                        "storage_error", "A bound weekly output directory cannot be created."
                    ) from exc
            except OSError as exc:
                raise ContractError(
                    "unsafe_report", "A weekly output directory component is unsafe."
                ) from exc
            opened = os.fstat(next_descriptor)
            if not stat.S_ISDIR(opened.st_mode):
                os.close(next_descriptor)
                raise ContractError("unsafe_report", "A weekly output component is not a directory.")
            os.close(current)
            current = next_descriptor
        return current
    except Exception:
        try:
            os.close(current)
        except OSError:
            pass
        raise


def _safe_component(name: str, context: str) -> str:
    if not name or name in {".", ".."} or "/" in name or "\x00" in name:
        raise ContractError("unsafe_report", f"{context} filename is unsafe.")
    return name


def _stat_at_optional(directory_descriptor: int, name: str, context: str) -> Optional[os.stat_result]:
    _safe_component(name, context)
    try:
        item_stat = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ContractError("storage_error", f"{context} cannot be inspected.") from exc
    if stat.S_ISLNK(item_stat.st_mode) or not stat.S_ISREG(item_stat.st_mode):
        raise ContractError("unsafe_report", f"{context} must be a regular non-symlink file.")
    return item_stat


def _read_at_optional(
    directory_descriptor: int, name: str, maximum: int, context: str
) -> Optional[bytes]:
    before = _stat_at_optional(directory_descriptor, name, context)
    if before is None:
        return None
    if before.st_size > maximum:
        raise ContractError("report_too_large", f"{context} exceeds max_report_bytes.")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory_descriptor)
    except OSError as exc:
        raise ContractError("storage_error", f"{context} cannot be opened safely.") from exc
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not _same_inode(before, opened)
        ):
            raise ContractError("document_conflict", f"{context} changed while opening.")
        chunks: List[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65_536, maximum + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > maximum:
                raise ContractError("report_too_large", f"{context} exceeds max_report_bytes.")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            opened.st_size != after.st_size
            or opened.st_mtime_ns != after.st_mtime_ns
            or opened.st_ctime_ns != after.st_ctime_ns
        ):
            raise ContractError("document_conflict", f"{context} changed while reading.")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _open_read_regular_at(
    directory_descriptor: int,
    name: str,
    maximum: int,
    context: str,
) -> Optional[Tuple[int, bytes, os.stat_result]]:
    """Open and stably read one leaf while keeping its inode pinned."""

    before = _stat_at_optional(directory_descriptor, name, context)
    if before is None:
        return None
    if before.st_size > maximum:
        raise ContractError("report_too_large", f"{context} exceeds max_report_bytes.")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory_descriptor)
    except OSError as exc:
        raise ContractError("storage_error", f"{context} cannot be opened safely.") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or not _same_inode(before, opened):
            raise ContractError("document_conflict", f"{context} changed while opening.")
        chunks: List[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(65_536, maximum + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > maximum:
                raise ContractError("report_too_large", f"{context} exceeds max_report_bytes.")
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            not _same_inode(opened, after)
            or opened.st_size != after.st_size
            or opened.st_mtime_ns != after.st_mtime_ns
            or opened.st_ctime_ns != after.st_ctime_ns
        ):
            raise ContractError("document_conflict", f"{context} changed while reading.")
        return descriptor, b"".join(chunks), after
    except Exception:
        os.close(descriptor)
        raise


def _preimage_matches(actual: Optional[bytes], preimage: Dict[str, str]) -> bool:
    if preimage["state"] == "absent":
        return actual is None
    return actual is not None and hashlib.sha256(actual).hexdigest() == preimage["sha256"]


def _verify_frozen_preimage(
    actual: Optional[bytes], preimage: Dict[str, str], context: str
) -> None:
    if not _preimage_matches(actual, preimage):
        raise ContractError(
            "document_conflict",
            f"{context} does not match the confirmation-time preimage.",
        )


def _rename_noreplace(
    source_directory_descriptor: int,
    source_name: str,
    target_directory_descriptor: int,
    target_name: str,
) -> None:
    _safe_component(source_name, "Rename source")
    _safe_component(target_name, "Rename target")
    try:
        renameatx = ctypes.CDLL(None, use_errno=True).renameatx_np
    except AttributeError as exc:
        raise ContractError(
            "conditional_rename_unavailable",
            "This Mac does not provide renameatx_np conditional rename support.",
        ) from exc
    renameatx.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameatx.restype = ctypes.c_int
    result = renameatx(
        source_directory_descriptor,
        os.fsencode(source_name),
        target_directory_descriptor,
        os.fsencode(target_name),
        RENAME_EXCL,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def _is_conditional_rename_unavailable(error_number: Optional[int]) -> bool:
    unavailable = {
        errno.EINVAL,
        errno.EXDEV,
        getattr(errno, "ENOTSUP", -1),
        getattr(errno, "EOPNOTSUPP", -1),
    }
    return error_number in unavailable


def _write_deterministic_staged_file_at(
    directory_descriptor: int,
    name: str,
    payload: bytes,
    mode: int,
    context: str,
) -> Tuple[int, os.stat_result]:
    _safe_component(name, context)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, mode, dir_fd=directory_descriptor)
    except FileExistsError:
        existing = _open_read_regular_at(
            directory_descriptor, name, len(payload), context
        )
        if existing is None or existing[1] != payload:
            if existing is not None:
                os.close(existing[0])
            raise ContractError("attempt_artifact_conflict", f"{context} staging artifact conflicts.")
        return existing[0], existing[2]
    except OSError as exc:
        raise ContractError("storage_error", f"{context} staging artifact cannot be created.") from exc
    try:
        os.fchmod(descriptor, mode)
        written = 0
        while written < len(payload):
            written += os.write(descriptor, payload[written:])
        os.fsync(descriptor)
        opened = os.fstat(descriptor)
        named = _stat_at_optional(directory_descriptor, name, context)
        if named is None or not _same_inode(opened, named):
            raise ContractError(
                "attempt_artifact_conflict", f"{context} staging artifact changed."
            )
    except Exception:
        os.close(descriptor)
        raise
    os.fsync(directory_descriptor)
    return descriptor, opened


def _unlink_at_if_present(directory_descriptor: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=directory_descriptor)
        os.fsync(directory_descriptor)
    except FileNotFoundError:
        return


def _attempt_artifact_names(request_digest: str, label: str) -> Tuple[str, str]:
    return (
        f".weekly-review-cas-v1-{request_digest}-{label}.staged",
        f".weekly-review-cas-v1-{request_digest}-{label}.backup",
    )


def _restore_claimed_backup(
    directory_descriptor: int, backup_name: str, target_name: str, context: str
) -> None:
    try:
        _rename_noreplace(
            directory_descriptor,
            backup_name,
            directory_descriptor,
            target_name,
        )
        os.fsync(directory_descriptor)
    except OSError as exc:
        if exc.errno != errno.EEXIST:
            raise ContractError(
                "document_conflict_preserved",
                f"{context} conflict is preserved in a deterministic backup.",
            ) from exc


def _conditional_install_at(
    directory_descriptor: int,
    target_name: str,
    target: bytes,
    frozen_preimage: Dict[str, str],
    request_digest: str,
    label: str,
    maximum: int,
    context: str,
    test_mode: bool,
) -> str:
    staged_name, backup_name = _attempt_artifact_names(request_digest, label)
    current_handle = _open_read_regular_at(
        directory_descriptor, target_name, maximum, context
    )
    backup_handle = _open_read_regular_at(
        directory_descriptor, backup_name, maximum, f"{context} backup"
    )
    staged_descriptor: Optional[int] = None
    try:
        current = current_handle[1] if current_handle is not None else None
        backup = backup_handle[1] if backup_handle is not None else None
        if current == target:
            if backup is not None:
                _verify_frozen_preimage(backup, frozen_preimage, f"{context} backup")
            return "already_exact"

        current_stat = current_handle[2] if current_handle is not None else None
        target_mode = stat.S_IMODE(current_stat.st_mode) if current_stat else 0o600
        staged_descriptor, staged_stat = _write_deterministic_staged_file_at(
            directory_descriptor,
            staged_name,
            target,
            target_mode,
            context,
        )
        _maybe_failpoint(f"after_{label}_staged", test_mode)

        if frozen_preimage["state"] == "absent":
            if backup is not None or current is not None:
                raise ContractError(
                    "document_conflict",
                    f"{context} appeared after confirmation; it was not overwritten.",
                )
        else:
            if backup is not None:
                _verify_frozen_preimage(backup, frozen_preimage, f"{context} backup")
                if current is not None:
                    raise ContractError(
                        "document_conflict",
                        f"{context} was created while its confirmed preimage was preserved.",
                    )
            elif current_handle is None:
                raise ContractError(
                    "document_conflict",
                    f"{context} disappeared after confirmation.",
                )
            else:
                _verify_frozen_preimage(current, frozen_preimage, context)
                try:
                    _rename_noreplace(
                        directory_descriptor,
                        target_name,
                        directory_descriptor,
                        backup_name,
                    )
                    os.fsync(directory_descriptor)
                except OSError as exc:
                    if _is_conditional_rename_unavailable(exc.errno):
                        raise ContractError(
                            "conditional_rename_unavailable",
                            f"{context} filesystem does not support safe conditional rename.",
                        ) from exc
                    raise ContractError(
                        "document_conflict",
                        f"{context} could not be claimed conditionally.",
                    ) from exc
                _maybe_failpoint(f"after_{label}_claim", test_mode)
                if backup_handle is not None:
                    os.close(backup_handle[0])
                backup_handle = _open_read_regular_at(
                    directory_descriptor,
                    backup_name,
                    maximum,
                    f"{context} backup",
                )
                if (
                    backup_handle is None
                    or not _same_inode(current_handle[2], backup_handle[2])
                    or not _preimage_matches(backup_handle[1], frozen_preimage)
                ):
                    if backup_handle is not None:
                        _restore_claimed_backup(
                            directory_descriptor, backup_name, target_name, context
                        )
                    raise ContractError(
                        "document_conflict_preserved",
                        f"{context} changed during its conditional claim; content was preserved.",
                    )

        current_after_claim = _read_at_optional(
            directory_descriptor, target_name, maximum, context
        )
        if current_after_claim is not None:
            if current_after_claim == target:
                return "already_exact"
            raise ContractError(
                "document_conflict",
                f"{context} was created concurrently; it was not overwritten.",
            )
        staged_named = _stat_at_optional(directory_descriptor, staged_name, context)
        if staged_named is None or not _same_inode(staged_stat, staged_named):
            raise ContractError(
                "attempt_artifact_conflict", f"{context} staging artifact changed."
            )
        try:
            _rename_noreplace(
                directory_descriptor,
                staged_name,
                directory_descriptor,
                target_name,
            )
            os.fsync(directory_descriptor)
        except OSError as exc:
            if _is_conditional_rename_unavailable(exc.errno):
                raise ContractError(
                    "conditional_rename_unavailable",
                    f"{context} conditional installation is unavailable.",
                ) from exc
            if exc.errno not in {errno.EEXIST, errno.ENOENT}:
                raise ContractError(
                    "storage_error", f"{context} conditional installation failed."
                ) from exc
            current_race = _read_at_optional(
                directory_descriptor, target_name, maximum, context
            )
            if current_race != target:
                raise ContractError(
                    "document_conflict",
                    f"{context} was created concurrently; it was not overwritten.",
                ) from exc
            return "already_exact"
        installed = _open_read_regular_at(
            directory_descriptor, target_name, maximum, context
        )
        if (
            installed is None
            or not _same_inode(staged_stat, installed[2])
            or installed[1] != target
        ):
            if installed is not None:
                os.close(installed[0])
            raise ContractError(
                "write_verification_failed",
                f"{context} installed inode or bytes differ from the staged target.",
            )
        os.close(installed[0])
        return "written"
    finally:
        if staged_descriptor is not None:
            os.close(staged_descriptor)
        if current_handle is not None:
            os.close(current_handle[0])
        if backup_handle is not None:
            os.close(backup_handle[0])


def _cleanup_attempt_artifacts_at(
    directory_descriptor: int,
    request_digest: str,
    label: str,
    frozen_preimage: Dict[str, str],
    target_sha256: str,
    maximum: int,
    context: str,
) -> None:
    staged_name, backup_name = _attempt_artifact_names(request_digest, label)
    staged = _open_read_regular_at(
        directory_descriptor, staged_name, maximum, f"{context} staged artifact"
    )
    if staged is not None:
        try:
            named = _stat_at_optional(
                directory_descriptor, staged_name, f"{context} staged artifact"
            )
            if hashlib.sha256(staged[1]).hexdigest() != target_sha256:
                raise ContractError(
                    "attempt_artifact_conflict",
                    f"{context} staged artifact bytes are not the confirmed target.",
                )
            if named is None or not _same_inode(staged[2], named):
                raise ContractError(
                    "attempt_artifact_conflict", f"{context} staged artifact changed."
                )
            _unlink_at_if_present(directory_descriptor, staged_name)
        finally:
            os.close(staged[0])
    backup = _open_read_regular_at(
        directory_descriptor, backup_name, maximum, f"{context} backup"
    )
    if backup is None:
        return
    try:
        _verify_frozen_preimage(backup[1], frozen_preimage, f"{context} backup")
        named = _stat_at_optional(directory_descriptor, backup_name, f"{context} backup")
        if named is None or not _same_inode(backup[2], named):
            raise ContractError(
                "attempt_artifact_conflict", f"{context} backup changed during cleanup."
            )
        _unlink_at_if_present(directory_descriptor, backup_name)
    finally:
        os.close(backup[0])


def _capture_stage_binding_and_preimages(
    config: Dict[str, Any],
    week_id: str,
    report_preimage: Dict[str, str],
    index_preimage: Dict[str, str],
) -> Tuple[str, str]:
    vault_descriptor, vault_identity = _open_bound_vault(config)
    output_descriptor: Optional[int] = None
    report_descriptor: Optional[int] = None
    try:
        output_parts = PurePosixPath(config["vault"]["output_root"]).parts
        output_descriptor = _open_directory_chain_at(
            vault_descriptor, output_parts, create=False
        )
        if output_descriptor is None:
            _verify_frozen_preimage(None, report_preimage, "Weekly report")
            _verify_frozen_preimage(None, index_preimage, "Weekly review index")
            return vault_identity, "absent"
        output_identity = _identity_digest(os.fstat(output_descriptor))
        index_actual = _read_at_optional(
            output_descriptor,
            "Weekly Reviews.md",
            config["limits"]["max_report_bytes"],
            "Weekly review index",
        )
        report_descriptor = _open_directory_chain_at(
            output_descriptor, [week_id[:4]], create=False
        )
        report_actual = (
            None
            if report_descriptor is None
            else _read_at_optional(
                report_descriptor,
                f"{week_id}.md",
                config["limits"]["max_report_bytes"],
                "Weekly report",
            )
        )
        _verify_frozen_preimage(report_actual, report_preimage, "Weekly report")
        _verify_frozen_preimage(index_actual, index_preimage, "Weekly review index")
        return vault_identity, output_identity
    finally:
        if report_descriptor is not None:
            os.close(report_descriptor)
        if output_descriptor is not None:
            os.close(output_descriptor)
        os.close(vault_descriptor)


def _verify_rebound_documents(
    config: Dict[str, Any],
    pending: Dict[str, Any],
    attempt: Dict[str, Any],
    report_bytes: bytes,
    index_bytes: bytes,
) -> None:
    """Rebind from '/' after writes so path swaps cannot advance the checkpoint."""

    vault_descriptor, _ = _open_bound_vault(config, pending["vault_identity"])
    output_descriptor: Optional[int] = None
    report_descriptor: Optional[int] = None
    try:
        output_descriptor = _open_directory_chain_at(
            vault_descriptor,
            PurePosixPath(config["vault"]["output_root"]).parts,
            create=False,
        )
        if output_descriptor is None or attempt["output_identity"] != _identity_digest(
            os.fstat(output_descriptor)
        ):
            raise ContractError(
                "directory_identity_conflict",
                "The weekly output path no longer resolves to the bound directory.",
            )
        report_descriptor = _open_directory_chain_at(
            output_descriptor, [pending["week_id"][:4]], create=False
        )
        if report_descriptor is None or attempt[
            "report_parent_identity"
        ] != _identity_digest(os.fstat(report_descriptor)):
            raise ContractError(
                "directory_identity_conflict",
                "The report path no longer resolves to the bound directory.",
            )
        maximum = config["limits"]["max_report_bytes"]
        if _read_at_optional(
            report_descriptor,
            f"{pending['week_id']}.md",
            maximum,
            "Weekly report",
        ) != report_bytes or _read_at_optional(
            output_descriptor,
            "Weekly Reviews.md",
            maximum,
            "Weekly review index",
        ) != index_bytes:
            raise ContractError(
                "write_verification_failed",
                "Rebound weekly review documents differ from the confirmed targets.",
            )
    finally:
        if report_descriptor is not None:
            os.close(report_descriptor)
        if output_descriptor is not None:
            os.close(output_descriptor)
        os.close(vault_descriptor)


def _frontmatter_scalars(text: str) -> Dict[str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ContractError("invalid_report", "Weekly report must start with YAML frontmatter.")
    closing: Optional[int] = None
    for index in range(1, min(len(lines), 512)):
        if lines[index].strip() == "---":
            closing = index
            break
    if closing is None:
        raise ContractError("invalid_report", "Weekly report frontmatter is not closed.")
    output: Dict[str, str] = {}
    for line in lines[1:closing]:
        if not line or line[0].isspace() or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        if key in output:
            raise ContractError("invalid_report", "Weekly report frontmatter has duplicate keys.")
        scalar = value.strip()
        if len(scalar) >= 2 and scalar[0] == scalar[-1] and scalar[0] in {"'", '"'}:
            scalar = scalar[1:-1]
        output[key] = scalar
    return output


def _canonical_review_targets(
    config: Dict[str, Any], week_id: str
) -> Tuple[str, str]:
    report_relative = _expected_report_relative(config, week_id)
    index_relative = PurePosixPath(
        config["vault"]["output_root"], "Weekly Reviews.md"
    ).as_posix()
    return report_relative, index_relative


def _validate_report_target_text(
    config: Dict[str, Any],
    week_id: str,
    window: Dict[str, str],
    text: Any,
) -> Tuple[str, bytes]:
    if not isinstance(text, str) or "\x00" in text:
        raise ContractError(
            "invalid_report", "report_text must be exact NUL-free UTF-8 Markdown."
        )
    encoded = text.encode("utf-8")
    if len(encoded) > config["limits"]["max_report_bytes"]:
        raise ContractError("report_too_large", "report_text exceeds max_report_bytes.")
    scalars = _frontmatter_scalars(text)
    if scalars.get("schema_version") != "1" or scalars.get("type") != "weekly-review":
        raise ContractError("invalid_report", "Weekly report frontmatter type/schema is invalid.")
    if scalars.get("week_id") != week_id:
        raise ContractError("invalid_report", "Weekly report frontmatter week_id does not match.")
    if scalars.get("status") != "confirmed":
        raise ContractError("report_not_confirmed", "Weekly report status must be confirmed.")
    start = _parse_aware_timestamp(window["start"], "window.start")
    end = _parse_aware_timestamp(window["end_exclusive"], "window.end_exclusive")
    if scalars.get("period_start") != start.date().isoformat():
        raise ContractError(
            "invalid_report", "Weekly report period_start does not match staged window."
        )
    if scalars.get("period_end_exclusive") != end.date().isoformat():
        raise ContractError(
            "invalid_report",
            "Weekly report period_end_exclusive does not match staged window.",
        )
    if scalars.get("timezone") != window["timezone"]:
        raise ContractError(
            "invalid_report", "Weekly report timezone does not match staged window."
        )
    if scalars.get("collected_through") != window["collected_through"]:
        raise ContractError(
            "invalid_report",
            "Weekly report collected_through must equal the staged collection cutoff.",
        )
    generated_start = "<!-- weekly-review:generated:start -->"
    generated_end = "<!-- weekly-review:generated:end -->"
    if text.count(generated_start) != 1 or text.count(generated_end) != 1:
        raise ContractError(
            "invalid_report", "Weekly report generated block must have one marker pair."
        )
    if text.index(generated_start) + len(generated_start) > text.index(generated_end):
        raise ContractError(
            "invalid_report", "Weekly report generated block markers are reversed."
        )
    return hashlib.sha256(encoded).hexdigest(), encoded


def _validate_index_target_text(
    config: Dict[str, Any], week_id: str, report_relative: str, text: Any
) -> Tuple[str, bytes]:
    if not isinstance(text, str) or "\x00" in text:
        raise ContractError(
            "invalid_index", "index_text must be exact NUL-free UTF-8 Markdown."
        )
    encoded = text.encode("utf-8")
    if len(encoded) > config["limits"]["max_report_bytes"]:
        raise ContractError("report_too_large", "index_text exceeds max_report_bytes.")
    start_marker = "<!-- weekly-review:index:start -->"
    end_marker = "<!-- weekly-review:index:end -->"
    if text.count(start_marker) != 1 or text.count(end_marker) != 1:
        raise ContractError("invalid_index", "Weekly review index managed block is invalid.")
    start = text.index(start_marker) + len(start_marker)
    end = text.index(end_marker)
    if start > end:
        raise ContractError("invalid_index", "Weekly review index markers are reversed.")
    target = report_relative[:-3] if report_relative.endswith(".md") else report_relative
    link = f"[[{target}|{week_id}]]"
    managed = text[start:end]
    matching_lines = [line for line in managed.splitlines() if link in line]
    confirmed_line = re.compile(
        rf"^- \[\[{re.escape(target)}\|{re.escape(week_id)}\]\] · confirmed · [^\r\n·]+$"
    )
    if (
        text.count(link) != 1
        or len(matching_lines) != 1
        or confirmed_line.fullmatch(matching_lines[0]) is None
    ):
        raise ContractError(
            "invalid_index",
            "Weekly review index must contain exactly one confirmed link to the report.",
        )
    return hashlib.sha256(encoded).hexdigest(), encoded


def _expected_report_relative(config: Dict[str, Any], week_id: str) -> str:
    year = week_id[:4]
    return PurePosixPath(
        config["vault"]["output_root"], year, f"{week_id}.md"
    ).as_posix()


def _verify_unique_week_report_at(
    output_descriptor: int,
    expected_relative: Tuple[str, ...],
    week_id: str,
    maximum: int,
) -> None:
    matches: List[Tuple[str, ...]] = []
    visited = 0

    def walk(directory_descriptor: int, prefix: Tuple[str, ...]) -> None:
        nonlocal visited
        try:
            names = os.listdir(directory_descriptor)
        except OSError as exc:
            raise ContractError("storage_error", "Weekly output cannot be enumerated safely.") from exc
        for name in names:
            item_stat = os.stat(
                name, dir_fd=directory_descriptor, follow_symlinks=False
            )
            if stat.S_ISLNK(item_stat.st_mode):
                raise ContractError("unsafe_report", "Weekly output contains a symlink.")
            relative = prefix + (name,)
            if stat.S_ISDIR(item_stat.st_mode):
                child = _open_directory_chain_at(directory_descriptor, [name], create=False)
                assert child is not None
                try:
                    walk(child, relative)
                finally:
                    os.close(child)
            elif stat.S_ISREG(item_stat.st_mode) and name.lower().endswith(".md"):
                visited += 1
                if visited > MAX_REPORT_FILES:
                    raise ContractError(
                        "report_limit", "Weekly output has too many Markdown files to verify."
                    )
                raw = _read_at_optional(
                    directory_descriptor, name, maximum, "Weekly output file"
                )
                assert raw is not None
                try:
                    scalars = _frontmatter_scalars(raw.decode("utf-8"))
                except UnicodeDecodeError:
                    continue
                except ContractError as exc:
                    if exc.code == "invalid_report":
                        continue
                    raise
                if scalars.get("type") == "weekly-review" and scalars.get("week_id") == week_id:
                    matches.append(relative)

    walk(output_descriptor, ())
    if matches != [expected_relative]:
        raise ContractError(
            "duplicate_report",
            "Exactly one canonical weekly report must exist for the checkpoint week.",
            {"match_count": len(matches)},
        )


def _private_snapshot_size(path: Path, maximum: int) -> int:
    try:
        item_stat = os.lstat(path)
    except FileNotFoundError as exc:
        raise ContractError(
            "corrupt_state", "A snapshot referenced by the current baseline is missing."
        ) from exc
    if stat.S_ISLNK(item_stat.st_mode):
        raise ContractError("unsafe_storage", "Snapshot file must not be a symlink.")
    _ensure_secure_file_stat(item_stat, "Snapshot file")
    if item_stat.st_size > maximum:
        raise ContractError(
            "corrupt_state", "A stored snapshot exceeds snapshot_max_file_bytes."
        )
    return item_stat.st_size


def _prepare_pending_observations(
    root: Path,
    state: Dict[str, Any],
    observations: List[Dict[str, Any]],
    config: Dict[str, Any],
) -> Tuple[Dict[str, Dict[str, str]], Dict[str, bytes]]:
    checkpoint = state.get("checkpoint")
    committed = checkpoint["baseline"] if checkpoint else {}
    staged: Dict[str, Dict[str, str]] = {}
    payloads: Dict[str, bytes] = {}
    for item in observations:
        previous = committed.get(item["source_digest"])
        entry = {"content_sha256": item["content_sha256"]}
        if "text" in item:
            name = f"{item['source_digest']}--{item['content_sha256']}.txt"
            entry["snapshot_name"] = name
            payloads[name] = item["text"].encode("utf-8")
        elif (
            previous is not None
            and previous["content_sha256"] == item["content_sha256"]
            and "snapshot_name" in previous
        ):
            entry["snapshot_name"] = previous["snapshot_name"]
        staged[item["source_digest"]] = entry

    prospective = {digest: dict(entry) for digest, entry in committed.items()}
    prospective.update(staged)
    if len(prospective) > config["limits"]["max_baseline_entries"]:
        raise ContractError(
            "baseline_limit",
            "Staged merge exceeds max_baseline_entries; no entries were discarded.",
            {
                "baseline_count": len(prospective),
                "max_baseline_entries": config["limits"]["max_baseline_entries"],
            },
        )

    committed_refs = {
        entry["snapshot_name"]
        for entry in committed.values()
        if "snapshot_name" in entry
    }
    staged_refs = {
        entry["snapshot_name"]
        for entry in staged.values()
        if "snapshot_name" in entry
    }
    total_bytes = 0
    for name in committed_refs.union(staged_refs):
        if name in payloads:
            total_bytes += len(payloads[name])
        else:
            total_bytes += _private_snapshot_size(
                root / SNAPSHOTS_NAME / name,
                config["limits"]["snapshot_max_file_bytes"],
            )
    if total_bytes > config["limits"]["snapshot_max_total_bytes"]:
        raise ContractError(
            "snapshot_limit",
            "Committed plus staged snapshots exceed snapshot_max_total_bytes.",
            {
                "snapshot_bytes": total_bytes,
                "snapshot_max_total_bytes": config["limits"]["snapshot_max_total_bytes"],
            },
        )
    return staged, payloads


def _write_pending_snapshot_payloads(
    root: Path, payloads: Dict[str, bytes], test_mode: bool
) -> None:
    if not payloads:
        return
    snapshots = _ensure_snapshots_directory(root)
    for name, payload in payloads.items():
        path = snapshots / name
        if path.exists() or path.is_symlink():
            existing = _read_private_bytes(path, len(payload), "Snapshot file")
            if existing != payload:
                raise ContractError(
                    "corrupt_state", "Content-addressed snapshot is inconsistent."
                )
        else:
            _atomic_write_bytes(
                path, payload, test_mode, "before_snapshot_replace"
            )


def _self_test(request: Dict[str, Any], operation: str, request_id: str) -> Dict[str, Any]:
    _ensure_keys(request, {"operation", "protocol_version", "request_id"}, "request")
    probe = hashlib.sha256(b"weekly-review-state-self-test").hexdigest()
    if len(probe) != 64 or not SHA256_RE.fullmatch(probe):
        raise ContractError("self_test_failed", "SHA-256 capability check failed.")
    output = _base_output(True, operation, request_id)
    output.update(
        {
            "capabilities": {
                "atomic_replace": True,
                "baseline_comparison": "sha256_only",
                "config_schema_version": SCHEMA_VERSION,
                "document_cas": "darwin_renameatx_np_rename_excl",
                "durable_promotion_receipts": True,
                "frozen_stage_preimages": True,
                "optimistic_revision": True,
                "snapshot_text": "explicit_file_root_opt_in_only",
                "maintenance": [
                    "maintenance.status",
                    "snapshots.purge",
                    "baseline.reset",
                ],
                "review_transaction": [
                    "review.stage",
                    "review.write-promote",
                    "review.abort",
                ],
                "write_ahead_phase": "staged_then_writing",
            },
            "production_state_accessed": False,
        }
    )
    return output


def _config_validate(request: Dict[str, Any], operation: str, request_id: str) -> Dict[str, Any]:
    _ensure_keys(
        request, {"operation", "protocol_version", "request_id", "config"}, "request"
    )
    _require_keys(request, {"config"}, "request")
    config = _validate_config(request["config"])
    output = _base_output(True, operation, request_id)
    output.update({"config": config, "valid": True})
    return output


def _config_get(request: Dict[str, Any], operation: str, request_id: str) -> Dict[str, Any]:
    _ensure_keys(request, {"operation", "protocol_version", "request_id"}, "request")
    with _locked_storage() as (root, test_mode):
        del test_mode
        _cleanup_atomic_temps(root)
        revision, config = _read_config(root)
    output = _base_output(True, operation, request_id)
    output.update({"configured": config is not None, "revision": revision})
    if config is not None:
        output["config"] = config
        path_diagnostics = _config_live_path_diagnostics(config)
        output["live_paths_valid"] = all(
            item["status"] == "available" for item in path_diagnostics
        )
        output["path_diagnostics"] = path_diagnostics
    return output


def _config_set(request: Dict[str, Any], operation: str, request_id: str) -> Dict[str, Any]:
    allowed = {
        "operation",
        "protocol_version",
        "request_id",
        "config",
        "confirmed",
        "expected_revision",
    }
    _ensure_keys(request, allowed, "request")
    _require_keys(request, {"config", "confirmed", "expected_revision"}, "request")
    if _require_bool(request["confirmed"], "confirmed") is not True:
        raise ContractError("confirmation_required", "config.set requires confirmed=true.")
    expected = _require_int(request["expected_revision"], "expected_revision", 0, 2**63 - 1)
    config = _validate_config(request["config"])
    with _locked_storage() as (root, test_mode):
        _cleanup_atomic_temps(root)
        current_revision, current_config = _read_config(root)
        if current_revision != expected:
            raise ContractError(
                "revision_conflict",
                "Config revision changed; read and reconfirm before retrying.",
                {"actual_revision": current_revision, "expected_revision": expected},
            )
        state = _read_state(root)
        _require_no_pending_review(state, operation)
        _cleanup_snapshots(root, _snapshot_references(state))
        opt_out_count = _snapshot_opt_out_count(current_config, config)
        snapshot_count, snapshot_bytes, snapshot_names = _snapshot_inventory(root, state)
        checkpoint = state.get("checkpoint")
        baseline_count = len(checkpoint["baseline"]) if checkpoint else 0
        if config["limits"]["max_baseline_entries"] < baseline_count:
            raise ContractError(
                "baseline_reset_required",
                "The proposed baseline cap is below retained entries; preview and confirm baseline.reset first.",
                {
                    "proposed_max_baseline_entries": config["limits"]["max_baseline_entries"],
                    "retained_baseline_entries": baseline_count,
                    "state_revision": state["revision"],
                },
            )
        largest_snapshot = max(
            (
                _private_snapshot_size(
                    root / SNAPSHOTS_NAME / name,
                    2 * 1024 * 1024,
                )
                for name in snapshot_names
            ),
            default=0,
        )
        if (
            config["limits"]["snapshot_max_total_bytes"] < snapshot_bytes
            or config["limits"]["snapshot_max_file_bytes"] < largest_snapshot
        ):
            raise ContractError(
                "snapshot_purge_required",
                "The proposed snapshot caps are below retained inventory; preview and confirm snapshots.purge first.",
                {
                    "largest_retained_snapshot_bytes": largest_snapshot,
                    "proposed_max_file_bytes": config["limits"]["snapshot_max_file_bytes"],
                    "proposed_max_total_bytes": config["limits"]["snapshot_max_total_bytes"],
                    "retained_snapshot_bytes": snapshot_bytes,
                    "snapshot_count": snapshot_count,
                    "state_revision": state["revision"],
                },
            )
        if opt_out_count and snapshot_count:
            raise ContractError(
                "snapshot_purge_required",
                "Disabling snapshot_text requires an explicit full snapshot preview and purge first.",
                {
                    "disabled_root_count": opt_out_count,
                    "snapshot_count": snapshot_count,
                    "state_revision": state["revision"],
                },
            )
        # Revalidate under the lock in case a selected path changed meanwhile.
        config = _validate_config(config)
        new_revision = current_revision + 1
        document = {
            "config": config,
            "revision": new_revision,
            "schema_version": SCHEMA_VERSION,
            "updated_at": _utc_now(),
        }
        _atomic_write_json(
            root / CONFIG_NAME,
            document,
            test_mode,
            "before_config_replace",
            _write_size_limit(
                MAX_CONFIG_BYTES, TEST_MAX_CONFIG_BYTES_ENV, test_mode
            ),
        )
    output = _base_output(True, operation, request_id)
    output.update({"confirmed": True, "revision": new_revision})
    return output


def _review_stage(
    request: Dict[str, Any], operation: str, request_id: str
) -> Dict[str, Any]:
    allowed = {
        "operation",
        "protocol_version",
        "request_id",
        "confirmed",
        "expected_config_revision",
        "expected_state_revision",
        "review_id",
        "week_id",
        "window",
        "coverage",
        "preview_sha256",
        "report_sha256",
        "index_sha256",
        "report_text",
        "index_text",
        "report_preimage",
        "index_preimage",
        "observations",
    }
    _ensure_keys(request, allowed, "request")
    _require_keys(request, allowed.difference({"operation", "protocol_version", "request_id"}), "request")
    if _require_bool(request["confirmed"], "confirmed") is not True:
        raise ContractError(
            "confirmation_required", "review.stage requires confirmed=true."
        )
    expected_config = _require_int(
        request["expected_config_revision"],
        "expected_config_revision",
        1,
        2**63 - 1,
    )
    expected_state = _require_int(
        request["expected_state_revision"],
        "expected_state_revision",
        0,
        2**63 - 1,
    )
    review_id = _validate_review_id(request["review_id"])
    week_id = _validate_week_id(request["week_id"])
    declared_preview = _require_sha256(
        request["preview_sha256"], "preview_sha256"
    )
    declared_report = _require_sha256(request["report_sha256"], "report_sha256")
    declared_index = _require_sha256(request["index_sha256"], "index_sha256")
    report_preimage = _parse_expected_preimage(
        request["report_preimage"], "report_preimage"
    )
    index_preimage = _parse_expected_preimage(
        request["index_preimage"], "index_preimage"
    )

    with _locked_storage() as (root, test_mode):
        _cleanup_atomic_temps(root)
        config_revision, config = _read_config(root)
        if config is None:
            raise ContractError("not_configured", "weekly-review config is not set.")
        _validate_vault_path_live(config["vault"]["path"])
        state = _read_state(root)
        _require_no_pending_review(state, operation)
        if config_revision != expected_config or state["revision"] != expected_state:
            raise ContractError(
                "revision_conflict",
                "Config or state changed after preview confirmation.",
                {
                    "actual_config_revision": config_revision,
                    "actual_state_revision": state["revision"],
                    "expected_config_revision": expected_config,
                    "expected_state_revision": expected_state,
                },
            )
        compatibility = _require_baseline_compatible(state, config, config_revision)
        checkpoint = state.get("checkpoint")
        if review_id in state["receipts"] or (
            checkpoint is not None and checkpoint["review_id"] == review_id
        ):
            raise ContractError(
                "review_id_conflict",
                "review_id is globally non-reusable after promotion.",
            )
        if len(state["receipts"]) >= MAX_RECEIPTS:
            raise ContractError(
                "receipt_limit",
                "Durable promotion receipts reached their non-discarding limit.",
            )
        if state["checkpoint"] and week_id < state["checkpoint"]["week_id"]:
            raise ContractError(
                "checkpoint_regression", "A staged review cannot move to an earlier ISO week."
            )
        window = _validate_review_window(request["window"], week_id, config["timezone"])
        coverage = _validate_coverage(request["coverage"])
        report_relative, index_relative = _canonical_review_targets(config, week_id)
        report_hash, _ = _validate_report_target_text(
            config, week_id, window, request["report_text"]
        )
        index_hash, _ = _validate_index_target_text(
            config, week_id, report_relative, request["index_text"]
        )
        if report_hash != declared_report or report_hash != declared_preview:
            raise ContractError(
                "hash_mismatch",
                "Confirmed preview/report hashes must equal exact report_text bytes.",
            )
        if index_hash != declared_index:
            raise ContractError(
                "hash_mismatch", "index_sha256 must equal exact index_text bytes."
            )
        vault_identity, output_identity = _capture_stage_binding_and_preimages(
            config,
            week_id,
            report_preimage,
            index_preimage,
        )
        observations = _parse_observations(request["observations"], config)
        _cleanup_snapshots(root, _snapshot_references(state))
        staged_entries, snapshot_payloads = _prepare_pending_observations(
            root, state, observations, config
        )
        staged_at = _utc_now()
        pending = {
            "attempt": None,
            "config_revision": config_revision,
            "coverage": coverage,
            "index_sha256": index_hash,
            "index_preimage": index_preimage,
            "observations": staged_entries,
            "output_identity": output_identity,
            "phase": "staged",
            "preview_sha256": declared_preview,
            "report_sha256": report_hash,
            "report_preimage": report_preimage,
            "review_id": review_id,
            "scope_fingerprint": _baseline_scope_fingerprint(config),
            "staged_at": staged_at,
            "vault_identity": vault_identity,
            "week_id": week_id,
            "window": window,
        }
        new_revision = state["revision"] + 1
        new_state = {
            "checkpoint": state.get("checkpoint"),
            "pending_review": pending,
            "receipts": state["receipts"],
            "revision": new_revision,
            "schema_version": SCHEMA_VERSION,
            "updated_at": staged_at,
        }
        encoded_state = _encode_json_document(
            new_state,
            _write_size_limit(MAX_STATE_BYTES, TEST_MAX_STATE_BYTES_ENV, test_mode),
            STATE_NAME,
        )
        _write_pending_snapshot_payloads(root, snapshot_payloads, test_mode)
        _atomic_write_bytes(
            root / STATE_NAME,
            encoded_state,
            test_mode,
            "before_stage_state_replace",
        )
        _cleanup_snapshots(root, _snapshot_references(new_state))
    output = _base_output(True, operation, request_id)
    output.update(
        {
            "baseline_compatibility": compatibility,
            "config_revision": config_revision,
            "index_relative_path": index_relative,
            "pending": True,
            "report_relative_path": report_relative,
            "review_id": review_id,
            "staged_at": staged_at,
            "state_revision": new_revision,
            "week_id": week_id,
        }
    )
    return output


def _parse_document_write(
    raw: Any, field: str, expected_relative: Optional[str] = None
) -> Tuple[str, str]:
    document = _require_object(raw, field)
    keys = {"relative_path", "target_text"}
    _ensure_keys(document, keys, field)
    _require_keys(document, keys, field)
    relative = _validate_relative_path(
        document["relative_path"], f"{field}.relative_path", 1024
    )
    if expected_relative is not None and relative != expected_relative:
        raise ContractError(
            "invalid_report_path",
            f"{field}.relative_path must be the canonical weekly-review target.",
            {"expected_relative_path": expected_relative},
        )
    text = document["target_text"]
    if not isinstance(text, str) or "\x00" in text:
        raise ContractError(
            "validation_error", f"{field}.target_text must be exact NUL-free UTF-8 text."
        )
    return relative, text


def _promotion_request_digest(
    review_id: str,
    report_relative: str,
    report_text: str,
    index_relative: str,
    index_text: str,
) -> str:
    canonical = json.dumps(
        {
            "index_relative_sha256": hashlib.sha256(
                index_relative.encode("utf-8")
            ).hexdigest(),
            "index_sha256": hashlib.sha256(index_text.encode("utf-8")).hexdigest(),
            "report_relative_sha256": hashlib.sha256(
                report_relative.encode("utf-8")
            ).hexdigest(),
            "report_sha256": hashlib.sha256(report_text.encode("utf-8")).hexdigest(),
            "review_id": review_id,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _build_promoted_state(
    receipts: Dict[str, Dict[str, Any]],
    pending: Dict[str, Any],
    baseline: Dict[str, Dict[str, Any]],
    review_id: str,
    request_digest: str,
    index_hash: str,
    report_hash: str,
    promoted_at: str,
    final_revision: int,
) -> Dict[str, Any]:
    final_receipts = dict(receipts)
    if len(final_receipts) >= MAX_RECEIPTS:
        raise ContractError(
            "receipt_limit",
            "Durable promotion receipts reached their non-discarding limit.",
        )
    final_receipts[review_id] = {
        "config_revision": pending["config_revision"],
        "index_sha256": index_hash,
        "promoted_at": promoted_at,
        "report_sha256": report_hash,
        "request_digest": request_digest,
        "scope_fingerprint": pending["scope_fingerprint"],
        "state_revision": final_revision,
        "week_id": pending["week_id"],
    }
    return {
        "checkpoint": {
            "baseline": baseline,
            "config_revision": pending["config_revision"],
            "index_sha256": index_hash,
            "report_sha256": report_hash,
            "review_id": review_id,
            "scope_fingerprint": pending["scope_fingerprint"],
            "week_id": pending["week_id"],
        },
        "pending_review": None,
        "receipts": final_receipts,
        "revision": final_revision,
        "schema_version": SCHEMA_VERSION,
        "updated_at": promoted_at,
    }


def _review_abort(
    request: Dict[str, Any], operation: str, request_id: str
) -> Dict[str, Any]:
    allowed = {
        "operation",
        "protocol_version",
        "request_id",
        "confirmed",
        "expected_config_revision",
        "expected_state_revision",
        "review_id",
    }
    _ensure_keys(request, allowed, "request")
    _require_keys(
        request,
        {"confirmed", "expected_config_revision", "expected_state_revision", "review_id"},
        "request",
    )
    if _require_bool(request["confirmed"], "confirmed") is not True:
        raise ContractError(
            "confirmation_required", "review.abort requires confirmed=true."
        )
    expected_config = _require_int(
        request["expected_config_revision"],
        "expected_config_revision",
        1,
        2**63 - 1,
    )
    expected_state = _require_int(
        request["expected_state_revision"],
        "expected_state_revision",
        1,
        2**63 - 1,
    )
    review_id = _validate_review_id(request["review_id"])

    with _locked_storage() as (root, test_mode):
        _cleanup_atomic_temps(root)
        config_revision, config = _read_config(root)
        if config is None:
            raise ContractError("not_configured", "weekly-review config is not set.")
        state = _read_state(root)
        pending = state.get("pending_review")
        if pending is None:
            raise ContractError("no_pending_review", "There is no pending review to abort.")
        if pending["review_id"] != review_id:
            raise ContractError(
                "pending_review_mismatch",
                "review_id does not identify the active durable lease.",
            )
        if pending["phase"] == "writing":
            raise ContractError(
                "write_recovery_required",
                "A document-write attempt is durable; it must be recovered and cannot be aborted.",
            )
        if config_revision != expected_config or state["revision"] != expected_state:
            raise ContractError(
                "revision_conflict",
                "Config or state changed after abort confirmation.",
                {
                    "actual_config_revision": config_revision,
                    "actual_state_revision": state["revision"],
                    "expected_config_revision": expected_config,
                    "expected_state_revision": expected_state,
                },
            )
        if (
            pending["config_revision"] != config_revision
            or pending["scope_fingerprint"] != _baseline_scope_fingerprint(config)
        ):
            raise ContractError(
                "pending_config_incompatible",
                "The active lease no longer matches the confirmed config.",
            )

        aborted_at = _utc_now()
        new_revision = state["revision"] + 1
        new_state = {
            "checkpoint": state.get("checkpoint"),
            "pending_review": None,
            "receipts": state["receipts"],
            "revision": new_revision,
            "schema_version": SCHEMA_VERSION,
            "updated_at": aborted_at,
        }
        encoded_state = _encode_json_document(
            new_state,
            _write_size_limit(MAX_STATE_BYTES, TEST_MAX_STATE_BYTES_ENV, test_mode),
            STATE_NAME,
        )
        _atomic_write_bytes(
            root / STATE_NAME,
            encoded_state,
            test_mode,
            "before_abort_state_replace",
        )
        _cleanup_snapshots(root, _snapshot_references(new_state))
    output = _base_output(True, operation, request_id)
    output.update(
        {
            "aborted": True,
            "aborted_at": aborted_at,
            "config_revision": config_revision,
            "review_id": review_id,
            "state_revision": new_revision,
        }
    )
    return output


def _review_write_promote(
    request: Dict[str, Any], operation: str, request_id: str
) -> Dict[str, Any]:
    allowed = {
        "operation",
        "protocol_version",
        "request_id",
        "confirmed",
        "expected_config_revision",
        "expected_state_revision",
        "review_id",
        "report",
        "index",
    }
    _ensure_keys(request, allowed, "request")
    _require_keys(
        request,
        {
            "confirmed",
            "expected_config_revision",
            "expected_state_revision",
            "review_id",
            "report",
            "index",
        },
        "request",
    )
    if _require_bool(request["confirmed"], "confirmed") is not True:
        raise ContractError(
            "confirmation_required", "review.write-promote requires confirmed=true."
        )
    expected_config = _require_int(
        request["expected_config_revision"],
        "expected_config_revision",
        1,
        2**63 - 1,
    )
    expected_state = _require_int(
        request["expected_state_revision"],
        "expected_state_revision",
        1,
        2**63 - 1,
    )
    review_id = _validate_review_id(request["review_id"])
    request_report_relative, request_report_text = _parse_document_write(
        request["report"], "report"
    )
    request_index_relative, request_index_text = _parse_document_write(
        request["index"], "index"
    )
    request_digest = _promotion_request_digest(
        review_id,
        request_report_relative,
        request_report_text,
        request_index_relative,
        request_index_text,
    )

    with _locked_storage() as (root, test_mode):
        _cleanup_atomic_temps(root)
        config_revision, config = _read_config(root)
        if config is None:
            raise ContractError("not_configured", "weekly-review config is not set.")
        state = _read_state(root)
        receipt = state["receipts"].get(review_id)
        if receipt is not None:
            if receipt["request_digest"] != request_digest:
                raise ContractError(
                    "review_id_conflict",
                    "review_id already has a different immutable promotion receipt.",
                )
            output = _base_output(True, operation, request_id)
            output.update(
                {
                    "already_promoted": True,
                    "config_revision": config_revision,
                    "index_disposition": "receipt_confirmed",
                    "promoted_at": receipt["promoted_at"],
                    "report_disposition": "receipt_confirmed",
                    "request_digest": request_digest,
                    "review_id": review_id,
                    "state_revision": state["revision"],
                    "week_id": receipt["week_id"],
                }
            )
            return output

        _validate_vault_path_live(config["vault"]["path"])
        pending = state.get("pending_review")
        checkpoint = state.get("checkpoint")
        if pending is None:
            raise ContractError(
                "no_pending_review", "There is no matching pending review to promote."
            )
        if pending["review_id"] != review_id:
            raise ContractError(
                "pending_review_mismatch",
                "review_id does not identify the active durable lease.",
            )
        report_relative, index_relative = _canonical_review_targets(
            config, pending["week_id"]
        )
        _, report_text = _parse_document_write(
            request["report"], "report", report_relative
        )
        _, index_text = _parse_document_write(
            request["index"], "index", index_relative
        )
        report_hash, report_bytes = _validate_report_target_text(
            config, pending["week_id"], pending["window"], report_text
        )
        index_hash, index_bytes = _validate_index_target_text(
            config, pending["week_id"], report_relative, index_text
        )
        if (
            report_hash != pending["report_sha256"]
            or report_hash != pending["preview_sha256"]
            or index_hash != pending["index_sha256"]
        ):
            raise ContractError(
                "hash_mismatch",
                "Document target bytes do not match the durable confirmed preview.",
            )

        current_fingerprint = _baseline_scope_fingerprint(config)
        if (
            pending["config_revision"] != config_revision
            or pending["scope_fingerprint"] != current_fingerprint
        ):
            raise ContractError(
                "pending_config_incompatible",
                "The durable lease no longer matches the confirmed config.",
            )
        previous_baseline = checkpoint["baseline"] if checkpoint else {}
        baseline = {digest: dict(entry) for digest, entry in previous_baseline.items()}
        baseline.update(
            {digest: dict(entry) for digest, entry in pending["observations"].items()}
        )
        if len(baseline) > config["limits"]["max_baseline_entries"]:
            raise ContractError(
                "baseline_limit",
                "Staged merge exceeds max_baseline_entries; no entries were discarded.",
            )
        state_size_limit = _write_size_limit(
            MAX_STATE_BYTES, TEST_MAX_STATE_BYTES_ENV, test_mode
        )
        if pending["phase"] == "staged":
            if config_revision != expected_config or state["revision"] != expected_state:
                raise ContractError(
                    "revision_conflict",
                    "Config or state changed after document-write confirmation.",
                    {
                        "actual_config_revision": config_revision,
                        "actual_state_revision": state["revision"],
                        "expected_config_revision": expected_config,
                        "expected_state_revision": expected_state,
                    },
                )
            started_at = _utc_now()
            attempt = {
                "output_identity": None,
                "report_parent_identity": None,
                "request_digest": request_digest,
                "started_at": started_at,
                "starting_state_revision": state["revision"],
            }
            pending = dict(pending)
            pending["attempt"] = attempt
            pending["phase"] = "writing"
            wal_state = {
                "checkpoint": checkpoint,
                "pending_review": pending,
                "receipts": state["receipts"],
                "revision": state["revision"] + 1,
                "schema_version": SCHEMA_VERSION,
                "updated_at": started_at,
            }
            encoded_wal = _encode_json_document(
                wal_state,
                state_size_limit,
                STATE_NAME,
            )

            # Promotion cannot become abort-forbidden until every later durable
            # state shape is known to fit. Directory identities are SHA-256
            # digests, so fixed-width placeholders exactly cover their encoded
            # size before any Vault directory is opened or created.
            preflight_attempt = dict(attempt)
            preflight_attempt["output_identity"] = "0" * 64
            preflight_attempt["report_parent_identity"] = "0" * 64
            preflight_pending = dict(pending)
            preflight_pending["attempt"] = preflight_attempt
            preflight_bound_state = {
                "checkpoint": checkpoint,
                "pending_review": preflight_pending,
                "receipts": wal_state["receipts"],
                "revision": wal_state["revision"] + 1,
                "schema_version": SCHEMA_VERSION,
                "updated_at": started_at,
            }
            _encode_json_document(
                preflight_bound_state,
                state_size_limit,
                STATE_NAME,
            )
            preflight_final_revision = preflight_bound_state["revision"] + 1
            preflight_final_state = _build_promoted_state(
                wal_state["receipts"],
                preflight_pending,
                baseline,
                review_id,
                request_digest,
                index_hash,
                report_hash,
                started_at,
                preflight_final_revision,
            )
            _encode_json_document(
                preflight_final_state,
                state_size_limit,
                STATE_NAME,
            )
            _atomic_write_bytes(
                root / STATE_NAME,
                encoded_wal,
                test_mode,
                "before_write_ahead_state_replace",
            )
            state = wal_state
            _maybe_failpoint("after_write_ahead", test_mode)
        else:
            attempt = pending["attempt"]
            assert attempt is not None
            if (
                attempt["request_digest"] != request_digest
                or attempt["starting_state_revision"] != expected_state
                or pending["config_revision"] != expected_config
            ):
                raise ContractError(
                    "write_attempt_mismatch",
                    "The retry does not match the durable document-write attempt.",
                )

        attempt = pending["attempt"]
        assert attempt is not None

        vault_descriptor: Optional[int] = None
        output_descriptor: Optional[int] = None
        report_parent_descriptor: Optional[int] = None
        try:
            vault_descriptor, _ = _open_bound_vault(
                config, pending["vault_identity"]
            )
            output_descriptor = _open_directory_chain_at(
                vault_descriptor,
                PurePosixPath(config["vault"]["output_root"]).parts,
                create=True,
            )
            assert output_descriptor is not None
            output_identity = _identity_digest(os.fstat(output_descriptor))
            if pending["output_identity"] != "absent" and (
                output_identity != pending["output_identity"]
            ):
                raise ContractError(
                    "directory_identity_conflict",
                    "The weekly output directory changed after staging.",
                )
            if (
                attempt["output_identity"] is not None
                and attempt["output_identity"] != output_identity
            ):
                raise ContractError(
                    "directory_identity_conflict",
                    "The bound weekly output directory changed during recovery.",
                )
            report_parent_descriptor = _open_directory_chain_at(
                output_descriptor, [pending["week_id"][:4]], create=True
            )
            assert report_parent_descriptor is not None
            report_parent_identity = _identity_digest(
                os.fstat(report_parent_descriptor)
            )
            if (
                attempt["report_parent_identity"] is not None
                and attempt["report_parent_identity"] != report_parent_identity
            ):
                raise ContractError(
                    "directory_identity_conflict",
                    "The bound report directory changed during recovery.",
                )

            if (
                attempt["output_identity"] is None
                or attempt["report_parent_identity"] is None
            ):
                attempt = dict(attempt)
                attempt["output_identity"] = output_identity
                attempt["report_parent_identity"] = report_parent_identity
                pending = dict(pending)
                pending["attempt"] = attempt
                bound_state = {
                    "checkpoint": checkpoint,
                    "pending_review": pending,
                    "receipts": state["receipts"],
                    "revision": state["revision"] + 1,
                    "schema_version": SCHEMA_VERSION,
                    "updated_at": _utc_now(),
                }
                encoded_bound = _encode_json_document(
                    bound_state,
                    state_size_limit,
                    STATE_NAME,
                )
                _atomic_write_bytes(
                    root / STATE_NAME,
                    encoded_bound,
                    test_mode,
                    "before_bound_attempt_state_replace",
                )
                state = bound_state
            _maybe_failpoint("after_directory_bind", test_mode)

            promoted_at = _utc_now()
            final_revision = state["revision"] + 1
            final_state = _build_promoted_state(
                state["receipts"],
                pending,
                baseline,
                review_id,
                request_digest,
                index_hash,
                report_hash,
                promoted_at,
                final_revision,
            )
            encoded_final = _encode_json_document(
                final_state,
                state_size_limit,
                STATE_NAME,
            )

            report_disposition = _conditional_install_at(
                report_parent_descriptor,
                f"{pending['week_id']}.md",
                report_bytes,
                pending["report_preimage"],
                request_digest,
                "report",
                config["limits"]["max_report_bytes"],
                "Weekly report",
                test_mode,
            )
            _maybe_failpoint("after_report_write", test_mode)
            index_disposition = _conditional_install_at(
                output_descriptor,
                "Weekly Reviews.md",
                index_bytes,
                pending["index_preimage"],
                request_digest,
                "index",
                config["limits"]["max_report_bytes"],
                "Weekly review index",
                test_mode,
            )
            _maybe_failpoint("after_index_write", test_mode)

            if _read_at_optional(
                report_parent_descriptor,
                f"{pending['week_id']}.md",
                config["limits"]["max_report_bytes"],
                "Weekly report",
            ) != report_bytes or _read_at_optional(
                output_descriptor,
                "Weekly Reviews.md",
                config["limits"]["max_report_bytes"],
                "Weekly review index",
            ) != index_bytes:
                raise ContractError(
                    "write_verification_failed",
                    "Weekly review documents changed before state promotion.",
                )
            _verify_unique_week_report_at(
                output_descriptor,
                (pending["week_id"][:4], f"{pending['week_id']}.md"),
                pending["week_id"],
                config["limits"]["max_report_bytes"],
            )
            _cleanup_attempt_artifacts_at(
                report_parent_descriptor,
                request_digest,
                "report",
                pending["report_preimage"],
                report_hash,
                config["limits"]["max_report_bytes"],
                "Weekly report",
            )
            _cleanup_attempt_artifacts_at(
                output_descriptor,
                request_digest,
                "index",
                pending["index_preimage"],
                index_hash,
                config["limits"]["max_report_bytes"],
                "Weekly review index",
            )
            _verify_rebound_documents(
                config, pending, attempt, report_bytes, index_bytes
            )
            _maybe_failpoint("before_state_promote", test_mode)
            _atomic_write_bytes(
                root / STATE_NAME,
                encoded_final,
                test_mode,
                "before_promote_state_replace",
            )
            _cleanup_snapshots(root, _snapshot_references(final_state))
        finally:
            if report_parent_descriptor is not None:
                os.close(report_parent_descriptor)
            if output_descriptor is not None:
                os.close(output_descriptor)
            if vault_descriptor is not None:
                os.close(vault_descriptor)

    output = _base_output(True, operation, request_id)
    output.update(
        {
            "already_promoted": False,
            "baseline_count": len(baseline),
            "config_revision": config_revision,
            "index_disposition": index_disposition,
            "promoted": True,
            "promoted_at": promoted_at,
            "report_disposition": report_disposition,
            "request_digest": request_digest,
            "review_id": review_id,
            "snapshot_count": len(_snapshot_references(final_state)),
            "state_revision": final_revision,
            "week_id": pending["week_id"],
        }
    )
    return output


def _maintenance_status(
    request: Dict[str, Any], operation: str, request_id: str
) -> Dict[str, Any]:
    _ensure_keys(request, {"operation", "protocol_version", "request_id"}, "request")
    with _locked_storage() as (root, test_mode):
        del test_mode
        config_revision, config = _read_config(root)
        if config is None:
            raise ContractError("not_configured", "weekly-review config is not set.")
        state = _read_state(root)
        snapshot_count, snapshot_bytes, _ = _snapshot_inventory(root, state)
        checkpoint = state.get("checkpoint")
        pending = state.get("pending_review")
        receipts = state["receipts"]
        latest_receipt: Optional[Tuple[str, Dict[str, Any]]] = None
        if receipts:
            latest_receipt = max(
                receipts.items(), key=lambda item: item[1]["state_revision"]
            )
        compatibility = _baseline_compatibility(state, config, config_revision)
        path_diagnostics = _config_live_path_diagnostics(config)
    output = _base_output(True, operation, request_id)
    output.update(
        {
            "baseline_compatibility": compatibility,
            "baseline_count": len(checkpoint["baseline"]) if checkpoint else 0,
            "checkpoint_week_id": checkpoint["week_id"] if checkpoint else None,
            "config_revision": config_revision,
            "pending_review": (
                {
                    "config_revision": pending["config_revision"],
                    "coverage": pending["coverage"],
                    "index_sha256": pending["index_sha256"],
                    "observation_count": len(pending["observations"]),
                    "phase": pending["phase"],
                    "preview_sha256": pending["preview_sha256"],
                    "report_sha256": pending["report_sha256"],
                    "review_id": pending["review_id"],
                    "scope_fingerprint": pending["scope_fingerprint"],
                    "staged_at": pending["staged_at"],
                    "week_id": pending["week_id"],
                    "window": pending["window"],
                }
                if pending
                else None
            ),
            "pending_review_active": pending is not None,
            "receipt_count": len(receipts),
            "latest_receipt": (
                {
                    "promoted_at": latest_receipt[1]["promoted_at"],
                    "request_digest": latest_receipt[1]["request_digest"],
                    "review_id": latest_receipt[0],
                    "state_revision": latest_receipt[1]["state_revision"],
                    "week_id": latest_receipt[1]["week_id"],
                }
                if latest_receipt is not None
                else None
            ),
            "live_paths_valid": all(
                item["status"] == "available" for item in path_diagnostics
            ),
            "path_diagnostics": path_diagnostics,
            "reset_required": compatibility["status"] == "incompatible",
            "snapshot_bytes": snapshot_bytes,
            "snapshot_count": snapshot_count,
            "snapshot_opt_in_root_count": sum(
                1
                for entry in config["files"]["content_roots"]
                if entry["snapshot_text"]
            ),
            "snapshot_purge_available": snapshot_count > 0,
            "state_revision": state["revision"],
            "state_written": False,
        }
    )
    return output


def _parse_maintenance_confirmation(
    request: Dict[str, Any], operation: str
) -> Tuple[int, int]:
    if _require_bool(request["confirmed"], "confirmed") is not True:
        raise ContractError(
            "confirmation_required", f"{operation} requires confirmed=true."
        )
    expected_config = _require_int(
        request["expected_config_revision"],
        "expected_config_revision",
        1,
        2**63 - 1,
    )
    expected_state = _require_int(
        request["expected_state_revision"],
        "expected_state_revision",
        0,
        2**63 - 1,
    )
    return expected_config, expected_state


def _snapshots_purge(
    request: Dict[str, Any], operation: str, request_id: str
) -> Dict[str, Any]:
    allowed = {
        "operation",
        "protocol_version",
        "request_id",
        "confirmed",
        "expected_config_revision",
        "expected_state_revision",
    }
    _ensure_keys(request, allowed, "request")
    _require_keys(
        request,
        {"confirmed", "expected_config_revision", "expected_state_revision"},
        "request",
    )
    expected_config, expected_state = _parse_maintenance_confirmation(
        request, operation
    )
    with _locked_storage() as (root, test_mode):
        _cleanup_atomic_temps(root)
        config_revision, config = _read_config(root)
        if config is None:
            raise ContractError("not_configured", "weekly-review config is not set.")
        state = _read_state(root)
        _require_no_pending_review(state, operation)
        if config_revision != expected_config or state["revision"] != expected_state:
            raise ContractError(
                "revision_conflict",
                "Config or state changed after the maintenance preview.",
                {
                    "actual_config_revision": config_revision,
                    "actual_state_revision": state["revision"],
                    "expected_config_revision": expected_config,
                    "expected_state_revision": expected_state,
                },
            )
        _cleanup_snapshots(root, _snapshot_references(state))
        snapshot_count, snapshot_bytes, _ = _snapshot_inventory(root, state)
        checkpoint = state.get("checkpoint")
        new_revision = state["revision"]
        if checkpoint is not None and snapshot_count:
            baseline = {
                digest: {"content_sha256": entry["content_sha256"]}
                for digest, entry in checkpoint["baseline"].items()
            }
            new_revision += 1
            new_state = {
                "checkpoint": {
                    "baseline": baseline,
                    "config_revision": checkpoint["config_revision"],
                    "index_sha256": checkpoint["index_sha256"],
                    "report_sha256": checkpoint["report_sha256"],
                    "review_id": checkpoint["review_id"],
                    "scope_fingerprint": checkpoint["scope_fingerprint"],
                    "week_id": checkpoint["week_id"],
                },
                "pending_review": None,
                "receipts": state["receipts"],
                "revision": new_revision,
                "schema_version": SCHEMA_VERSION,
                "updated_at": _utc_now(),
            }
            _atomic_write_json(
                root / STATE_NAME,
                new_state,
                test_mode,
                "before_state_replace",
                _write_size_limit(
                    MAX_STATE_BYTES, TEST_MAX_STATE_BYTES_ENV, test_mode
                ),
            )
        # The state pointer is changed before deleting raw text. A crash leaves
        # only unreferenced files; retry/status recovery can remove them safely.
        _cleanup_snapshots(root, set())
    output = _base_output(True, operation, request_id)
    output.update(
        {
            "config_revision": config_revision,
            "hash_baseline_preserved": True,
            "snapshot_bytes_purged": snapshot_bytes,
            "snapshot_count_purged": snapshot_count,
            "state_revision": new_revision,
        }
    )
    return output


def _baseline_reset(
    request: Dict[str, Any], operation: str, request_id: str
) -> Dict[str, Any]:
    allowed = {
        "operation",
        "protocol_version",
        "request_id",
        "confirmed",
        "expected_config_revision",
        "expected_state_revision",
    }
    _ensure_keys(request, allowed, "request")
    _require_keys(
        request,
        {"confirmed", "expected_config_revision", "expected_state_revision"},
        "request",
    )
    expected_config, expected_state = _parse_maintenance_confirmation(
        request, operation
    )
    with _locked_storage() as (root, test_mode):
        _cleanup_atomic_temps(root)
        config_revision, config = _read_config(root)
        if config is None:
            raise ContractError("not_configured", "weekly-review config is not set.")
        state = _read_state(root)
        _require_no_pending_review(state, operation)
        if config_revision != expected_config or state["revision"] != expected_state:
            raise ContractError(
                "revision_conflict",
                "Config or state changed after the maintenance preview.",
                {
                    "actual_config_revision": config_revision,
                    "actual_state_revision": state["revision"],
                    "expected_config_revision": expected_config,
                    "expected_state_revision": expected_state,
                },
            )
        _cleanup_snapshots(root, _snapshot_references(state))
        snapshot_count, snapshot_bytes, _ = _snapshot_inventory(root, state)
        checkpoint = state.get("checkpoint")
        baseline_count = len(checkpoint["baseline"]) if checkpoint else 0
        new_revision = state["revision"]
        preserved_checkpoint_identity = checkpoint is not None
        if checkpoint is not None:
            new_revision += 1
            new_state = {
                "checkpoint": {
                    "baseline": {},
                    "config_revision": config_revision,
                    "index_sha256": checkpoint["index_sha256"],
                    "report_sha256": checkpoint["report_sha256"],
                    "review_id": checkpoint["review_id"],
                    "scope_fingerprint": _baseline_scope_fingerprint(config),
                    "week_id": checkpoint["week_id"],
                },
                "pending_review": None,
                "receipts": state["receipts"],
                "revision": new_revision,
                "schema_version": SCHEMA_VERSION,
                "updated_at": _utc_now(),
            }
            _atomic_write_json(
                root / STATE_NAME,
                new_state,
                test_mode,
                "before_state_replace",
                _write_size_limit(
                    MAX_STATE_BYTES, TEST_MAX_STATE_BYTES_ENV, test_mode
                ),
            )
        _cleanup_snapshots(root, set())
    output = _base_output(True, operation, request_id)
    output.update(
        {
            "baseline_count_cleared": baseline_count,
            "config_revision": config_revision,
            "preserved_checkpoint_identity": preserved_checkpoint_identity,
            "snapshot_bytes_purged": snapshot_bytes,
            "snapshot_count_purged": snapshot_count,
            "state_revision": new_revision,
        }
    )
    return output


def _baseline_compare(request: Dict[str, Any], operation: str, request_id: str) -> Dict[str, Any]:
    _ensure_keys(
        request,
        {"operation", "protocol_version", "request_id", "observations"},
        "request",
    )
    _require_keys(request, {"observations"}, "request")
    with _locked_storage() as (root, test_mode):
        del test_mode
        _cleanup_atomic_temps(root)
        config_revision, config = _read_config(root)
        if config is None:
            raise ContractError("not_configured", "weekly-review config is not set.")
        state = _read_state(root)
        compatibility = _require_baseline_compatible(state, config, config_revision)
        _cleanup_snapshots(root, _snapshot_references(state))
        observations = _parse_observations(request["observations"], config)
        comparisons = _compare_observations(root, state, observations, config)
    output = _base_output(True, operation, request_id)
    output.update(
        {
            "checkpoint_week_id": (
                state["checkpoint"]["week_id"] if state["checkpoint"] else None
            ),
            "comparisons": comparisons,
            "baseline_compatibility": compatibility,
            "config_revision": config_revision,
            "state_revision": state["revision"],
            "state_written": False,
        }
    )
    return output


HANDLERS = {
    "self-test": _self_test,
    "config.validate": _config_validate,
    "config.get": _config_get,
    "config.set": _config_set,
    "review.stage": _review_stage,
    "review.write-promote": _review_write_promote,
    "review.abort": _review_abort,
    "maintenance.status": _maintenance_status,
    "snapshots.purge": _snapshots_purge,
    "baseline.reset": _baseline_reset,
    "baseline.compare": _baseline_compare,
}


def main() -> int:
    request: Optional[Dict[str, Any]] = None
    operation: Optional[str] = None
    request_id: Optional[str] = None
    try:
        request = _read_request()
        operation, request_id = _parse_common(request)
        handler = HANDLERS.get(operation)
        if handler is None:
            raise ContractError("unsupported_operation", "operation is not supported.")
        _emit(handler(request, operation, request_id))
        return 0
    except ContractError as failure:
        _emit_failure(failure, operation, request_id)
        return 2
    except Exception:
        _emit_failure(
            ContractError("internal_error", "Unexpected local state manager failure."),
            operation,
            request_id,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
