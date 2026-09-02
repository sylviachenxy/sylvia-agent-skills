#!/usr/bin/env python3
"""Bounded, privacy-preserving transport for the bundled read-only EventKit app."""

from __future__ import annotations

import datetime as dt
import json
import math
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parent
MAX_INPUT = 1_048_576
MAX_OUTPUT = 4_194_304
MAX_WINDOW_DAYS = 100
COMMANDS = {"capabilities", "doctor", "self-test", "setup authorize", "setup containers list", "events list", "reminders list"}
OFFLINE = {"capabilities", "doctor", "self-test"}
TOKEN = "morning-brief-eventkit-read-v1-B89E3F52"
COMMON = {"ok", "reader_version", "protocol_version", "eventkit_data_mutated", "command", "eventkit_data_accessed"}
PERSONAL = {"item_id", "title", "created_at", "last_modified_at", "managed", "managed_status"}
EVENT = PERSONAL | {"calendar_id", "start_at", "end_at", "all_day", "timezone", "status", "availability", "recurring", "detached", "occurrence_start_at", "original_occurrence_at", "start_date", "end_date_exclusive", "date_timezone", "date_timezone_inferred_from_request"}
REMINDER = PERSONAL | {"list_id", "due_date", "due_at", "due", "completed", "completion_at", "priority", "recurring", "current_instance_only"}


class TransportError(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message


def failure(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "reader_version": "1.0.0", "protocol_version": 1, "eventkit_data_mutated": False,
            "error": {"code": code, "message": message}}


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def finite_number(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("nonfinite number")
    return parsed


def reject_constant(_: str) -> None:
    raise ValueError("nonfinite constant")


def strict_json(raw: bytes) -> dict[str, Any]:
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object, parse_float=finite_number, parse_constant=reject_constant)
    if not isinstance(value, dict):
        raise ValueError("object required")
    return value


def nonblank(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value.encode("utf-8")) <= 4096 and "\0" not in value


def timestamp(value: Any) -> dt.datetime:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value):
        raise ValueError("offset timestamp required")
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def validate_input(command: str, value: dict[str, Any]) -> None:
    if command.startswith("setup "):
        allowed = {"entity", "confirmed"} | ({"timeout_seconds"} if command == "setup authorize" else set())
        if set(value) - allowed or value.get("entity") not in {"event", "reminder"} or value.get("confirmed") is not True:
            raise ValueError("explicit setup confirmation required")
        timeout = value.get("timeout_seconds", 120)
        if type(timeout) is not int or not 30 <= timeout <= 180:
            raise ValueError("invalid timeout")
        return
    kind = "calendar_ids" if command == "events list" else "list_ids"
    allowed = {kind, "window", "timezone", "limit", "include_goal_links"}
    if command == "reminders list":
        allowed |= {"include_undated", "timeout_seconds"}
    if set(value) - allowed:
        raise ValueError("unknown request field")
    ids = value.get(kind)
    if not isinstance(ids, list) or not 1 <= len(ids) <= 50 or not all(nonblank(x) for x in ids) or len(set(ids)) != len(ids):
        raise ValueError("explicit container allowlist required")
    window = value.get("window")
    if not isinstance(window, dict) or set(window) != {"start_at", "end_at"}:
        raise ValueError("explicit window required")
    start, end = timestamp(window["start_at"]), timestamp(window["end_at"])
    if not 0 < (end - start).total_seconds() <= MAX_WINDOW_DAYS * 86400:
        raise ValueError("source window exceeds bounds")
    if not nonblank(value.get("timezone")):
        raise ValueError("timezone required")
    if type(value.get("limit")) is not int or not 1 <= value["limit"] <= 500:
        raise ValueError("result limit required")
    if type(value.get("include_goal_links", False)) is not bool:
        raise ValueError("goal metadata flag must be boolean")
    if command == "reminders list":
        if type(value.get("include_undated")) is not bool:
            raise ValueError("undated inclusion must be explicit")
        timeout = value.get("timeout_seconds", 20)
        if type(timeout) is not int or not 5 <= timeout <= 60:
            raise ValueError("invalid timeout")


