"""手机消费协议的纯离线 Python 参考，不是可安装到 iPhone 的快捷指令。

唯一公开入口 select_note(config_id, note_bodies, now)。接收方只固定稳定
config_id；note_bodies 必须已经由调用方限制到获准 Notes 范围。now 是显式
带偏移 ISO 时间或 aware datetime。无文件、配置、网络、设备或系统时钟读取。

返回安全的选择结果和元数据，不返回正文/标题/来源。READY 只说明实际收到
的快照可用；not_latest_verified 永远为 True，不证明 Mac 的最新版本已同步。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import re
import unicodedata


PROTOCOL_VERSION = 2
MAX_NOTES = 1000
MAX_BODY_BYTES = 1_048_576
MAX_TOTAL_BYTES = 16 * MAX_BODY_BYTES
MAX_SAFE_INTEGER = 9_007_199_254_740_991
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:Z|[+-][0-9]{2}:[0-9]{2})\Z")
_ZONE = re.compile(r"(?:UTC|[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)+)\Z")
_WRAPPER = re.compile(
    r"\A(?:[^\n]*\n)?MB:BEGIN\nMB:CONTENT-BEGIN\n(.*?)"
    r"\nMB:CONTENT-END\nMB:CONTENT-SHA256=([0-9a-f]{64})\nMB:END\n?\Z", re.S)
_FIELDS = frozenset(("SCHEMA", "CONFIG", "CONFIG-REVISION", "DATE", "TIMEZONE",
                     "BRIEF", "REVISION", "STATUS", "GENERATED", "FRESH-UNTIL",
                     "VALID-FROM", "VALID-UNTIL"))
_LEGACY_FIELDS = _FIELDS - {"VALID-FROM", "VALID-UNTIL"}


class _Invalid(ValueError):
    """仅使用固定错误码，不能带入私人输入。"""


def _timestamp(value):
    if not isinstance(value, str) or not _STAMP.fullmatch(value) or value.endswith("-00:00"):
        raise _Invalid("INVALID_TIMESTAMP")
    if not value.endswith("Z"):
        hours, minutes = int(value[-5:-3]), int(value[-2:])
        if hours > 14 or minutes > 59 or (hours == 14 and minutes):
            raise _Invalid("INVALID_TIMESTAMP")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        result.astimezone(timezone.utc)  # Reject conversion overflow as well.
        return result
    except (ValueError, OverflowError):
        raise _Invalid("INVALID_TIMESTAMP") from None


def _positive_integer(value):
    if not re.fullmatch(r"[1-9][0-9]{0,15}", value):
        raise _Invalid("INVALID_VERSION")
    result = int(value)
    if result > MAX_SAFE_INTEGER:
        raise _Invalid("INVALID_VERSION")
    return result


def _normalize(body):
    if not isinstance(body, str):
        raise _Invalid("INVALID_BODY")
    try:
        if len(body.encode("utf-8")) > MAX_BODY_BYTES:
            raise _Invalid("BODY_TOO_LARGE")
    except UnicodeError:
        raise _Invalid("INVALID_BODY") from None
    # Only equivalent transport forms; never strip spaces/paragraphs or content.
    normalized = unicodedata.normalize("NFC", body.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " "))
    if any((ch != "\n" and unicodedata.category(ch) in {"Cc", "Cf", "Cs"}) or
           ch in "\u2028\u2029" for ch in normalized):
        raise _Invalid("INVALID_BODY")
    return normalized


def _profile_hints(body):
    """Untrusted routing hints only, never sufficient to accept a candidate."""
    if not isinstance(body, str) or len(body) > MAX_BODY_BYTES:
        return set()
    return set(re.findall(r"^MB:CONFIG=([^\r\n]+)$", body.replace("\r\n", "\n").replace("\r", "\n"), re.M))


def _decode(body):
    normalized = _normalize(body)
    match = _WRAPPER.fullmatch(normalized)
    if match is None:
        raise _Invalid("INVALID_WRAPPER")
    content, checksum = match.groups()
    if hashlib.sha256(content.encode("utf-8")).hexdigest() != checksum:
        raise _Invalid("CONTENT_HASH_MISMATCH")
    fields = {}
    for line in content.split("\n"):
        if "MB:" not in line.upper():
            continue
        marker = re.fullmatch(r"MB:([A-Z-]+)=([^\n]*)", line)
        if marker is None or marker[1] not in _FIELDS or marker[1] in fields:
            raise _Invalid("INVALID_METADATA")
        fields[marker[1]] = marker[2]
    if fields.get("SCHEMA") == "1" and set(fields) == _LEGACY_FIELDS:
        return {"legacy": True, "config_id": fields.get("CONFIG")}
    if fields.get("SCHEMA") != str(PROTOCOL_VERSION):
        raise _Invalid("UNSUPPORTED_SCHEMA")
    if set(fields) != _FIELDS:
        raise _Invalid("INVALID_METADATA")
    if not _ID.fullmatch(fields["CONFIG"]) or not _ID.fullmatch(fields["BRIEF"]):
        raise _Invalid("INVALID_IDENTITY")
    if len(fields["TIMEZONE"]) > 100 or not _ZONE.fullmatch(fields["TIMEZONE"]):
        raise _Invalid("INVALID_TIMEZONE_LABEL")
    if fields["STATUS"] not in ("READY", "PARTIAL"):
        raise _Invalid("INVALID_STATUS")
    if not _DATE.fullmatch(fields["DATE"]):
        raise _Invalid("INVALID_DATE")
    try:
        applicable = date.fromisoformat(fields["DATE"])
        tomorrow = applicable + timedelta(days=1)
    except (ValueError, OverflowError):
        raise _Invalid("INVALID_DATE") from None
    stamps = {key: _timestamp(fields[key]) for key in ("GENERATED", "FRESH-UNTIL", "VALID-FROM", "VALID-UNTIL")}
    start, end = stamps["VALID-FROM"], stamps["VALID-UNTIL"]
    # Offset-bearing civil midnights are producer data, not the device's date.
    if (start.date() != applicable or end.date() != tomorrow or
            any((value.hour, value.minute, value.second) != (0, 0, 0) for value in (start, end)) or
            not start < end):
        raise _Invalid("INVALID_VALIDITY")
    if not start <= stamps["GENERATED"] < end:
        raise _Invalid("GENERATED_OUTSIDE_VALIDITY")
    # A stale source can honestly put FRESH-UNTIL before generation or midnight.
    if stamps["FRESH-UNTIL"] > end:
        raise _Invalid("FRESHNESS_OUTSIDE_VALIDITY")
    metadata = {"schema_version": PROTOCOL_VERSION, "config_id": fields["CONFIG"],
                "config_revision": _positive_integer(fields["CONFIG-REVISION"]),
                "applicable_date": fields["DATE"], "timezone": fields["TIMEZONE"],
                "brief_id": fields["BRIEF"], "revision": _positive_integer(fields["REVISION"]),
                "generated_status": fields["STATUS"], "generated_at": fields["GENERATED"],
                "fresh_until": fields["FRESH-UNTIL"], "valid_from": fields["VALID-FROM"],
                "valid_until": fields["VALID-UNTIL"], "content_sha256": checksum}
    return {"metadata": metadata, "stamps": stamps, "config_id": fields["CONFIG"], "legacy": False}


def _result(status, *, selected=None, rejected=None, ignored=None, error=None):
    rejected = rejected or []
    result = {"status": status, "selected_index": None, "metadata": None,
              "fallback": bool(selected and any(item["target_profile"] for item in rejected)),
              "not_latest_verified": True,
              "rejected": [{"index": item["index"], "code": item["code"]} for item in rejected],
              "ignored": ignored or {}}
    if selected is not None:
        result.update(selected_index=selected["index"], metadata=selected["metadata"])
    if error is not None:
        result["error"] = error
    return result


def select_note(config_id, note_bodies, now):
    """Select a received protocol-2 snapshot without any Mac config dependency.

    Order currently applicable, hash-verified notes by (config_revision, revision).
    Duplicate current logical versions are READ_ERROR, even with equal bodies or
    differing brief IDs. Historical/future-day/other-profile/legacy notes are
    ignored, never selected. A current note generated in the future is rejected.
    A valid fallback can stay READY/PARTIAL, with fallback=True only when a
    rejected candidate names this receiver's config_id. Unknown malformed inputs
    are reported without inventing a profile. No accepted note plus malformed
    candidates yields READ_ERROR; only ignored/empty candidates yields NOT_READY.

    TIMEZONE is a bounded IANA-shaped display label; validity uses the embedded
    ISO offsets. Neither the device timezone nor a local tzdata table is used.
    """
    try:
        if not isinstance(config_id, str) or not _ID.fullmatch(config_id):
            raise _Invalid("INVALID_CONFIG_ID")
        if not isinstance(note_bodies, list) or len(note_bodies) > MAX_NOTES:
            raise _Invalid("INVALID_CANDIDATES")
        if isinstance(now, str):
            current = _timestamp(now)
        elif isinstance(now, datetime) and now.tzinfo is not None and now.utcoffset() is not None:
            current = now.astimezone(timezone.utc)
        else:
            raise _Invalid("INVALID_NOW")
        total = sum(len(body.encode("utf-8")) for body in note_bodies if isinstance(body, str))
        if total > MAX_TOTAL_BYTES:
            raise _Invalid("CANDIDATES_TOO_LARGE")
    except (_Invalid, UnicodeError, ValueError, OverflowError) as error:
        code = str(error) if isinstance(error, _Invalid) else "INVALID_INPUT"
        return _result("READ_ERROR", error=code)
    accepted, rejected, ignored = [], [], {}
    for index, body in enumerate(note_bodies):
        hints = _profile_hints(body)
        try:
            item = _decode(body)
            reason = None
            if item["config_id"] != config_id:
                reason = "other_profile"
            elif item["legacy"]:
                reason = "legacy_protocol"
            elif current < item["stamps"]["VALID-FROM"]:
                reason = "future_day"
            elif current >= item["stamps"]["VALID-UNTIL"]:
                reason = "historical"
            elif item["stamps"]["GENERATED"] > current:
                raise _Invalid("FUTURE_GENERATED")
            if reason is not None:
                ignored[reason] = ignored.get(reason, 0) + 1
                continue
            item["index"] = index
            accepted.append(item)
        except _Invalid as error:
            if hints and config_id not in hints:
                ignored["other_profile"] = ignored.get("other_profile", 0) + 1
                continue
            rejected.append({"index": index, "code": str(error), "target_profile": config_id in hints})
    versions = set()
    for item in accepted:
        meta = item["metadata"]
        version = (meta["config_revision"], meta["revision"])
        if version in versions:
            return _result("READ_ERROR", rejected=rejected, ignored=ignored, error="DUPLICATE_VERSION")
        versions.add(version)
    if not accepted:
        return _result("READ_ERROR" if rejected else "NOT_READY", rejected=rejected, ignored=ignored)
    selected = max(accepted, key=lambda item: (item["metadata"]["config_revision"], item["metadata"]["revision"]))
    status = selected["metadata"]["generated_status"]
    if current > selected["stamps"]["FRESH-UNTIL"]:
        status = "PARTIAL"
    return _result(status, selected=selected, rejected=rejected, ignored=ignored)
