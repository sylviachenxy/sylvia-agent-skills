#!/usr/bin/env python3
"""Stateful, fail-closed executor for the personal-scheduler EventKit bridge."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import pwd
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


EXECUTOR_VERSION = "1.0.0"
STATE_SCHEMA_VERSION = 2
MAX_INPUT_BYTES = 1_048_576
MAX_STATE_BYTES = 1_048_576
APP_DIR_NAME = "io.github.sylviachenxy.sylvia-agent-skills.personal-scheduler-eventkit"
STATE_FILE_NAME = "state-v2.json"
LEGACY_STATE_FILE_NAMES = ("state-v1.json",)
LOCK_FILE_NAME = "state.lock"
BRIDGE_DIR = Path(__file__).resolve().parent / "apple-eventkit-bridge"
BRIDGE_RUNNER = BRIDGE_DIR / "run.sh"
BRIDGE_INTERNAL_TOKEN = "personal-scheduler-executor-v1-9F2D7B1C"
os.umask(0o077)

READ_COMMANDS = {
    "authorize",
    "sources list",
    "containers list",
    "availability",
    "events list",
    "reminders list",
    "items find",
    "items get",
}
MUTATION_COMMANDS = {
    "containers create": "container_create",
    "items create": "create",
    "items patch": "patch",
    "reminders complete": "complete",
    "items delete": "delete",
    "items claim": "claim",
    "unmanaged items patch": "unmanaged_patch",
    "unmanaged reminders complete": "unmanaged_complete",
    "unmanaged items delete": "unmanaged_delete",
}
COMMAND_BY_KIND = {kind: command for command, kind in MUTATION_COMMANDS.items()}
LOW_LEVEL_ACCEPTS_OPERATION_ID = {
    "items claim",
    "unmanaged items patch",
    "unmanaged reminders complete",
    "unmanaged items delete",
}


class ExecutorError(Exception):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def validate_uuid_id(value: Any, prefix: str, field: str) -> str:
    if not isinstance(value, str) or not value.startswith(prefix + "-") or value != value.upper():
        raise ExecutorError("validation_error", f"{field} must be {prefix}- followed by a canonical uppercase UUID.")
    suffix = value[len(prefix) + 1 :]
    try:
        parsed = uuid.UUID(suffix)
    except (ValueError, AttributeError) as exc:
        raise ExecutorError("validation_error", f"{field} must be {prefix}- followed by a canonical uppercase UUID.") from exc
    if str(parsed).upper() != suffix:
        raise ExecutorError("validation_error", f"{field} must be {prefix}- followed by a canonical uppercase UUID.")
    return value


def require_sha256(value: Any, field: str, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or len(value) != 71 or not value.startswith("sha256:"):
        raise ExecutorError("validation_error", f"{field} must be a sha256 fingerprint.")
    try:
        int(value[7:], 16)
    except ValueError as exc:
        raise ExecutorError("validation_error", f"{field} must be a sha256 fingerprint.") from exc
    return value


def require_string(value: Any, field: str, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ExecutorError("validation_error", f"{field} must be a non-empty string.")
    return value


def strict_keys(value: dict[str, Any], allowed: set[str], context: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ExecutorError("validation_error", f"Unknown key(s) in {context}.", {"keys": unknown})


def default_state() -> dict[str, Any]:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "revision": 0,
        "event_store_id": None,
        "timezone": None,
        "scopes": {
            "event": {"read_container_ids": [], "write_source_id": None, "write_container_id": None, "private_confirmed": False},
            "reminder": {"read_container_ids": [], "write_source_id": None, "write_container_id": None, "private_confirmed": False},
        },
        "schedules": {},
        "operations": {},
    }


def validate_scope(value: Any, entity: str) -> None:
    if not isinstance(value, dict):
        raise ExecutorError("unsafe_state", f"state.scopes.{entity} is invalid.")
    strict_keys(value, {"read_container_ids", "write_source_id", "write_container_id", "private_confirmed"}, f"state.scopes.{entity}")
    read_ids = value.get("read_container_ids")
    if not isinstance(read_ids, list) or len(read_ids) > 50 or any(not isinstance(item, str) or not item for item in read_ids) or len(set(read_ids)) != len(read_ids):
        raise ExecutorError("unsafe_state", f"state.scopes.{entity}.read_container_ids is invalid.")
    require_string(value.get("write_source_id"), f"state.scopes.{entity}.write_source_id", nullable=True)
    require_string(value.get("write_container_id"), f"state.scopes.{entity}.write_container_id", nullable=True)
    if not isinstance(value.get("private_confirmed"), bool):
        raise ExecutorError("unsafe_state", f"state.scopes.{entity}.private_confirmed is invalid.")


def validate_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ExecutorError("unsafe_state", "The state file must contain one JSON object.")
    strict_keys(value, {"schema_version", "revision", "event_store_id", "timezone", "scopes", "schedules", "operations"}, "state")
    if value.get("schema_version") != STATE_SCHEMA_VERSION:
        raise ExecutorError("unsupported_state_schema", "The state schema version is not supported.")
    if not isinstance(value.get("revision"), int) or value["revision"] < 0:
        raise ExecutorError("unsafe_state", "state.revision is invalid.")
    require_string(value.get("event_store_id"), "state.event_store_id", nullable=True)
    timezone = value.get("timezone")
    if timezone is not None:
        require_string(timezone, "state.timezone")
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise ExecutorError("unsafe_state", "state.timezone is not a valid IANA timezone.") from exc
    scopes = value.get("scopes")
    if not isinstance(scopes, dict) or set(scopes) != {"event", "reminder"}:
        raise ExecutorError("unsafe_state", "state.scopes is invalid.")
    validate_scope(scopes["event"], "event")
    validate_scope(scopes["reminder"], "reminder")
    schedules = value.get("schedules")
    operations = value.get("operations")
    if not isinstance(schedules, dict) or not isinstance(operations, dict):
        raise ExecutorError("unsafe_state", "state schedules or operations are invalid.")
    allowed_schedule = {"entity", "state", "event_store_id", "source_id", "container_id", "item_id", "external_id", "intent_hash", "last_fingerprint", "updated_at"}
    for schedule_id, record in schedules.items():
        validate_uuid_id(schedule_id, "PS", "state schedule key")
        if not isinstance(record, dict):
            raise ExecutorError("unsafe_state", "A schedule record is invalid.")
        strict_keys(record, allowed_schedule, f"state.schedules.{schedule_id}")
        schedule_entity = record.get("entity")
        schedule_state = record.get("state")
        if (
            not isinstance(schedule_entity, str)
            or schedule_entity not in {"event", "reminder"}
            or not isinstance(schedule_state, str)
            or schedule_state not in {"pending", "verified_local", "outcome_unknown", "conflict", "deleted", "retired"}
        ):
            raise ExecutorError("unsafe_state", "A schedule record has an invalid entity or state.")
        for key in ("event_store_id", "source_id", "container_id", "item_id", "external_id", "updated_at"):
            require_string(record.get(key), f"schedule.{key}", nullable=True)
        require_sha256(record.get("intent_hash"), "schedule.intent_hash", nullable=True)
        require_sha256(record.get("last_fingerprint"), "schedule.last_fingerprint", nullable=True)
    allowed_operation = {"kind", "phase", "entity", "schedule_id", "event_store_id", "source_id", "container_id", "item_id", "before_fingerprint", "intent_hash", "created_at", "started_at", "finished_at", "outcome", "error_code"}
    allowed_kinds = set(MUTATION_COMMANDS.values())
    allowed_phases = {"prepared", "in_flight", "outcome_unknown", "terminal"}
    for operation_id, record in operations.items():
        if not isinstance(record, dict):
            raise ExecutorError("unsafe_state", "An operation record is invalid.")
        prefix = "COP" if record.get("kind") == "container_create" else "OP"
        validate_uuid_id(operation_id, prefix, "state operation key")
        strict_keys(record, allowed_operation, f"state.operations.{operation_id}")
        operation_kind = record.get("kind")
        operation_phase = record.get("phase")
        operation_entity_value = record.get("entity")
        if (
            not isinstance(operation_kind, str)
            or operation_kind not in allowed_kinds
            or not isinstance(operation_phase, str)
            or operation_phase not in allowed_phases
            or not isinstance(operation_entity_value, str)
            or operation_entity_value not in {"event", "reminder"}
        ):
            raise ExecutorError("unsafe_state", "An operation record has an invalid kind, phase, or entity.")
        schedule_id = record.get("schedule_id")
        if schedule_id is not None:
            validate_uuid_id(schedule_id, "PS", "operation.schedule_id")
        for key in ("event_store_id", "source_id", "container_id", "item_id", "created_at", "started_at", "finished_at", "outcome", "error_code"):
            require_string(record.get(key), f"operation.{key}", nullable=True)
        require_sha256(record.get("before_fingerprint"), "operation.before_fingerprint", nullable=True)
        require_sha256(record.get("intent_hash"), "operation.intent_hash")
        operation_outcome = record.get("outcome")
        if operation_outcome is not None and (
            not isinstance(operation_outcome, str)
            or operation_outcome not in {"verified_local", "deleted", "not_applied", "outcome_unknown", "conflict", "abandon_unknown"}
        ):
            raise ExecutorError("unsafe_state", "An operation record has an invalid outcome.")
        phase = record["phase"]
        outcome = record.get("outcome")
        if phase in {"in_flight", "outcome_unknown"} and record.get("event_store_id") is None:
            raise ExecutorError("unsafe_state", "An operation that entered in_flight must retain its EventKit store identity.")
        if phase == "terminal" and record.get("started_at") is not None and record.get("event_store_id") is None:
            raise ExecutorError("unsafe_state", "A terminal operation that reached EventKit must retain its EventKit store identity.")
        if phase == "prepared" and any(record.get(key) is not None for key in ("started_at", "finished_at", "outcome")):
            raise ExecutorError("unsafe_state", "A prepared operation cannot contain started, finished, or outcome fields.")
        if phase == "in_flight" and (record.get("started_at") is None or record.get("finished_at") is not None or outcome is not None):
            raise ExecutorError("unsafe_state", "An in-flight operation has inconsistent lifecycle fields.")
        if phase == "outcome_unknown" and (record.get("started_at") is None or record.get("finished_at") is None or outcome != "outcome_unknown"):
            raise ExecutorError("unsafe_state", "An outcome-unknown operation has inconsistent lifecycle fields.")
        if phase == "terminal":
            if record.get("finished_at") is None or outcome in {None, "outcome_unknown"}:
                raise ExecutorError("unsafe_state", "A terminal operation is incomplete.")
            if record.get("started_at") is None and outcome != "not_applied":
                raise ExecutorError("unsafe_state", "Only a prepared operation may terminate without entering in_flight.")
    return value


class StateStore:
    def __init__(self, root_override: Path | None = None):
        self.root_override = root_override

    @property
    def display_path(self) -> str:
        if self.root_override is not None:
            return str(self.root_override / STATE_FILE_NAME)
        return f"~/Library/Application Support/{APP_DIR_NAME}/{STATE_FILE_NAME}"

    def _open_root(self, create: bool) -> int:
        if self.root_override is not None:
            path = self.root_override
            if create and not path.exists():
                path.mkdir(mode=0o700, parents=False)
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(path, flags)
            except FileNotFoundError as exc:
                raise ExecutorError("state_missing", "The test state directory does not exist.") from exc
            except OSError as exc:
                raise ExecutorError("unsafe_state", "The test state directory is unavailable.", {"errno": exc.errno}) from exc
        else:
            user = pwd.getpwuid(os.getuid())
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
            try:
                home_fd = os.open(user.pw_dir, flags)
                library_fd = os.open("Library", flags, dir_fd=home_fd)
                os.close(home_fd)
                home_fd = -1
                support_fd = os.open("Application Support", flags, dir_fd=library_fd)
                os.close(library_fd)
                library_fd = -1
                if create:
                    try:
                        os.mkdir(APP_DIR_NAME, 0o700, dir_fd=support_fd)
                    except FileExistsError:
                        pass
                descriptor = os.open(APP_DIR_NAME, flags, dir_fd=support_fd)
                os.close(support_fd)
                support_fd = -1
            except FileNotFoundError as exc:
                for candidate in (locals().get("home_fd"), locals().get("library_fd"), locals().get("support_fd")):
                    if isinstance(candidate, int):
                        with contextlib.suppress(OSError):
                            os.close(candidate)
                raise ExecutorError("state_missing", "The private state directory does not exist yet.") from exc
            except OSError as exc:
                for candidate in (locals().get("home_fd"), locals().get("library_fd"), locals().get("support_fd")):
                    if isinstance(candidate, int):
                        with contextlib.suppress(OSError):
                            os.close(candidate)
                raise ExecutorError("unsafe_state", "The private Application Support directory is unavailable.", {"errno": exc.errno}) from exc
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
            os.close(descriptor)
            raise ExecutorError("unsafe_state", "The state directory failed owner, type, or 0700 mode validation.")
        return descriptor

    @contextlib.contextmanager
    def locked(self, timeout_seconds: float = 10.0) -> Iterator[tuple[int, dict[str, Any]]]:
        root_fd = self._open_root(create=True)
        lock_fd = -1
        try:
            flags = os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
            try:
                lock_fd = os.open(LOCK_FILE_NAME, flags, 0o600, dir_fd=root_fd)
            except OSError as exc:
                raise ExecutorError("unsafe_lock", "The state lock is unavailable.", {"errno": exc.errno}) from exc
            info = os.fstat(lock_fd)
            if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
                raise ExecutorError("unsafe_lock", "The state lock failed owner, type, or 0600 mode validation.")
            deadline = time.monotonic() + timeout_seconds
            while True:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise ExecutorError("lock_timeout", "Another scheduler operation is still in progress.")
                    time.sleep(0.05)
            yield root_fd, self._load(root_fd)
        finally:
            if lock_fd >= 0:
                with contextlib.suppress(OSError):
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    os.close(lock_fd)
            os.close(root_fd)

    def _load(self, root_fd: int) -> dict[str, Any]:
        flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(STATE_FILE_NAME, flags, dir_fd=root_fd)
        except FileNotFoundError:
            self._reject_legacy_state(root_fd)
            return default_state()
        except OSError as exc:
            raise ExecutorError("unsafe_state", "The state file is unavailable.", {"errno": exc.errno}) from exc
        try:
            info = os.fstat(descriptor)
            if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > MAX_STATE_BYTES:
                raise ExecutorError("unsafe_state", "The state file failed owner, type, size, or 0600 mode validation.")
            data = bytearray()
            while True:
                chunk = os.read(descriptor, 65_536)
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > MAX_STATE_BYTES:
                    raise ExecutorError("unsafe_state", "The state file exceeds the size limit.")
        finally:
            os.close(descriptor)
        try:
            parsed = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ExecutorError("unsafe_state", "The state file is not valid JSON; it was not replaced.") from exc
        return validate_state(parsed)

    @staticmethod
    def _reject_legacy_state(root_fd: int) -> None:
        for legacy_name in LEGACY_STATE_FILE_NAMES:
            try:
                os.stat(legacy_name, dir_fd=root_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ExecutorError(
                    "unsafe_state",
                    "A legacy state path could not be inspected safely.",
                    {"errno": exc.errno},
                ) from exc
            raise ExecutorError(
                "legacy_state_requires_manual_audit",
                "A pre-first-cut state-v1 file exists. It is not auto-migrated because unresolved operations lack a trustworthy EventKit epoch; audit it manually before starting with state-v2.",
            )

    def save(self, root_fd: int, state: dict[str, Any]) -> None:
        validate_state(state)
        data = canonical_bytes(state) + b"\n"
        if len(data) > MAX_STATE_BYTES:
            raise ExecutorError("unsafe_state", "The state exceeds the size limit.")
        temp_name = f".state-{uuid.uuid4().hex}.tmp"
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        descriptor = -1
        try:
            descriptor = os.open(temp_name, flags, 0o600, dir_fd=root_fd)
            info = os.fstat(descriptor)
            if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
                raise ExecutorError("unsafe_state", "The temporary state file failed security validation.")
            offset = 0
            while offset < len(data):
                offset += os.write(descriptor, data[offset:])
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.rename(temp_name, STATE_FILE_NAME, src_dir_fd=root_fd, dst_dir_fd=root_fd)
            os.fsync(root_fd)
        finally:
            if descriptor >= 0:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temp_name, dir_fd=root_fd)

    def peek(self) -> tuple[bool, dict[str, Any] | None]:
        try:
            root_fd = self._open_root(create=False)
        except ExecutorError as error:
            if error.code == "state_missing":
                return False, None
            raise
        try:
            flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(STATE_FILE_NAME, flags, dir_fd=root_fd)
            except FileNotFoundError:
                self._reject_legacy_state(root_fd)
                return False, None
            os.close(descriptor)
            return True, self._load(root_fd)
        finally:
            os.close(root_fd)


def read_stdin(required: bool = True) -> dict[str, Any]:
    data = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(data) > MAX_INPUT_BYTES:
        raise ExecutorError("invalid_json", "stdin exceeds the 1 MiB input limit.")
    if not data.strip():
        if required:
            raise ExecutorError("invalid_json", "A JSON object is required on stdin.")
        return {}
    try:
        value = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ExecutorError("invalid_json", "stdin is not valid JSON.") from exc
    if not isinstance(value, dict):
        raise ExecutorError("invalid_json", "stdin must contain one JSON object.")
    return value


def normalize_bridge_output(exit_code: int, output: dict[str, Any]) -> dict[str, Any]:
    if exit_code == 0 or output.get("ok") is not True:
        return output
    normalized = dict(output)
    normalized["ok"] = False
    normalized.pop("mutated", None)
    error = normalized.get("error")
    if not isinstance(error, dict) or not isinstance(error.get("code"), str) or not isinstance(error.get("message"), str):
        normalized["error"] = {
            "code": "bridge_protocol_error",
            "message": "The bridge exited nonzero while claiming success; the response was rejected.",
            "details": {"exit_code": exit_code},
        }
    return normalized


def run_bridge(command: str, payload: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    if not BRIDGE_RUNNER.is_file():
        raise ExecutorError("bridge_missing", "The bundled EventKit bridge runner is missing.")
    argv = [str(BRIDGE_RUNNER), *command.split(" ")]
    raw_input = b"" if payload is None else canonical_bytes(payload) + b"\n"
    child_environment = os.environ.copy()
    child_environment["PERSONAL_SCHEDULER_INTERNAL_TOKEN"] = BRIDGE_INTERNAL_TOKEN
    try:
        completed = subprocess.run(
            argv,
            input=raw_input,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=330,
            check=False,
            env=child_environment,
        )
    except subprocess.TimeoutExpired as exc:
        raise ExecutorError("operation_timeout_outcome_unknown", "The bridge runner exceeded the executor watchdog.") from exc
    if len(completed.stdout) > MAX_INPUT_BYTES:
        raise ExecutorError("bridge_protocol_error", "The bridge output exceeds the size limit.")
    if len(completed.stderr) > 65_536:
        raise ExecutorError("bridge_protocol_error", "The bridge diagnostic output exceeds the size limit.")
    try:
        output = json.loads(completed.stdout)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ExecutorError("bridge_protocol_error", "The bridge did not return one valid JSON object.", {"exit_code": completed.returncode}) from exc
    if not isinstance(output, dict) or not isinstance(output.get("ok"), bool):
        raise ExecutorError("bridge_protocol_error", "The bridge output envelope is invalid.")
    return completed.returncode, normalize_bridge_output(completed.returncode, output)


def public_intent(command: str, request: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(request)
    normalized.pop("preview_hash", None)
    normalized.pop("dry_run", None)
    return {"command": command, "request": normalized}


def low_level_request(
    command: str,
    request: dict[str, Any],
    dry_run: bool,
    expected_event_store_id: str | None = None,
) -> dict[str, Any]:
    result = dict(request)
    result.pop("preview_hash", None)
    result.pop("expected_event_store_id", None)
    if command not in LOW_LEVEL_ACCEPTS_OPERATION_ID:
        result.pop("operation_id", None)
    result["dry_run"] = dry_run
    if not dry_run:
        if not isinstance(expected_event_store_id, str) or not expected_event_store_id:
            raise ExecutorError(
                "bridge_protocol_error",
                "An actual mutation requires the EventKit store identity from its fresh preflight.",
            )
        result["expected_event_store_id"] = expected_event_store_id
    return result


def operation_entity(command: str, request: dict[str, Any]) -> str:
    if command in {"reminders complete", "unmanaged reminders complete"}:
        return "reminder"
    entity = request.get("entity")
    if not isinstance(entity, str) or entity not in {"event", "reminder"}:
        raise ExecutorError("validation_error", "entity must be 'event' or 'reminder'.")
    return entity


def operation_schedule_id(command: str, request: dict[str, Any]) -> str | None:
    if command in {"items create", "items patch", "items claim"}:
        managed = request.get("managed")
        if not isinstance(managed, dict):
            raise ExecutorError("validation_error", "managed must be an object.")
        value = managed.get("schedule_id")
    else:
        value = request.get("schedule_id")
    if value is None:
        return None
    return validate_uuid_id(value, "PS", "schedule_id")


def verify_write_scope(state: dict[str, Any], command: str, request: dict[str, Any], entity: str) -> None:
    source_id = require_string(request.get("source_id"), "source_id")
    scope = state["scopes"][entity]
    if command == "containers create":
        if not request.get("confirm_icloud_source"):
            raise ExecutorError("confirmation_required", "Container creation requires explicit iCloud source confirmation.")
        return
    container_id = require_string(request.get("container_id"), "container_id")
    if not scope["private_confirmed"] or scope["write_source_id"] != source_id or scope["write_container_id"] != container_id:
        raise ExecutorError("write_scope_mismatch", "The request does not match the user-confirmed private write scope.")
    if request.get("confirm_private_container") is not True:
        raise ExecutorError("confirmation_required", "The request must repeat private-container confirmation.")


def verify_read_scope(state: dict[str, Any], command: str, request: dict[str, Any]) -> None:
    if command in {"availability", "events list"}:
        requested = request.get("calendar_ids")
        allowed = state["scopes"]["event"]["read_container_ids"]
        if (
            not isinstance(requested, list)
            or not 1 <= len(requested) <= 50
            or any(not isinstance(item, str) or not item for item in requested)
            or len(set(requested)) != len(requested)
            or not set(requested).issubset(set(allowed))
        ):
            raise ExecutorError("read_scope_mismatch", "calendar_ids exceed the user-confirmed Calendar read scope.")
        return
    if command == "reminders list":
        requested = request.get("list_ids")
        allowed = state["scopes"]["reminder"]["read_container_ids"]
        if (
            not isinstance(requested, list)
            or not 1 <= len(requested) <= 50
            or any(not isinstance(item, str) or not item for item in requested)
            or len(set(requested)) != len(requested)
            or not set(requested).issubset(set(allowed))
        ):
            raise ExecutorError("read_scope_mismatch", "list_ids exceed the user-confirmed Reminders read scope.")
        return
    if command in {"items find", "items get"}:
        entity = request.get("entity")
        if not isinstance(entity, str) or entity not in {"event", "reminder"}:
            raise ExecutorError("validation_error", "entity must be 'event' or 'reminder'.")
        container_id = require_string(request.get("container_id"), "container_id")
        scope = state["scopes"][entity]
        allowed = set(scope["read_container_ids"])
        if scope["write_container_id"] is not None:
            allowed.add(scope["write_container_id"])
        if container_id not in allowed:
            raise ExecutorError("read_scope_mismatch", "The item container exceeds the user-confirmed read/write scope.")


def invalidate_for_store_change(state: dict[str, Any], new_store_id: str) -> None:
    for scope in state["scopes"].values():
        scope["read_container_ids"] = []
        scope["write_source_id"] = None
        scope["write_container_id"] = None
        scope["private_confirmed"] = False
    for schedule in state["schedules"].values():
        if schedule["state"] not in {"deleted", "retired"}:
            schedule["state"] = "conflict"
            schedule["source_id"] = None
            schedule["container_id"] = None
            schedule["item_id"] = None
            schedule["external_id"] = None
            schedule["event_store_id"] = new_store_id
            schedule["updated_at"] = now_utc()
    state["event_store_id"] = new_store_id


def operation_blocks_target(record: dict[str, Any]) -> bool:
    return record["phase"] in {"prepared", "in_flight", "outcome_unknown"} or (
        record["phase"] == "terminal" and record.get("outcome") in {"conflict", "abandon_unknown"}
    )


def same_operation_target(
    record: dict[str, Any],
    *,
    entity: str,
    schedule_id: str | None,
    source_id: Any,
    container_id: Any,
    item_id: Any,
    operation_kind: str,
) -> bool:
    if record.get("entity") != entity:
        return False
    if schedule_id is not None and record.get("schedule_id") == schedule_id:
        return True
    if item_id is not None and record.get("item_id") == item_id and record.get("source_id") == source_id and record.get("container_id") == container_id:
        return True
    if operation_kind == "container_create" and record.get("kind") == "container_create" and record.get("source_id") == source_id:
        return True
    return False


def guard_target_is_not_unresolved(
    state: dict[str, Any],
    operation_id: str,
    operation_kind: str,
    entity: str,
    schedule_id: str | None,
    request: dict[str, Any],
) -> None:
    for other_id, other in state["operations"].items():
        if other_id == operation_id or not operation_blocks_target(other):
            continue
        if same_operation_target(
            other,
            entity=entity,
            schedule_id=schedule_id,
            source_id=request.get("source_id"),
            container_id=request.get("container_id"),
            item_id=request.get("item_id"),
            operation_kind=operation_kind,
        ):
            raise ExecutorError(
                "reconciliation_required",
                "Another unresolved or explicitly abandoned operation already owns this target.",
                {"operation_id": other_id, "phase": other["phase"], "outcome": other.get("outcome")},
            )
    if schedule_id is None:
        return
    schedule = state["schedules"].get(schedule_id)
    if schedule is None:
        return
    if operation_kind in {"create", "claim"}:
        raise ExecutorError("schedule_conflict", "This schedule_id is already reserved or managed; it cannot be claimed or created again.")
    if schedule["state"] in {"pending", "outcome_unknown", "conflict", "deleted", "retired"}:
        raise ExecutorError("reconciliation_required", "The schedule is not in a writable verified_local state.", {"schedule_id": schedule_id, "state": schedule["state"]})


def reserve_new_schedule(
    state: dict[str, Any],
    *,
    schedule_id: str | None,
    operation_kind: str,
    entity: str,
    request: dict[str, Any],
    intent_hash: str,
    before_fingerprint: str | None,
) -> None:
    if schedule_id is None or operation_kind not in {"create", "claim"}:
        return
    state["schedules"][schedule_id] = {
        "entity": entity,
        "state": "pending",
        "event_store_id": state.get("event_store_id"),
        "source_id": request.get("source_id"),
        "container_id": request.get("container_id"),
        "item_id": request.get("item_id"),
        "external_id": None,
        "intent_hash": intent_hash,
        "last_fingerprint": before_fingerprint,
        "updated_at": now_utc(),
    }


def mark_schedule_unknown(
    state: dict[str, Any],
    schedule_id: str | None,
    entity: str,
    request: dict[str, Any],
    intent_hash: str,
    *,
    clear_locator: bool = False,
) -> None:
    if schedule_id is None:
        return
    record = state["schedules"].get(schedule_id)
    if record is None:
        record = {
            "entity": entity,
            "state": "outcome_unknown",
            "event_store_id": state.get("event_store_id"),
            "source_id": None if clear_locator else request.get("source_id"),
            "container_id": None if clear_locator else request.get("container_id"),
            "item_id": None if clear_locator else request.get("item_id"),
            "external_id": None,
            "intent_hash": intent_hash,
            "last_fingerprint": None if clear_locator else request.get("expected_fingerprint"),
            "updated_at": now_utc(),
        }
        state["schedules"][schedule_id] = record
    else:
        record["state"] = "outcome_unknown"
        record["intent_hash"] = intent_hash
        record["updated_at"] = now_utc()
    if clear_locator:
        record["event_store_id"] = state.get("event_store_id")
        record["source_id"] = None
        record["container_id"] = None
        record["item_id"] = None
        record["external_id"] = None
        record["last_fingerprint"] = None


def summarize_operation(record: dict[str, Any]) -> dict[str, Any]:
    return dict(record)


def terminal_replay_result(
    command: str,
    operation_id: str,
    record: dict[str, Any],
    current_store_id: str | None,
) -> dict[str, Any]:
    successful = record.get("outcome") in {"verified_local", "deleted"}
    result: dict[str, Any] = {
        "ok": successful,
        "executor_version": EXECUTOR_VERSION,
        "command": command,
        "operation_id": operation_id,
        "already_terminal": True,
        "operation": summarize_operation(record),
        "mutated": False,
    }
    if successful:
        operation_store_id = require_string(record.get("event_store_id"), "operation.event_store_id")
        if current_store_id != operation_store_id:
            result["ok"] = False
            result["historical_event_store_id"] = operation_store_id
            result["error"] = {
                "code": "operation_terminal_stale_epoch",
                "message": "The operation succeeded in a previous EventKit store epoch and is not current verification for this store.",
            }
        else:
            result["event_store_id"] = operation_store_id
    else:
        result["error"] = {
            "code": "operation_terminal_without_success",
            "message": "This operation is terminal but did not verify the requested mutation; use a new operation only when its outcome is proven not_applied.",
        }
    return result


def require_bridge_store_id(output: dict[str, Any], context: str) -> str:
    value = output.get("event_store_id")
    if not isinstance(value, str) or not value:
        raise ExecutorError(
            "bridge_protocol_error",
            f"A successful {context} response omitted event_store_id; no mutation was authorized.",
        )
    return value


def handle_mutation(command: str, request: dict[str, Any], store: StateStore) -> dict[str, Any]:
    strict_keys(request, set(request), "request")  # command-specific strictness is enforced by the bridge
    if "expected_event_store_id" in request:
        raise ExecutorError(
            "validation_error",
            "expected_event_store_id is executor-owned and must not appear in a public request.",
        )
    if not isinstance(request.get("dry_run"), bool):
        raise ExecutorError("validation_error", "dry_run is required and must be a boolean for every mutation.")
    operation_kind = MUTATION_COMMANDS[command]
    prefix = "COP" if operation_kind == "container_create" else "OP"
    operation_id = validate_uuid_id(request.get("operation_id"), prefix, "operation_id")
    intent_hash = digest(public_intent(command, request))
    if request.get("dry_run") is True:
        entity = operation_entity(command, request)
        with store.locked() as (root_fd, state):
            verify_write_scope(state, command, request, entity)
            status, output = run_bridge(command, low_level_request(command, request, dry_run=True))
            if status == 0 and output.get("ok") is True:
                preview_store_id = require_bridge_store_id(output, "mutation preview")
                previous_store_id = state.get("event_store_id")
                if isinstance(previous_store_id, str) and previous_store_id != preview_store_id:
                    invalidate_for_store_change(state, preview_store_id)
                    state["revision"] += 1
                    store.save(root_fd, state)
                    raise ExecutorError("event_store_changed", "The EventKit store identity changed; saved scopes and locators were invalidated before preview.")
                if previous_store_id is None:
                    state["event_store_id"] = preview_store_id
                    state["revision"] += 1
                    store.save(root_fd, state)
        if status != 0 or output.get("ok") is not True:
            return output
        output["operation_id"] = operation_id
        output["preview_hash"] = intent_hash
        output["journaled"] = False
        return output
    preview_hash = require_sha256(request.get("preview_hash"), "preview_hash")
    if preview_hash != intent_hash:
        raise ExecutorError("preview_mismatch", "The confirmed preview does not match this mutation request.")
    entity = operation_entity(command, request)
    schedule_id = operation_schedule_id(command, request)
    before_fingerprint = request.get("expected_fingerprint")
    if before_fingerprint is not None:
        before_fingerprint = require_sha256(before_fingerprint, "expected_fingerprint")
    with store.locked() as (root_fd, state):
        verify_write_scope(state, command, request, entity)
        existing = state["operations"].get(operation_id)
        if existing is not None:
            if existing["intent_hash"] != intent_hash or existing["kind"] != operation_kind:
                raise ExecutorError("operation_conflict", "operation_id is already bound to a different intent.")
            if existing["phase"] == "terminal":
                return terminal_replay_result(command, operation_id, existing, state.get("event_store_id"))
            if existing["phase"] in {"in_flight", "outcome_unknown"}:
                raise ExecutorError("reconciliation_required", "This operation may already have reached EventKit; reconcile it before any retry.", {"operation_id": operation_id, "phase": existing["phase"]})
        else:
            guard_target_is_not_unresolved(state, operation_id, operation_kind, entity, schedule_id, request)
            state["operations"][operation_id] = {
                "kind": operation_kind,
                "phase": "prepared",
                "entity": entity,
                "schedule_id": schedule_id,
                "event_store_id": state.get("event_store_id"),
                "source_id": request.get("source_id"),
                "container_id": request.get("container_id"),
                "item_id": request.get("item_id"),
                "before_fingerprint": before_fingerprint,
                "intent_hash": intent_hash,
                "created_at": now_utc(),
                "started_at": None,
                "finished_at": None,
                "outcome": None,
                "error_code": None,
            }
            reserve_new_schedule(
                state,
                schedule_id=schedule_id,
                operation_kind=operation_kind,
                entity=entity,
                request=request,
                intent_hash=intent_hash,
                before_fingerprint=before_fingerprint,
            )
            state["revision"] += 1
            store.save(root_fd, state)

        preflight_status, preflight = run_bridge(command, low_level_request(command, request, dry_run=True))
        if preflight_status != 0 or preflight.get("ok") is not True:
            preflight["operation_id"] = operation_id
            preflight["journal_phase"] = "prepared"
            preflight["safe_to_retry_same_intent"] = True
            return preflight

        try:
            preflight_store_id = require_bridge_store_id(preflight, "fresh mutation preflight")
        except ExecutorError as error:
            state["operations"][operation_id]["error_code"] = error.code
            state["revision"] += 1
            store.save(root_fd, state)
            raise
        previous_store_id = state.get("event_store_id")
        if isinstance(previous_store_id, str) and previous_store_id != preflight_store_id:
            invalidate_for_store_change(state, preflight_store_id)
            state["operations"][operation_id]["error_code"] = "event_store_changed"
            state["revision"] += 1
            store.save(root_fd, state)
            raise ExecutorError("event_store_changed", "The EventKit store identity changed; saved scopes and locators were invalidated before mutation.")
        if previous_store_id is None:
            state["event_store_id"] = preflight_store_id
            state["revision"] += 1
            store.save(root_fd, state)

        record = state["operations"][operation_id]
        record["event_store_id"] = preflight_store_id
        record["phase"] = "in_flight"
        record["started_at"] = now_utc()
        record["error_code"] = None
        state["revision"] += 1
        store.save(root_fd, state)

        try:
            status, output = run_bridge(
                command,
                low_level_request(
                    command,
                    request,
                    dry_run=False,
                    expected_event_store_id=preflight_store_id,
                ),
            )
        except ExecutorError as error:
            record["phase"] = "outcome_unknown"
            record["outcome"] = "outcome_unknown"
            record["finished_at"] = now_utc()
            record["error_code"] = error.code
            mark_schedule_unknown(state, schedule_id, entity, request, intent_hash)
            state["revision"] += 1
            store.save(root_fd, state)
            raise

        if status == 0 and output.get("ok") is True:
            try:
                event_store_id = require_bridge_store_id(output, "mutation result")
            except ExecutorError as error:
                record["phase"] = "outcome_unknown"
                record["outcome"] = "outcome_unknown"
                record["finished_at"] = now_utc()
                record["error_code"] = error.code
                mark_schedule_unknown(state, schedule_id, entity, request, intent_hash)
                state["revision"] += 1
                store.save(root_fd, state)
                return {
                    "ok": False,
                    "command": command,
                    "operation_id": operation_id,
                    "journal_phase": "outcome_unknown",
                    "error": {"code": error.code, "message": error.message},
                    "mutation_outcome": "unknown",
                }
            if event_store_id != preflight_store_id:
                invalidate_for_store_change(state, event_store_id)
                record["phase"] = "outcome_unknown"
                record["outcome"] = "outcome_unknown"
                record["finished_at"] = now_utc()
                record["error_code"] = "event_store_changed_during_mutation"
                mark_schedule_unknown(state, schedule_id, entity, request, intent_hash, clear_locator=True)
                state["revision"] += 1
                store.save(root_fd, state)
                return {
                    "ok": False,
                    "command": command,
                    "operation_id": operation_id,
                    "journal_phase": "outcome_unknown",
                    "error": {
                        "code": "event_store_changed_during_mutation",
                        "message": "The successful bridge envelope came from a different EventKit store than its fresh preflight; the mutation outcome is not trusted.",
                    },
                    "mutation_outcome": "unknown",
                }
            record["phase"] = "terminal"
            record["finished_at"] = now_utc()
            record["outcome"] = "deleted" if operation_kind in {"delete", "unmanaged_delete"} else "verified_local"
            record["error_code"] = None
            state["event_store_id"] = event_store_id
            if operation_kind == "container_create" and isinstance(output.get("container"), dict):
                created_container_id = output["container"].get("container_id")
                if isinstance(created_container_id, str) and created_container_id:
                    record["container_id"] = created_container_id
            if schedule_id is not None:
                item = output.get("item") if isinstance(output.get("item"), dict) else None
                state["schedules"][schedule_id] = {
                    "entity": entity,
                    "state": "deleted" if operation_kind == "delete" else "verified_local",
                    "event_store_id": event_store_id,
                    "source_id": request.get("source_id"),
                    "container_id": request.get("container_id"),
                    "item_id": (item or {}).get("item_id") or request.get("item_id"),
                    "external_id": (item or {}).get("external_id"),
                    "intent_hash": intent_hash,
                    "last_fingerprint": (item or {}).get("fingerprint"),
                    "updated_at": now_utc(),
                }
            state["revision"] += 1
            store.save(root_fd, state)
            output["operation_id"] = operation_id
            output["journal_phase"] = "terminal"
            return output

        error = output.get("error") if isinstance(output.get("error"), dict) else {}
        error_code = error.get("code") if isinstance(error.get("code"), str) else "bridge_error"
        record["error_code"] = error_code
        record["finished_at"] = now_utc()
        # The actual child invocation started only after the durable in_flight
        # checkpoint. A low-level error code may be emitted either before a save
        # or during post-save readback, so it can never prove not_applied.
        record["phase"] = "outcome_unknown"
        record["outcome"] = "outcome_unknown"
        mark_schedule_unknown(state, schedule_id, entity, request, intent_hash)
        state["revision"] += 1
        store.save(root_fd, state)
        output.pop("mutated", None)
        output["operation_id"] = operation_id
        output["journal_phase"] = record["phase"]
        output["mutation_outcome"] = "unknown"
        return output


def parse_scope_input(value: Any, entity: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ExecutorError("validation_error", f"{entity} must be an object.")
    strict_keys(value, {"read_container_ids", "write_source_id", "write_container_id", "private_confirmed"}, entity)
    read_ids = value.get("read_container_ids")
    if not isinstance(read_ids, list) or len(read_ids) > 50 or any(not isinstance(item, str) or not item for item in read_ids) or len(set(read_ids)) != len(read_ids):
        raise ExecutorError("validation_error", f"{entity}.read_container_ids is invalid.")
    source_id = require_string(value.get("write_source_id"), f"{entity}.write_source_id", nullable=True)
    container_id = require_string(value.get("write_container_id"), f"{entity}.write_container_id", nullable=True)
    confirmed = value.get("private_confirmed")
    if not isinstance(confirmed, bool) or confirmed != (source_id is not None and container_id is not None):
        raise ExecutorError("validation_error", f"{entity}.private_confirmed must be true exactly when both write IDs are set.")
    return {"read_container_ids": read_ids, "write_source_id": source_id, "write_container_id": container_id, "private_confirmed": confirmed}


def handle_settings_set(request: dict[str, Any], store: StateStore) -> dict[str, Any]:
    strict_keys(request, {"expected_revision", "confirmed", "event_store_id", "timezone", "event", "reminder"}, "request")
    if request.get("confirmed") is not True:
        raise ExecutorError("confirmation_required", "settings set requires explicit user confirmation.")
    expected_revision = request.get("expected_revision")
    if not isinstance(expected_revision, int) or expected_revision < 0:
        raise ExecutorError("validation_error", "expected_revision must be a non-negative integer.")
    timezone = require_string(request.get("timezone"), "timezone")
    event_store_id = require_string(request.get("event_store_id"), "event_store_id")
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ExecutorError("validation_error", "timezone must be a valid IANA timezone.") from exc
    event_scope = parse_scope_input(request.get("event"), "event")
    reminder_scope = parse_scope_input(request.get("reminder"), "reminder")
    with store.locked() as (root_fd, state):
        if state["revision"] != expected_revision:
            raise ExecutorError("state_revision_conflict", "Settings changed after the preview; read them again.", {"current_revision": state["revision"]})
        previous_store_id = state.get("event_store_id")
        if isinstance(previous_store_id, str) and previous_store_id != event_store_id:
            invalidate_for_store_change(state, event_store_id)
        else:
            state["event_store_id"] = event_store_id
        state["timezone"] = timezone
        state["scopes"] = {"event": event_scope, "reminder": reminder_scope}
        state["revision"] += 1
        store.save(root_fd, state)
        return {"ok": True, "executor_version": EXECUTOR_VERSION, "command": "settings set", "revision": state["revision"], "event_store_id": event_store_id, "timezone": timezone, "scopes": state["scopes"], "mutated_eventkit": False}


def normalized_utc_timestamp(value: Any, field: str) -> str:
    text = require_string(value, field)
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError as exc:
        raise ExecutorError("validation_error", f"{field} must be RFC 3339.") from exc
    if parsed.tzinfo is None:
        raise ExecutorError("validation_error", f"{field} must include an explicit UTC offset.")
    return parsed.astimezone(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def desired_content_hash(entity: str, request: dict[str, Any]) -> str:
    payload = request.get("payload")
    if not isinstance(payload, dict):
        raise ExecutorError("validation_error", "The original request payload is missing.")
    title = require_string(payload.get("title"), "payload.title").strip()
    if entity == "reminder":
        due = payload.get("due")
        if not isinstance(due, dict):
            raise ExecutorError("validation_error", "payload.due is invalid.")
        kind = due.get("kind")
        if kind == "none":
            normalized_due: dict[str, Any] = {"kind": "none"}
        elif kind == "date":
            normalized_due = {"kind": "date", "date": require_string(due.get("date"), "payload.due.date")}
        elif kind == "date_time":
            normalized_due = {
                "kind": "date_time",
                "at": normalized_utc_timestamp(due.get("at"), "payload.due.at"),
                "timezone": require_string(due.get("timezone"), "payload.due.timezone"),
            }
        else:
            raise ExecutorError("validation_error", "payload.due.kind is invalid.")
        priority = payload.get("priority")
        if not isinstance(priority, int):
            raise ExecutorError("validation_error", "payload.priority is invalid.")
        return digest({"title": title, "due": normalized_due, "priority": priority})

    timing = payload.get("time")
    if not isinstance(timing, dict):
        raise ExecutorError("validation_error", "payload.time is invalid.")
    kind = timing.get("kind")
    if kind == "timed":
        normalized_timing: dict[str, Any] = {
            "kind": "timed",
            "start_at": normalized_utc_timestamp(timing.get("start_at"), "payload.time.start_at"),
            "end_at": normalized_utc_timestamp(timing.get("end_at"), "payload.time.end_at"),
            "timezone": require_string(timing.get("timezone"), "payload.time.timezone"),
        }
    elif kind == "all_day":
        normalized_timing = {
            "kind": "all_day",
            "start_date": require_string(timing.get("start_date"), "payload.time.start_date"),
            "end_date_exclusive": require_string(timing.get("end_date_exclusive"), "payload.time.end_date_exclusive"),
        }
    else:
        raise ExecutorError("validation_error", "payload.time.kind is invalid.")
    raw_alarms = payload.get("alarms")
    if not isinstance(raw_alarms, list):
        raise ExecutorError("validation_error", "payload.alarms is invalid.")
    minutes: list[int] = []
    for alarm in raw_alarms:
        if not isinstance(alarm, dict) or not isinstance(alarm.get("minutes_before"), int):
            raise ExecutorError("validation_error", "payload.alarms[] is invalid.")
        minutes.append(alarm["minutes_before"])
    location = payload.get("location")
    if location is not None:
        require_string(location, "payload.location")
    return digest(
        {
            "title": title,
            "location": location,
            "time": normalized_timing,
            "alarms": [{"minutes_before": value} for value in sorted(minutes)],
        }
    )


def bridge_error_code(output: dict[str, Any]) -> str | None:
    error = output.get("error")
    if not isinstance(error, dict):
        return None
    return error.get("code") if isinstance(error.get("code"), str) else None


def require_read_success_envelope(output: dict[str, Any], expected_command: str) -> None:
    if output.get("ok") is not True or output.get("command") != expected_command or output.get("mutated") is not False:
        raise ExecutorError(
            "bridge_protocol_error",
            "A reconciliation read must be an ok=true, mutated=false envelope for the exact requested command.",
        )
    require_string(output.get("event_store_id"), "reconcile.event_store_id")


def require_reconcile_item_shape(item: dict[str, Any], operation_kind: str) -> None:
    ownership = item.get("ownership")
    if not isinstance(ownership, str) or ownership not in {"personal_scheduler", "goal_planner", "unmanaged", "foreign_marker", "malformed_marker"}:
        raise ExecutorError("bridge_protocol_error", "A reconciliation item has an invalid ownership classification.")
    if not isinstance(item.get("recurring"), bool):
        raise ExecutorError("bridge_protocol_error", "A reconciliation item is missing its recurrence classification.")
    try:
        require_sha256(item.get("fingerprint"), "reconcile.item.fingerprint")
    except ExecutorError as error:
        raise ExecutorError("bridge_protocol_error", "A reconciliation item is missing a valid fingerprint.") from error
    if operation_kind in {"create", "patch", "unmanaged_patch"}:
        try:
            require_sha256(item.get("content_hash"), "reconcile.item.content_hash")
        except ExecutorError as error:
            raise ExecutorError("bridge_protocol_error", "A reconciliation item is missing a valid content_hash.") from error
    if operation_kind in {"complete", "unmanaged_complete"} and not isinstance(item.get("completed"), bool):
        raise ExecutorError("bridge_protocol_error", "A reconciliation Reminder is missing its completion state.")


def require_find_success_shape(output: dict[str, Any], expected: dict[str, Any], operation_kind: str) -> dict[str, Any] | None:
    require_read_success_envelope(output, "items find")
    count = output.get("count")
    item = output.get("item")
    if type(count) is not int or count not in {0, 1}:
        raise ExecutorError("bridge_protocol_error", "A successful items find response must have count 0 or 1.")
    if count == 0 and item is None:
        return None
    if count == 1 and isinstance(item, dict):
        managed = item.get("managed")
        allowed_roles = {"task", "deadline"} if expected.get("entity") == "reminder" else {"appointment", "commitment", "time-block"}
        if (
            item.get("entity") != expected.get("entity")
            or item.get("source_id") != expected.get("source_id")
            or item.get("container_id") != expected.get("container_id")
            or not isinstance(item.get("item_id"), str)
            or not item["item_id"]
            or item.get("ownership") != "personal_scheduler"
            or not isinstance(managed, dict)
            or managed.get("schema_version") != 1
            or managed.get("schedule_id") != expected.get("schedule_id")
            or managed.get("entity") != expected.get("entity")
            or not isinstance(managed.get("role"), str)
            or managed.get("role") not in allowed_roles
        ):
            raise ExecutorError("bridge_protocol_error", "A successful items find response does not match its exact managed lookup.")
        require_reconcile_item_shape(item, operation_kind)
        return item
    raise ExecutorError("bridge_protocol_error", "A successful items find response has an inconsistent count/item shape.")


def require_get_success_shape(output: dict[str, Any], expected: dict[str, Any], operation_kind: str) -> dict[str, Any]:
    require_read_success_envelope(output, "items get")
    item = output.get("item")
    if not isinstance(item, dict):
        raise ExecutorError("bridge_protocol_error", "A successful items get response must contain one item object.")
    if any(item.get(field) != expected.get(field) for field in ("entity", "source_id", "container_id", "item_id")):
        raise ExecutorError("bridge_protocol_error", "A successful items get response must match the exact requested locator.")
    require_reconcile_item_shape(item, operation_kind)
    return item


def require_containers_success_shape(output: dict[str, Any]) -> list[dict[str, Any]]:
    require_read_success_envelope(output, "containers list")
    containers = output.get("containers")
    if not isinstance(containers, list) or any(not isinstance(candidate, dict) for candidate in containers):
        raise ExecutorError("bridge_protocol_error", "A successful containers list response must contain an array of container objects.")
    for candidate in containers:
        string_fields = ("container_id", "source_id", "title")
        if any(not isinstance(candidate.get(field), str) or not candidate[field] for field in string_fields):
            raise ExecutorError("bridge_protocol_error", "A container candidate is missing a stable ID, source ID, or title.")
        bool_fields = ("writable", "subscribed", "immutable", "source_is_delegate")
        if any(not isinstance(candidate.get(field), bool) for field in bool_fields):
            raise ExecutorError("bridge_protocol_error", "A container candidate has invalid safety flags.")
        allowed = candidate.get("allowed_entities")
        if not isinstance(allowed, list) or any(not isinstance(entity, str) or entity not in {"event", "reminder"} for entity in allowed):
            raise ExecutorError("bridge_protocol_error", "A container candidate has invalid allowed_entities.")
    return containers


def reconciliation_protocol_unknown(operation_id: str, error: ExecutorError) -> dict[str, Any]:
    return {
        "ok": False,
        "command": "operations reconcile",
        "operation_id": operation_id,
        "resolution": "outcome_unknown",
        "cause": {"ok": False, "error": {"code": error.code, "message": error.message}},
        "mutated_eventkit": False,
    }


def reconciliation_find_request(record: dict[str, Any], original: dict[str, Any]) -> dict[str, Any]:
    schedule_id = validate_uuid_id(record.get("schedule_id"), "PS", "operation.schedule_id")
    request: dict[str, Any] = {
        "entity": record["entity"],
        "source_id": record.get("source_id"),
        "container_id": record.get("container_id"),
        "schedule_id": schedule_id,
    }
    if record["entity"] == "event":
        request["search_window"] = original.get("search_window")
    if "timeout_seconds" in original:
        request["timeout_seconds"] = original["timeout_seconds"]
    return request


def require_matching_store_epoch(state: dict[str, Any], record: dict[str, Any], output: dict[str, Any], root_fd: int, store: StateStore) -> str:
    event_store_id = require_string(output.get("event_store_id"), "reconcile.event_store_id")
    operation_store_id = require_string(record.get("event_store_id"), "operation.event_store_id")
    previous_store_id = state.get("event_store_id")
    if isinstance(previous_store_id, str) and previous_store_id != event_store_id:
        invalidate_for_store_change(state, event_store_id)
        record["error_code"] = "event_store_changed"
        state["revision"] += 1
        store.save(root_fd, state)
        raise ExecutorError("event_store_changed", "The EventKit store changed during reconciliation; scopes and locators were invalidated.")
    if operation_store_id != event_store_id:
        if record.get("error_code") != "operation_event_store_changed":
            record["error_code"] = "operation_event_store_changed"
            state["revision"] += 1
            store.save(root_fd, state)
        raise ExecutorError(
            "operation_event_store_changed",
            "This unresolved operation belongs to a different EventKit store epoch and cannot be reconciled automatically.",
        )
    state["event_store_id"] = event_store_id
    return event_store_id


def managed_matches(item: dict[str, Any], schedule_id: str, expected: dict[str, Any] | None = None) -> bool:
    managed = item.get("managed")
    basic_match = (
        item.get("ownership") == "personal_scheduler"
        and isinstance(managed, dict)
        and managed.get("schedule_id") == schedule_id
        and managed.get("entity") == item.get("entity")
    )
    if not basic_match:
        return False
    if expected is None:
        return True
    return all(managed.get(key) == expected.get(key) for key in ("schema_version", "schedule_id", "entity", "role"))


def checkpoint_reconciled_schedule(
    state: dict[str, Any],
    record: dict[str, Any],
    item: dict[str, Any] | None,
    event_store_id: str | None,
    outcome: str,
) -> None:
    schedule_id = record.get("schedule_id")
    if schedule_id is None:
        return
    if outcome == "not_applied" and record["kind"] == "claim":
        state["schedules"].pop(schedule_id, None)
        return
    if outcome == "conflict":
        schedule = state["schedules"].get(schedule_id)
        if schedule is not None:
            schedule["state"] = "conflict"
            schedule["updated_at"] = now_utc()
        return
    if item is None:
        return
    if outcome == "not_applied":
        schedule = state["schedules"].get(schedule_id)
        if schedule is not None:
            schedule.update(
                {
                    "entity": record["entity"],
                    "state": "verified_local",
                    "event_store_id": event_store_id,
                    "source_id": record.get("source_id"),
                    "container_id": record.get("container_id"),
                    "item_id": item.get("item_id"),
                    "external_id": item.get("external_id"),
                    "last_fingerprint": item.get("fingerprint"),
                    "updated_at": now_utc(),
                }
            )
        return
    state["schedules"][schedule_id] = {
        "entity": record["entity"],
        "state": "verified_local",
        "event_store_id": event_store_id,
        "source_id": record.get("source_id"),
        "container_id": record.get("container_id"),
        "item_id": item.get("item_id"),
        "external_id": item.get("external_id"),
        "intent_hash": record["intent_hash"],
        "last_fingerprint": item.get("fingerprint"),
        "updated_at": now_utc(),
    }


def handle_operation_reconcile(request: dict[str, Any], store: StateStore) -> dict[str, Any]:
    strict_keys(request, {"operation_id", "command", "original_request"}, "request")
    operation_id = require_string(request.get("operation_id"), "operation_id")
    command = require_string(request.get("command"), "command")
    original = request.get("original_request")
    if not isinstance(original, dict):
        raise ExecutorError("validation_error", "original_request must be the complete original mutation request.")
    if original.get("operation_id") != operation_id:
        raise ExecutorError("validation_error", "original_request.operation_id does not match operation_id.")
    if original.get("dry_run") is not False:
        raise ExecutorError("validation_error", "original_request must be the actual dry_run=false request.")
    require_sha256(original.get("preview_hash"), "original_request.preview_hash")

    with store.locked() as (root_fd, state):
        record = state["operations"].get(operation_id)
        if not isinstance(record, dict):
            raise ExecutorError("operation_missing", "The operation journal entry does not exist.")
        if record["phase"] not in {"in_flight", "outcome_unknown"}:
            raise ExecutorError("operation_conflict", "Only an unresolved in-flight operation can be reconciled.")
        expected_command = COMMAND_BY_KIND[record["kind"]]
        if command != expected_command or digest(public_intent(command, original)) != record["intent_hash"]:
            raise ExecutorError("operation_conflict", "The supplied original request does not match the journal intent hash.")
        operation_store_id = require_string(record.get("event_store_id"), "operation.event_store_id")
        if state.get("event_store_id") != operation_store_id:
            if record.get("error_code") != "operation_event_store_changed":
                record["error_code"] = "operation_event_store_changed"
                state["revision"] += 1
                store.save(root_fd, state)
            raise ExecutorError(
                "operation_event_store_changed",
                "This unresolved operation belongs to a different EventKit store epoch and cannot be reconciled automatically.",
            )
        entity = record["entity"]
        verify_write_scope(state, command, original, entity)

        item: dict[str, Any] | None = None
        event_store_id: str | None = None
        uniqueness_conflict = False
        claim_zero_match_rechecked = False
        bridge_output: dict[str, Any]
        if record["kind"] == "container_create":
            status, bridge_output = run_bridge("containers list", {"entity": entity, "source_id": record.get("source_id")})
            if status != 0 or bridge_output.get("ok") is not True:
                return {"ok": False, "command": "operations reconcile", "operation_id": operation_id, "resolution": "outcome_unknown", "cause": bridge_output, "mutated_eventkit": False}
            try:
                containers = require_containers_success_shape(bridge_output)
            except ExecutorError as error:
                return reconciliation_protocol_unknown(operation_id, error)
            event_store_id = require_matching_store_epoch(state, record, bridge_output, root_fd, store)
            title = require_string(original.get("title"), "original_request.title").strip()
            candidates = [candidate for candidate in containers if candidate.get("title") == title and candidate.get("source_id") == record.get("source_id")]
            if len(candidates) == 0:
                return {"ok": True, "command": "operations reconcile", "operation_id": operation_id, "resolution": "outcome_unknown", "reason": "zero_exact_container_matches", "mutated_eventkit": False}
            if len(candidates) > 1:
                outcome = "conflict"
            else:
                candidate = candidates[0]
                safe = (
                    candidate.get("writable") is True
                    and candidate.get("subscribed") is False
                    and candidate.get("immutable") is False
                    and candidate.get("source_is_delegate") is False
                    and entity in candidate.get("allowed_entities", [])
                )
                outcome = "verified_local" if safe else "conflict"
                if safe:
                    record["container_id"] = candidate.get("container_id")
        else:
            schedule_id = record.get("schedule_id")
            if record["kind"] == "create":
                find_request = reconciliation_find_request(record, original)
                status, bridge_output = run_bridge("items find", find_request)
                if status == 0 and bridge_output.get("ok") is True:
                    try:
                        item = require_find_success_shape(bridge_output, find_request, record["kind"])
                    except ExecutorError as error:
                        return reconciliation_protocol_unknown(operation_id, error)
                    event_store_id = require_matching_store_epoch(state, record, bridge_output, root_fd, store)
            else:
                get_request = {
                    "entity": entity,
                    "source_id": record.get("source_id"),
                    "container_id": record.get("container_id"),
                    "item_id": record.get("item_id"),
                }
                status, bridge_output = run_bridge("items get", get_request)
                if status == 0 and bridge_output.get("ok") is True:
                    try:
                        item = require_get_success_shape(bridge_output, get_request, record["kind"])
                    except ExecutorError as error:
                        return reconciliation_protocol_unknown(operation_id, error)
                    event_store_id = require_matching_store_epoch(state, record, bridge_output, root_fd, store)
                elif schedule_id is not None and bridge_error_code(bridge_output) == "item_missing":
                    find_request = reconciliation_find_request(record, original)
                    status, bridge_output = run_bridge("items find", find_request)
                    if status == 0 and bridge_output.get("ok") is True:
                        try:
                            item = require_find_success_shape(bridge_output, find_request, record["kind"])
                        except ExecutorError as error:
                            return reconciliation_protocol_unknown(operation_id, error)
                        event_store_id = require_matching_store_epoch(state, record, bridge_output, root_fd, store)

                if status == 0 and bridge_output.get("ok") is True and item is not None and schedule_id is not None:
                    uniqueness_request = reconciliation_find_request(record, original)
                    status, uniqueness_output = run_bridge("items find", uniqueness_request)
                    if status == 0 and uniqueness_output.get("ok") is True:
                        try:
                            unique_item = require_find_success_shape(uniqueness_output, uniqueness_request, record["kind"])
                        except ExecutorError as error:
                            return reconciliation_protocol_unknown(operation_id, error)
                        event_store_id = require_matching_store_epoch(state, record, uniqueness_output, root_fd, store)
                        if unique_item is None:
                            if record["kind"] == "claim" and item.get("ownership") == "unmanaged":
                                refresh_status, refresh_output = run_bridge("items get", get_request)
                                if refresh_status != 0 or refresh_output.get("ok") is not True:
                                    return {
                                        "ok": False,
                                        "command": "operations reconcile",
                                        "operation_id": operation_id,
                                        "resolution": "outcome_unknown",
                                        "cause": refresh_output,
                                        "mutated_eventkit": False,
                                    }
                                try:
                                    refreshed_item = require_get_success_shape(refresh_output, get_request, record["kind"])
                                except ExecutorError as error:
                                    return reconciliation_protocol_unknown(operation_id, error)
                                event_store_id = require_matching_store_epoch(state, record, refresh_output, root_fd, store)
                                item = refreshed_item
                                claim_zero_match_rechecked = True
                            else:
                                uniqueness_conflict = True
                        elif unique_item.get("item_id") != item.get("item_id"):
                            uniqueness_conflict = True
                        else:
                            item = unique_item
                    else:
                        return {"ok": False, "command": "operations reconcile", "operation_id": operation_id, "resolution": "outcome_unknown", "cause": uniqueness_output, "mutated_eventkit": False}

            if not uniqueness_conflict and (status != 0 or bridge_output.get("ok") is not True):
                return {"ok": False, "command": "operations reconcile", "operation_id": operation_id, "resolution": "outcome_unknown", "cause": bridge_output, "mutated_eventkit": False}
            if uniqueness_conflict:
                outcome = "conflict"
            elif item is None:
                return {"ok": True, "command": "operations reconcile", "operation_id": operation_id, "resolution": "outcome_unknown", "reason": "item_absent_is_not_proof", "mutated_eventkit": False}
            elif item.get("entity") != entity or item.get("source_id") != record.get("source_id") or item.get("container_id") != record.get("container_id") or item.get("recurring") is not False:
                outcome = "conflict"
            else:
                fingerprint = item.get("fingerprint")
                before = record.get("before_fingerprint")
                kind = record["kind"]
                if kind in {"delete", "unmanaged_delete"}:
                    outcome = "not_applied" if fingerprint == before else "conflict"
                elif kind == "claim":
                    expected_managed = original.get("managed") if isinstance(original.get("managed"), dict) else None
                    if claim_zero_match_rechecked:
                        outcome = "not_applied" if item.get("ownership") == "unmanaged" and fingerprint == before else "conflict"
                    elif isinstance(schedule_id, str) and expected_managed is not None and managed_matches(item, schedule_id, expected_managed):
                        outcome = "verified_local"
                    elif item.get("ownership") == "unmanaged" and fingerprint == before:
                        outcome = "not_applied"
                    else:
                        outcome = "conflict"
                elif kind in {"complete", "unmanaged_complete"}:
                    expected_ownership = "personal_scheduler" if kind == "complete" else "unmanaged"
                    identity_ok = item.get("ownership") == expected_ownership and (kind != "complete" or managed_matches(item, schedule_id))
                    if identity_ok and item.get("completed") is True:
                        outcome = "verified_local"
                    elif fingerprint == before:
                        outcome = "not_applied"
                    else:
                        outcome = "conflict"
                else:
                    expected_ownership = "personal_scheduler" if kind in {"create", "patch"} else "unmanaged"
                    expected_managed = original.get("managed") if isinstance(original.get("managed"), dict) else None
                    identity_ok = item.get("ownership") == expected_ownership and (
                        kind not in {"create", "patch"}
                        or (expected_managed is not None and managed_matches(item, schedule_id, expected_managed))
                    )
                    target_hash = desired_content_hash(entity, original)
                    if identity_ok and item.get("content_hash") == target_hash:
                        outcome = "verified_local"
                    elif kind != "create" and fingerprint == before:
                        outcome = "not_applied"
                    else:
                        outcome = "conflict"

        record["phase"] = "terminal"
        record["finished_at"] = now_utc()
        record["outcome"] = outcome
        record["error_code"] = None
        if record["kind"] != "container_create":
            checkpoint_reconciled_schedule(state, record, item, event_store_id, outcome)
        state["revision"] += 1
        store.save(root_fd, state)
        return {"ok": True, "command": "operations reconcile", "operation_id": operation_id, "resolution": outcome, "mutated_eventkit": False}


def handle_operation_resolve(request: dict[str, Any], store: StateStore) -> dict[str, Any]:
    strict_keys(request, {"operation_id", "resolution", "confirmed"}, "request")
    operation_id = require_string(request.get("operation_id"), "operation_id")
    resolution = request.get("resolution")
    if (
        not isinstance(resolution, str)
        or resolution not in {"not_applied", "conflict", "abandon_unknown"}
        or request.get("confirmed") is not True
    ):
        raise ExecutorError("confirmation_required", "A supported resolution and explicit confirmation are required.")
    with store.locked() as (root_fd, state):
        record = state["operations"].get(operation_id)
        if not isinstance(record, dict):
            raise ExecutorError("operation_missing", "The operation journal entry does not exist.")
        if record["phase"] == "prepared":
            if resolution != "not_applied":
                raise ExecutorError("resolution_forbidden", "A prepared operation can only be closed as not_applied because it never entered in_flight.")
            schedule_id = record.get("schedule_id")
            if schedule_id is not None and record["kind"] in {"create", "claim"}:
                state["schedules"].pop(schedule_id, None)
        elif record["phase"] in {"in_flight", "outcome_unknown"}:
            if resolution == "not_applied":
                raise ExecutorError("resolution_forbidden", "Only operations reconcile may prove not_applied after in_flight.")
            schedule_id = record.get("schedule_id")
            if schedule_id is not None:
                schedule = state["schedules"].get(schedule_id)
                if schedule is not None:
                    schedule["state"] = "conflict" if resolution == "conflict" else "outcome_unknown"
                    schedule["updated_at"] = now_utc()
        else:
            raise ExecutorError("operation_conflict", "Only a non-terminal operation can be resolved.")
        record["phase"] = "terminal"
        record["finished_at"] = now_utc()
        record["outcome"] = resolution
        record["error_code"] = None
        state["revision"] += 1
        store.save(root_fd, state)
        return {"ok": True, "executor_version": EXECUTOR_VERSION, "command": "operations resolve", "operation_id": operation_id, "resolution": resolution, "mutated_eventkit": False}


def state_self_test() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="personal-scheduler-state-test-") as directory:
        root = Path(directory) / "state"
        root.mkdir(mode=0o700)
        store = StateStore(root_override=root)
        with store.locked() as (root_fd, state):
            state["timezone"] = "Asia/Shanghai"
            state["revision"] = 1
            store.save(root_fd, state)
        exists, loaded = store.peek()
        if not exists or loaded is None or loaded["timezone"] != "Asia/Shanghai" or stat.S_IMODE((root / STATE_FILE_NAME).stat().st_mode) != 0o600:
            raise ExecutorError("self_test_failed", "Secure state round-trip failed.")
        sentinel = Path(directory) / "sentinel"
        sentinel.write_text("unchanged", encoding="utf-8")
        (root / STATE_FILE_NAME).unlink()
        (root / STATE_FILE_NAME).symlink_to(sentinel)
        try:
            with store.locked():
                pass
        except ExecutorError as error:
            if error.code != "unsafe_state":
                raise
        else:
            raise ExecutorError("self_test_failed", "A symlink state file was not rejected.")
        if sentinel.read_text(encoding="utf-8") != "unchanged":
            raise ExecutorError("self_test_failed", "The symlink sentinel changed.")
    return {"name": "secure_state_round_trip_and_symlink_rejection", "passed": True}


def emit(value: dict[str, Any], status: int = 0) -> None:
    value.setdefault("executor_version", EXECUTOR_VERSION)
    value.setdefault("state_schema_version", STATE_SCHEMA_VERSION)
    sys.stdout.write(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    raise SystemExit(status)


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("command", nargs="*")
    args = parser.parse_args()
    command = " ".join(args.command)
    store = StateStore()

    if command in {"doctor", "status"}:
        _, bridge = run_bridge("doctor")
        exists, state = store.peek()
        bridge["executor_version"] = EXECUTOR_VERSION
        bridge["state_schema_version"] = STATE_SCHEMA_VERSION
        bridge["state"] = {"path": store.display_path, "exists": exists, "revision": state["revision"] if state else None, "production_state_accessed": exists}
        emit(bridge, 0 if bridge.get("ok") else 2)
    if command == "self-test":
        status, bridge = run_bridge("self-test")
        if status != 0 or bridge.get("ok") is not True:
            emit(bridge, 2)
        state_test = state_self_test()
        emit({"ok": True, "command": "self-test", "bridge": bridge, "tests": [state_test], "eventkit_data_accessed": False, "production_state_accessed": False, "mutated": False})
    if command == "id new":
        request = read_stdin()
        strict_keys(request, {"kind"}, "request")
        kind = request.get("kind")
        prefixes = {"schedule": "PS", "operation": "OP", "container_operation": "COP"}
        if not isinstance(kind, str) or kind not in prefixes:
            raise ExecutorError("validation_error", "kind must be schedule, operation, or container_operation.")
        emit({"ok": True, "command": "id new", "kind": kind, "id": prefixes[kind] + "-" + str(uuid.uuid4()).upper(), "mutated": False})
    if command in {"settings get", "state get", "operations list"}:
        request = read_stdin(required=False)
        strict_keys(request, set(), "request")
        with store.locked() as (_, state):
            if command == "settings get":
                result = {"revision": state["revision"], "event_store_id": state["event_store_id"], "timezone": state["timezone"], "scopes": state["scopes"]}
            elif command == "operations list":
                result = {"revision": state["revision"], "operations": state["operations"]}
            else:
                result = state
        emit({"ok": True, "command": command, "state": result, "mutated": False})
    if command == "settings set":
        emit(handle_settings_set(read_stdin(), store))
    if command == "operations resolve":
        emit(handle_operation_resolve(read_stdin(), store))
    if command == "operations reconcile":
        emit(handle_operation_reconcile(read_stdin(), store))
    if command in READ_COMMANDS:
        request = read_stdin()
        if command in {"availability", "events list", "reminders list", "items find", "items get"}:
            with store.locked() as (root_fd, state):
                verify_read_scope(state, command, request)
                status, output = run_bridge(command, request)
                output_store_id = output.get("event_store_id") if isinstance(output, dict) else None
                if status == 0 and output.get("ok") is True and isinstance(output_store_id, str) and output_store_id:
                    previous_store_id = state.get("event_store_id")
                    if isinstance(previous_store_id, str) and previous_store_id != output_store_id:
                        invalidate_for_store_change(state, output_store_id)
                        state["revision"] += 1
                        store.save(root_fd, state)
                        raise ExecutorError("event_store_changed", "The EventKit store identity changed; saved scopes and locators were invalidated.")
                    if previous_store_id is None:
                        state["event_store_id"] = output_store_id
                        state["revision"] += 1
                        store.save(root_fd, state)
        else:
            status, output = run_bridge(command, request)
        emit(output, 0 if status == 0 and output.get("ok") else 2)
    if command in MUTATION_COMMANDS:
        output = handle_mutation(command, read_stdin(), store)
        emit(output, 0 if output.get("ok") else 2)
    raise ExecutorError("unknown_command", "Unknown command.", {"received": command})


if __name__ == "__main__":
    try:
        main()
    except ExecutorError as error:
        error_object: dict[str, Any] = {"code": error.code, "message": error.message}
        if error.details:
            error_object["details"] = error.details
        emit({"ok": False, "error": error_object}, 2)