def validate_item(item: Any, command: str) -> None:
    allowed = EVENT if command == "events list" else REMINDER
    if not isinstance(item, dict) or set(item) - allowed or not nonblank(item.get("item_id")):
        raise ValueError("invalid native item")
    optional = {"start_date", "end_date_exclusive", "date_timezone", "date_timezone_inferred_from_request"} if command == "events list" else set()
    if not (allowed - optional).issubset(item):
        raise ValueError("incomplete native item")
    title = item.get("title")
    if not isinstance(title, str) or len(title.encode("utf-8")) > 4096 or "\0" in title:
        raise ValueError("invalid title")
    for key in ("created_at", "last_modified_at"):
        if item[key] is not None:
            timestamp(item[key])
    managed = item.get("managed")
    if managed is not None:
        if not isinstance(managed, dict) or set(managed) != {"schema_version", "goal_id", "action_id", "projection_id"} or type(managed["schema_version"]) is not int or managed["schema_version"] != 2:
            raise ValueError("unsafe metadata projection")
        goal = managed["goal_id"]
        if not isinstance(goal, str) or not re.fullmatch(r"G-\d{4}-\d{3}", goal):
            raise ValueError("invalid goal")
        suffix = "E" if command == "events list" else "R"
        if not re.fullmatch(re.escape(goal) + "-" + suffix + r"\d{3}", managed["projection_id"]):
            raise ValueError("invalid projection")
        action = managed["action_id"]
        if action is not None and (not isinstance(action, str) or not re.fullmatch(re.escape(goal) + r"-A\d{3}", action)):
            raise ValueError("invalid action")
    if item.get("managed_status") not in {"not_requested", "absent", "valid", "malformed", "unsupported", "oversized"}:
        raise ValueError("invalid metadata status")
    if (item["managed_status"] == "valid") != (managed is not None):
        raise ValueError("metadata status mismatch")
    if command == "events list":
        start, end = timestamp(item.get("start_at")), timestamp(item.get("end_at"))
        if start > end or timestamp(item.get("occurrence_start_at")) != start:
            raise ValueError("invalid occurrence timing")
        if item["original_occurrence_at"] is not None:
            timestamp(item["original_occurrence_at"])
        if type(item.get("all_day")) is not bool or not nonblank(item.get("calendar_id")):
            raise ValueError("invalid event")
        if item["timezone"] is not None and not nonblank(item["timezone"]):
            raise ValueError("invalid native timezone")
        if item["status"] not in {"none", "confirmed", "tentative", "canceled", "unknown"} or item["availability"] not in {"busy", "free", "tentative", "unavailable", "not_supported", "unknown"}:
            raise ValueError("invalid native status")
        if any(type(item[k]) is not bool for k in ("recurring", "detached")):
            raise ValueError("invalid native recurrence")
        if item["all_day"]:
            if not optional.issubset(item) or dt.date.fromisoformat(item["start_date"]) >= dt.date.fromisoformat(item["end_date_exclusive"]):
                raise ValueError("invalid all-day exclusive range")
            if not nonblank(item["date_timezone"]) or type(item["date_timezone_inferred_from_request"]) is not bool:
                raise ValueError("invalid all-day timezone")
        elif optional.intersection(item):
            raise ValueError("timed event has all-day fields")
    else:
        if item.get("completed") is not False or item.get("current_instance_only") is not True or not nonblank(item.get("list_id")):
            raise ValueError("invalid reminder")
        if item["completion_at"] is not None:
            timestamp(item["completion_at"])
        if item.get("due_date") is not None:
            dt.date.fromisoformat(item["due_date"])
        if item.get("due_at") is not None:
            timestamp(item["due_at"])
        if item.get("due_date") is not None and item.get("due_at") is not None:
            raise ValueError("date-only and timed due cannot coexist")
        due = item.get("due")
        if due is not None and (not isinstance(due, dict) or set(due) - {"kind", "timezone", "effective_timezone", "timezone_inferred_from_request", "year", "month", "day", "hour", "minute", "second"}):
            raise ValueError("unsafe due fields")
        if type(item["priority"]) is not int or not 0 <= item["priority"] <= 9 or type(item["recurring"]) is not bool:
            raise ValueError("invalid reminder priority/recurrence")
        if due is None:
            if item["due_date"] is not None or item["due_at"] is not None:
                raise ValueError("undated reminder has a deadline")
        else:
            if not {"kind", "timezone", "effective_timezone", "timezone_inferred_from_request", "year", "month", "day"}.issubset(due):
                raise ValueError("missing due components")
            if due["kind"] not in {"date", "date_time"} or type(due["timezone_inferred_from_request"]) is not bool:
                raise ValueError("invalid date semantics")
            if (due["kind"] == "date") != (item["due_date"] is not None) or (due["kind"] == "date_time") != (item["due_at"] is not None):
                raise ValueError("due components and normalized deadline differ")
            if not nonblank(due["effective_timezone"]) or (due["timezone"] is not None and not nonblank(due["timezone"])):
                raise ValueError("invalid due timezone")
            for key in ("year", "month", "day", "hour", "minute", "second"):
                if key in due and type(due[key]) is not int:
                    raise ValueError("invalid due component type")
            civil = dt.date(due["year"], due["month"], due["day"])
            if due["kind"] == "date" and (civil.isoformat() != item["due_date"] or {"hour", "minute", "second"}.intersection(due)):
                raise ValueError("inconsistent civil due date")


def validate_output(value: dict[str, Any], status: int, command: str, request: dict[str, Any] | None = None) -> int:
    if value.get("reader_version") != "1.0.0" or type(value.get("protocol_version")) is not int or value["protocol_version"] != 1 or value.get("eventkit_data_mutated") is not False:
        raise ValueError("native protocol mismatch")
    if value.get("ok") is False:
        error = value.get("error")
        if status != 2 or set(value) - {"ok", "reader_version", "protocol_version", "eventkit_data_mutated", "error"} or not isinstance(error, dict) or set(error) != {"code", "message"} or not all(nonblank(error.get(k)) for k in ("code", "message")):
            raise ValueError("invalid error envelope")
        return 2
    if value.get("ok") is not True or status != 0 or value.get("command") != command:
        raise ValueError("native exit/envelope mismatch")
    if command in OFFLINE:
        extra = {"commands", "access"} if command == "capabilities" else {"bundle_identity_matches", "minimum_macos", "permissions", "signature_kind", "rebuild_may_require_reauthorization", "native_store_initialized"} if command == "doctor" else {"tests", "native_store_initialized", "production_state_accessed"}
        if set(value) - (COMMON | extra) or value.get("eventkit_data_accessed") is not False:
            raise ValueError("offline operation accessed native data")
        return 0
    if command == "setup authorize":
        if set(value) - (COMMON | {"entity", "status", "prompted", "permission_state_changed"}) or value.get("eventkit_data_accessed") is not False:
            raise ValueError("unsafe authorization envelope")
        return 0
    if command == "setup containers list":
        if set(value) - (COMMON | {"entity", "event_store_id", "scope", "containers"}):
            raise ValueError("unsafe metadata envelope")
        containers = value.get("containers")
        if not isinstance(containers, list) or len(containers) > 1000:
            raise ValueError("invalid containers")
        for container in containers:
            if not isinstance(container, dict) or set(container) != {"container_id", "title", "source_id", "source_title"}:
                raise ValueError("unsafe container fields")
            if not nonblank(container["container_id"]) or not nonblank(container["source_id"]) or any(not isinstance(container[k], str) or len(container[k].encode("utf-8")) > 4096 for k in ("title", "source_title")):
                raise ValueError("invalid container metadata type")
        if value.get("scope") != {"metadata_only": True, "all_containers_for_entity": True, "item_contents_read": False}:
            raise ValueError("unsafe metadata scope")
        return 0
    extra = {"event_store_id", "coverage", "as_of", "collected_through", "query_window", "timezone", "scope", "result_count", "matched_count", "limit", "truncated", "truncated_reason", "error", "items"}
    if set(value) != COMMON | extra:
        raise ValueError("unsafe source envelope")
    items = value["items"]
    if not isinstance(items, list) or type(value["limit"]) is not int or not 1 <= value["limit"] <= 500 or len(items) > value["limit"] or type(value["result_count"]) is not int or value["result_count"] != len(items) or type(value["matched_count"]) is not int or value["matched_count"] < len(items):
        raise ValueError("invalid result counts")
    if len(items) != min(value["matched_count"], value["limit"]):
        raise ValueError("native items missing before the result limit")
    truncated = value["matched_count"] > value["limit"]
    if value["truncated"] is not truncated or value["coverage"] != ("partial" if truncated else "complete") or value["truncated_reason"] != ("result_limit" if truncated else None) or value["error"] is not None:
        raise ValueError("invalid coverage")
    timestamp(value["as_of"]); timestamp(value["collected_through"])
    if not nonblank(value["event_store_id"]) or value["eventkit_data_accessed"] is not True:
        raise ValueError("invalid source identity")
    scope = value["scope"]
    scope_allowed = {"calendar_ids", "backend_query_window", "candidate_mode", "include_goal_links", "notes_exported", "cancelled_and_free_included"} if command == "events list" else {"list_ids", "backend_query_window", "candidate_mode", "candidate_count", "include_undated", "include_goal_links", "completed_included", "notes_exported", "sort"}
    if not isinstance(scope, dict) or set(scope) != scope_allowed or scope.get("notes_exported") is not False:
        raise ValueError("unsafe scope")
    if type(scope["include_goal_links"]) is not bool:
        raise ValueError("invalid goal metadata scope")
    if command == "events list":
        if scope["candidate_mode"] != "events_overlapping_window" or scope["cancelled_and_free_included"] is not True:
            raise ValueError("invalid event scope")
    else:
        if type(scope["include_undated"]) is not bool or scope["completed_included"] is not False or type(scope["candidate_count"]) is not int or scope["candidate_count"] < value["matched_count"]:
            raise ValueError("invalid reminder scope")
        mode = "all_incomplete_in_selected_lists" if scope["include_undated"] else "incomplete_due_with_civil_day_timezone_guard"
        if scope["candidate_mode"] != mode or scope["sort"] != "due_instant_or_end_of_civil_due_day_then_id; undated_last":
            raise ValueError("invalid reminder query semantics")
    for name, window in (("query_window", value["query_window"]), ("backend_query_window", scope["backend_query_window"])):
        if name == "backend_query_window" and window is None and command == "reminders list" and scope["include_undated"] is True:
            continue
        if not isinstance(window, dict) or set(window) != {"start_at", "end_at"} or timestamp(window["start_at"]) >= timestamp(window["end_at"]):
            raise ValueError("unsafe output window")
    id_key = "calendar_ids" if command == "events list" else "list_ids"
    ids = scope[id_key]
    if not isinstance(ids, list) or not 1 <= len(ids) <= 50 or not all(nonblank(x) for x in ids) or len(set(ids)) != len(ids):
        raise ValueError("unsafe output allowlist")
    if request is not None:
        if ids != request[id_key] or value["limit"] != request["limit"] or value["timezone"] != request["timezone"] or scope["include_goal_links"] is not request.get("include_goal_links", False):
            raise ValueError("native query scope differs from request")
        if any(timestamp(value["query_window"][k]) != timestamp(request["window"][k]) for k in ("start_at", "end_at")):
            raise ValueError("native query window differs from request")
        if command == "reminders list" and scope["include_undated"] is not request["include_undated"]:
            raise ValueError("native undated scope differs from request")
    for item in items:
        validate_item(item, command)
        if item["calendar_id" if command == "events list" else "list_id"] not in ids:
            raise ValueError("item outside selected containers")
        if scope["include_goal_links"] is False and item["managed_status"] != "not_requested":
            raise ValueError("unrequested goal metadata")
        if command == "reminders list" and scope["include_undated"] is False and item["due"] is None:
            raise ValueError("unrequested undated reminder")
    return 0


def bounded_run(args: list[str], raw: bytes, timeout: float, env: dict[str, str]) -> tuple[bytes, int]:
    """Pipe in-memory only; never preserve private stdout/stderr in runtime files."""
    proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, start_new_session=True)
    selector = selectors.DefaultSelector()
    output = bytearray()
    offset, deadline = 0, time.monotonic() + timeout
    try:
        for stream in (proc.stdin, proc.stdout):
            os.set_blocking(stream.fileno(), False)
        selector.register(proc.stdout, selectors.EVENT_READ, "read")
        if raw:
            selector.register(proc.stdin, selectors.EVENT_WRITE, "write")
        else:
            proc.stdin.close()
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TransportError("operation_timeout", "The read-only operation exceeded its watchdog and was terminated.")
            for key, _ in selector.select(min(remaining, 0.2)):
                if key.data == "write":
                    try:
                        offset += os.write(key.fd, raw[offset:offset + 65536])
                    except BrokenPipeError:
                        offset = len(raw)
                    if offset == len(raw):
                        selector.unregister(key.fileobj); key.fileobj.close()
                else:
                    chunk = os.read(key.fd, 65536)
                    output.extend(chunk)
                    if len(output) > MAX_OUTPUT:
                        raise TransportError("output_limit_exceeded", "The native output exceeded the 4 MiB protocol limit.")
                    if not chunk:
                        selector.unregister(key.fileobj); key.fileobj.close()
        try:
            status = proc.wait(timeout=max(0.01, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise TransportError("operation_timeout", "The read-only operation exceeded its watchdog and was terminated.") from None
        return bytes(output), status
    finally:
        selector.close()
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        for stream in (proc.stdin, proc.stdout):
            if not stream.closed:
                stream.close()


def main() -> int:
    os.umask(0o077)
    try:
        command = " ".join(sys.argv[1:])
        if command not in COMMANDS:
            raise TransportError("unknown_command", "Only documented read-only and explicit setup commands are supported.")
        raw = b""
        value = {}
        if command not in OFFLINE:
            raw = sys.stdin.buffer.read(MAX_INPUT + 1)
            if len(raw) > MAX_INPUT:
                raise TransportError("input_limit_exceeded", "The request exceeds the 1 MiB input limit.")
            try:
                value = strict_json(raw); validate_input(command, value)
            except (ValueError, TypeError, KeyError, OverflowError, UnicodeError, RecursionError):
                raise TransportError("validation_error", "The request is invalid; use the documented explicit scope and field types.") from None
        if sys.platform != "darwin":
            raise TransportError("unsupported_platform", "The native source adapter requires macOS 14 or later.")
        try:
            _, build_status = bounded_run(["/bin/zsh", str(ROOT / "build.sh")], b"", 120, dict(os.environ))
        except TransportError:
            raise TransportError("reader_build_failed", "The native reader build exceeded its time limit.") from None
        if build_status:
            raise TransportError("reader_build_failed", "The native reader could not be built or verified; build details were suppressed.")
        binary = ROOT / ".build/MorningBriefEventKitReader.app/Contents/MacOS/morning-brief-eventkit-reader"
        env = dict(os.environ, MORNING_BRIEF_EVENTKIT_INTERNAL=TOKEN)
        timeout = (value.get("timeout_seconds", 120) + 10) if command == "setup authorize" else (value.get("timeout_seconds", 20) + 10) if command == "reminders list" else 30
        output, status = bounded_run([str(binary), *sys.argv[1:]], raw, timeout, env)
        try:
            result = strict_json(output); final_status = validate_output(result, status, command, value)
        except (ValueError, TypeError, KeyError, OverflowError, UnicodeError, RecursionError):
            raise TransportError("reader_protocol_error", "The native reader returned malformed, unsafe or inconsistent protocol output.") from None
        print(json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True))
        return final_status
    except TransportError as error:
        print(json.dumps(failure(error.code, error.message), separators=(",", ":")))
        return 2
    except Exception:
        # Includes OS/subprocess exceptions: paths or native/private values are not surfaced.
        print(json.dumps(failure("transport_error", "The read-only transport failed; private diagnostic details were suppressed."), separators=(",", ":")))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
