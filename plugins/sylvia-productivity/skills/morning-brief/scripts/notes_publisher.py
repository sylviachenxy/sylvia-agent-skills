#!/usr/bin/env python3
"""Scoped, immutable Notes publishing; Python stdlib and native AppleScriptObjC.

Public CLI stdout contains only status/locators, never note text. The private
stdin/stdout pipe to notes_bridge.applescript carries the selected note for verification.
Native HTML/title conversion still requires a device-specific acceptance test.
Local verification is not evidence of iPhone delivery or atomic iCloud sync.
New writes require phone protocol v2. Retained v1 packages may only be verified
using the complete original request; their records never masquerade as v2.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
import fcntl
import hashlib
import html
from html.parser import HTMLParser
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import unicodedata
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


MAX_INPUT_BYTES = 1_000_000
MAX_BODY_CHARS = 200_000
MAX_NATIVE_BYTES = 3_000_000
MAX_JOURNAL_BYTES = 200_000
MAX_REVISIONS = 500
PHONE_PROTOCOL_VERSION = 2
JOURNAL_SCHEMA_VERSION = 2
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
URL = re.compile(r"https?://[^\s<>\"']+")
NATIVE_STAGES = {"STDIN_READING", "INPUT_PARSED", "INPUT_VALIDATED", "ACCOUNT_LOOKUP",
                 "FOLDER_LOOKUP", "FOLDER_SAFETY", "NOTE_LOOKUP", "NOTE_SAFETY", "NOTE_TITLE_READ",
                 "NOTE_BODY_READ", "NOTE_PLAINTEXT_READ", "NOTE_ID_READ", "NOTE_CREATE", "COMPLETE"}


class PublisherError(Exception):
    """Only fixed, public error codes may cross the CLI boundary."""

    def __init__(self, code: str, status: str = "invalid_request"):
        super().__init__(code)
        self.code, self.status = code, status


class NativeError(Exception):
    def __init__(self, code: str = "NATIVE_ERROR", uncertain: bool = False, stage=None):
        super().__init__(code)
        self.code, self.uncertain = code, uncertain
        self.stage = stage if stage in NATIVE_STAGES else None


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def strict_json_loads(raw):
    def object_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("DUPLICATE_JSON_KEY")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError("NONFINITE_JSON_NUMBER")

    def finite_float(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("NONFINITE_JSON_NUMBER")
        return result

    return json.loads(raw, object_pairs_hook=object_pairs, parse_constant=reject_constant, parse_float=finite_float)


def require_text(value, limit=512, multiline=False):
    if not isinstance(value, str) or not value or len(value) > limit:
        raise PublisherError("INVALID_TEXT_FIELD")
    if value != unicodedata.normalize("NFC", value) or "\u00a0" in value:
        raise PublisherError("NONCANONICAL_TEXT")
    if any(unicodedata.category(ch).startswith("C") and not (multiline and ch == "\n")
           for ch in value):
        raise PublisherError("UNSUPPORTED_CONTROL_CHARACTER")
    if not multiline and "\n" in value:
        raise PublisherError("INVALID_TEXT_FIELD")
    return value


def _protocol_version(package):
    value = package.get("protocol_version", 1)
    if type(value) is not int or value not in (1, PHONE_PROTOCOL_VERSION):
        raise PublisherError("UNSUPPORTED_PHONE_PROTOCOL")
    return value


def _local_midnight(day, zone):
    naive = datetime.combine(day, time.min)
    candidates = [naive.replace(tzinfo=zone, fold=fold) for fold in (0, 1)]
    candidates = [item for item in candidates if item.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) == naive]
    if len({item.astimezone(timezone.utc) for item in candidates}) != 1:
        raise PublisherError("AMBIGUOUS_VALIDITY_BOUNDARY")
    return candidates[0]


def validate_package(package, allow_legacy=False):
    if not isinstance(package, dict):
        raise PublisherError("INVALID_PACKAGE")
    try:
        if type(package["schema_version"]) is not int or package["schema_version"] != 1:
            raise PublisherError("UNSUPPORTED_SCHEMA")
        protocol = _protocol_version(package)
        if protocol == 1 and not allow_legacy:
            raise PublisherError("LEGACY_PROTOCOL_VERIFY_ONLY")
        for key in ("revision", "config_revision"):
            if type(package[key]) is not int or not 1 <= package[key] <= 999999:
                raise PublisherError("INVALID_REVISION")
        brief_id = require_text(package["brief_id"], 128)
        if not IDENTIFIER.fullmatch(brief_id):
            raise PublisherError("INVALID_BRIEF_ID")
        require_text(package["config_id"], 128)
        require_text(package["timezone"], 128)
        zone = ZoneInfo(package["timezone"])
        day = date.fromisoformat(package["applicable_date"])
        if day.isoformat() != package["applicable_date"]:
            raise PublisherError("INVALID_DATE")
        generated = datetime.fromisoformat(require_text(package["generated_at"], 128).replace("Z", "+00:00"))
        if generated.utcoffset() is None:
            raise PublisherError("NAIVE_GENERATED_AT")
        fresh_until = datetime.fromisoformat(require_text(package["fresh_until"], 128).replace("Z", "+00:00"))
        if fresh_until.utcoffset() is None:
            raise PublisherError("NAIVE_FRESH_UNTIL")
        if protocol == PHONE_PROTOCOL_VERSION:
            valid_from = require_text(package["valid_from"], 128)
            valid_until = require_text(package["valid_until"], 128)
            start = _local_midnight(day, zone)
            end = _local_midnight(day + timedelta(days=1), zone)
            if valid_from.replace("Z", "+00:00") != start.isoformat() or valid_until.replace("Z", "+00:00") != end.isoformat():
                raise PublisherError("INVALID_VALIDITY_BOUNDARY")
            if not start.astimezone(timezone.utc) <= generated.astimezone(timezone.utc) < end.astimezone(timezone.utc):
                raise PublisherError("GENERATED_OUTSIDE_VALIDITY")
            if fresh_until.astimezone(timezone.utc) > end.astimezone(timezone.utc):
                raise PublisherError("FRESHNESS_EXCEEDS_VALIDITY")
        if package["readiness"] not in ("READY", "PARTIAL"):
            raise PublisherError("PACKAGE_NOT_READY")
        title = require_text(package["title"], 300)
        if brief_id not in title or "MB:" in title.upper():
            raise PublisherError("TITLE_ID_MISMATCH")
        if protocol == PHONE_PROTOCOL_VERSION and title != f"晨间简报 · {package['applicable_date']} · {brief_id} · c{package['config_revision']:02d} · r{package['revision']:02d}":
            raise PublisherError("TITLE_VERSION_MISMATCH")
        content = require_text(package["content_text"], MAX_BODY_CHARS, multiline=True)
        body = require_text(package["body_text"], MAX_BODY_CHARS, multiline=True)
        if content.endswith("\n") or body.endswith("\n"):
            raise PublisherError("NONCANONICAL_TEXT")
        for key, value in (("body_sha256", body), ("content_sha256", content)):
            if not isinstance(package[key], str) or not HEX64.fullmatch(package[key]) or digest(value) != package[key]:
                raise PublisherError("PACKAGE_HASH_MISMATCH")
        markers = {
            "MB:SCHEMA=" + str(protocol), "MB:CONFIG=" + package["config_id"],
            "MB:CONFIG-REVISION=" + str(package["config_revision"]),
            "MB:DATE=" + package["applicable_date"], "MB:TIMEZONE=" + package["timezone"],
            "MB:BRIEF=" + brief_id, "MB:REVISION=" + str(package["revision"]),
            "MB:STATUS=" + package["readiness"], "MB:GENERATED=" + package["generated_at"],
            "MB:FRESH-UNTIL=" + package["fresh_until"],
        }
        if protocol == PHONE_PROTOCOL_VERSION:
            markers.update({"MB:VALID-FROM=" + package["valid_from"], "MB:VALID-UNTIL=" + package["valid_until"]})
        actual = [line for line in content.split("\n") if "MB:" in line.upper()]
        if len(actual) != len(markers) or set(actual) != markers:
            raise PublisherError("PACKAGE_MARKER_MISMATCH")
        expected = (title + "\nMB:BEGIN\nMB:CONTENT-BEGIN\n" + content +
                    "\nMB:CONTENT-END\nMB:CONTENT-SHA256=" + package["content_sha256"] + "\nMB:END")
        if body != expected:
            raise PublisherError("PACKAGE_ENVELOPE_MISMATCH")
    except (KeyError, TypeError, ValueError, OverflowError, ZoneInfoNotFoundError):
        raise PublisherError("INVALID_PACKAGE") from None
    return package


def validate_request(request, allow_legacy=False):
    if not isinstance(request, dict):
        raise PublisherError("INVALID_REQUEST")
    if set(request) - {"authorized", "state_dir", "account", "folder", "package"}:
        raise PublisherError("UNKNOWN_REQUEST_FIELD")
    if request.get("authorized") is not True:
        raise PublisherError("AUTHORIZATION_REQUIRED")
    try:
        require_text(request["account"])
        require_text(request["folder"])
        state_dir = Path(require_text(request["state_dir"], 4096))
        if not state_dir.is_absolute() or state_dir == Path("/") or state_dir == Path.home():
            raise PublisherError("EXPLICIT_PRIVATE_STATE_DIR_REQUIRED")
        validate_package(request["package"], allow_legacy=allow_legacy)
    except KeyError:
        raise PublisherError("MISSING_REQUEST_FIELD") from None
    return request


def _escape_text(text):
    # Preserve runs and edge spaces without making every word non-breaking.
    escaped = html.escape(text, quote=False)
    return re.sub(r" +", lambda m: ("&nbsp;" * len(m[0]) if m.start() == 0 or m.end() == len(escaped)
                                    else "&nbsp;" * (len(m[0]) - 1) + " "), escaped)


def text_to_html(text):
    """Encode visible text only; HTTP(S) anchor text equals its destination."""
    lines = []
    for line in text.split("\n"):
        fragments, cursor = [], 0
        for match in URL.finditer(line):
            target = match[0].rstrip(".,;:!?，。；：！？、")
            for closing, opening in ((")", "("), ("]", "["), ("}", "{")):
                while target.endswith(closing) and target.count(closing) > target.count(opening):
                    target = target[:-1]
            if not target:
                continue
            fragments.append(_escape_text(line[cursor:match.start()]))
            fragments.append('<a href="' + html.escape(target, quote=True) + '">' + _escape_text(target) + "</a>")
            cursor = match.start() + len(target)
        fragments.append(_escape_text(line[cursor:]))
        lines.append("<div>" + ("".join(fragments) if line else "<br>") + "</div>")
    return "".join(lines)


def normalize_plaintext(text):
    """Only transport normalization: NFC, line endings, NBSP, one final LF.

    Packages prohibit literal NBSP, so Notes' nonbreaking-space representation
    cannot erase a distinction present in the expected source. No other spaces,
    blank lines, zero-width characters, or substantive content are stripped.
    """
    if not isinstance(text, str) or len(text) > MAX_BODY_CHARS * 3:
        raise PublisherError("INVALID_READBACK", "conflict")
    text = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " "))
    return text[:-1] if text.endswith("\n") else text


class VisibleHTML(HTMLParser):
    """A deliberately narrow Notes-text renderer, not a browser/CSS emulator.

    Fail closed on attachments, hidden content, unsupported layout, or changed
    link destinations. Native variants outside this subset need a fresh PoC.
    """

    BLOCK = {"div", "p", "h1", "h2", "h3", "h4", "h5", "h6"}
    INLINE = {"span", "b", "strong", "i", "em", "u", "s", "strike", "font", "a"}
    CONTAINERS = {"root", "html", "body"}
    SAFE_STYLE = {"font", "font-size", "font-family", "font-weight", "font-style", "line-height",
                  "text-align", "text-decoration", "white-space", "margin", "margin-top", "margin-bottom"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = ["root", {}, []]
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        if tag not in self.BLOCK | self.INLINE | self.CONTAINERS | {"br"}:
            raise PublisherError("UNSUPPORTED_READBACK_HTML", "conflict")
        attributes = dict(attrs)
        if "hidden" in attributes or attributes.get("aria-hidden", "false").lower() != "false":
            raise PublisherError("HIDDEN_READBACK_CONTENT", "conflict")
        for declaration in attributes.get("style", "").split(";"):
            if not declaration.strip():
                continue
            key, sep, value = declaration.partition(":")
            if not sep or key.strip().lower() not in self.SAFE_STYLE or re.search(r"url|expression|none|transparent", value, re.I):
                raise PublisherError("UNSUPPORTED_READBACK_STYLE", "conflict")
        node = [tag, attributes, []]
        self.stack[-1][2].append(node)
        if tag != "br":
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag != "br":
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag == "br":
            return
        if len(self.stack) < 2 or self.stack[-1][0] != tag:
            raise PublisherError("MALFORMED_READBACK_HTML", "conflict")
        self.stack.pop()

    def handle_data(self, data):
        # Serialization whitespace between block nodes is not visible content.
        if self.stack[-1][0] in self.CONTAINERS and not data.strip():
            return
        self.stack[-1][2].append(data)

    def handle_comment(self, data):
        raise PublisherError("UNSUPPORTED_READBACK_HTML", "conflict")

    def render(self, node=None):
        node = node or self.root
        tag, attrs, children = node
        if tag == "br":
            return "\n"
        rendered = "".join(child if isinstance(child, str) else self.render(child) for child in children)
        if tag == "a":
            target = attrs.get("href", "")
            if not re.match(r"https?://", target) or target != rendered:
                raise PublisherError("READBACK_LINK_MISMATCH", "conflict")
        if tag in self.BLOCK and not rendered.endswith("\n"):
            rendered += "\n"
        return rendered


def html_to_visible(source):
    if not isinstance(source, str) or len(source) > MAX_NATIVE_BYTES:
        raise PublisherError("INVALID_READBACK", "conflict")
    parser = VisibleHTML()
    try:
        parser.feed(source)
        parser.close()
        if len(parser.stack) != 1:
            raise PublisherError("MALFORMED_READBACK_HTML", "conflict")
        return normalize_plaintext(parser.render())
    except (ValueError, RecursionError):
        raise PublisherError("MALFORMED_READBACK_HTML", "conflict") from None


def verify_note(note, package):
    if not isinstance(note, dict) or note.get("shared") is not False or note.get("password_protected") is not False:
        raise PublisherError("UNSAFE_NOTE_SCOPE", "conflict")
    note_id = note.get("note_id")
    if not isinstance(note_id, str) or not 1 <= len(note_id) <= 1024 or any(ord(ch) < 32 for ch in note_id):
        raise PublisherError("INVALID_NOTE_LOCATOR", "conflict")
    if note.get("title") != package["title"]:
        raise PublisherError("READBACK_TITLE_MISMATCH", "conflict")
    if note.get("truncated") is not False:
        raise PublisherError("TRUNCATED_READBACK", "conflict")
    plaintext = normalize_plaintext(note.get("body_text"))
    visible_html = html_to_visible(note.get("body_html"))
    if plaintext != package["body_text"] or visible_html != package["body_text"]:
        raise PublisherError("VISIBLE_BODY_MISMATCH", "conflict")
    if digest(plaintext) != package["body_sha256"] or digest(visible_html) != package["body_sha256"]:
        raise PublisherError("READBACK_HASH_MISMATCH", "conflict")
    return note_id


class NativeBridge:
    def __init__(self, timeout=15):
        if not isinstance(timeout, (int, float)) or not 1 <= timeout <= 60:
            raise PublisherError("INVALID_TIMEOUT")
        self.timeout = timeout

    def call(self, action, request):
        payload = {"authorized": True, "action": action, "account": request["account"],
                   "folder": request["folder"], "title": request["package"]["title"]}
        if action == "create":
            payload["body_html"] = text_to_html(request["package"]["body_text"])
        try:
            completed = subprocess.run(
                ["/usr/bin/osascript", str(Path(__file__).with_name("notes_bridge.applescript"))],
                input=json.dumps(payload, ensure_ascii=False), text=True, capture_output=True,
                timeout=self.timeout, check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise NativeError("NATIVE_TIMEOUT", uncertain=action == "create", stage=_native_stage(error.stderr)) from None
        except OSError:
            raise NativeError("NATIVE_UNAVAILABLE") from None
        if completed.returncode or len(completed.stdout.encode("utf-8")) > MAX_NATIVE_BYTES:
            raise NativeError("NATIVE_PROCESS_ERROR", uncertain=action == "create", stage=_native_stage(completed.stderr))
        try:
            result = strict_json_loads(completed.stdout)
        except (ValueError, TypeError):
            raise NativeError("INVALID_NATIVE_RESPONSE", uncertain=action == "create", stage=_native_stage(completed.stderr)) from None
        if not isinstance(result, dict) or result.get("ok") is not True:
            # Never forward native error messages, which can contain private text.
            raise NativeError("NATIVE_REQUEST_FAILED", uncertain=action == "create", stage=_native_stage(completed.stderr))
        return result


def _native_stage(stderr):
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="ignore")
    if not isinstance(stderr, str):
        return None
    stages = [value for value in re.findall(r"^MB_STAGE:([A-Z_]+)$", stderr[-4000:], re.MULTILINE) if value in NATIVE_STAGES]
    return stages[-1] if stages else None


def _native_diagnostic(error):
    return {"native_stage": error.stage} if error.stage else {}


def _check_private_file(fd):
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PublisherError("STATE_FILE_NOT_PRIVATE", "blocked")


@contextmanager
def state_lock(state_dir):
    path = Path(state_dir)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PublisherError("STATE_DIR_NOT_PRIVATE", "blocked")
    fd = os.open(path / "notes-publisher.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        _check_private_file(fd)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PublisherError("PUBLISHER_BUSY", "blocked") from None
        yield path
    finally:
        os.close(fd)


class Journal:
    """Private publication evidence, separate from config/package JSON schema.

    v2 keys identify both config and report revisions. v1 journal entries retain
    their unknown configuration as legacy.rN until original-package readback;
    any unresolved legacy or current entry blocks another publication version.
    """

    def __init__(self, directory, request):
        package = request["package"]
        scope = json.dumps([request["account"], request["folder"], package["brief_id"]], ensure_ascii=False)
        self.path = directory / ("notes-" + digest(scope) + ".json")
        self.records = {}
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            _check_private_file(stream.fileno())
            raw = stream.read(MAX_JOURNAL_BYTES + 1)
        try:
            data = strict_json_loads(raw)
            if (len(raw.encode("utf-8")) > MAX_JOURNAL_BYTES or not isinstance(data, dict) or
                    set(data) != {"schema_version", "records"} or type(data["schema_version"]) is not int or
                    data["schema_version"] not in (1, JOURNAL_SCHEMA_VERSION) or not isinstance(data["records"], dict) or
                    len(data["records"]) > MAX_REVISIONS):
                raise ValueError()
            for version, record in data["records"].items():
                key_pattern = (r"[1-9][0-9]{0,5}" if data["schema_version"] == 1 else
                               r"(?:legacy\.r[1-9][0-9]{0,5}|(?:p1\.)?c[1-9][0-9]{0,5}\.r[1-9][0-9]{0,5})")
                if (not re.fullmatch(key_pattern, version) or not isinstance(record, dict) or
                        set(record) - {"state", "body_sha256", "title_sha256", "note_id"} or
                        record["state"] not in ("pending", "uncertain", "verified", "conflict")):
                    raise ValueError()
                if not HEX64.fullmatch(record["body_sha256"]) or not HEX64.fullmatch(record["title_sha256"]):
                    raise ValueError()
                if "note_id" in record:
                    locator = record["note_id"]
                    if not isinstance(locator, str) or not 1 <= len(locator) <= 1024 or any(ord(ch) < 32 for ch in locator):
                        raise ValueError()
                elif record["state"] == "verified":
                    raise ValueError()
                # Old journals did not record config_revision. Keep that
                # ambiguity explicit instead of assigning their hashes to c1.
                key = "legacy.r" + version if data["schema_version"] == 1 else version
                self.records[key] = record
        except (ValueError, KeyError, TypeError):
            raise PublisherError("INVALID_STATE_JOURNAL", "blocked") from None

    def key(self, package):
        version = f"c{package['config_revision']}.r{package['revision']}"
        if _protocol_version(package) == 1:
            legacy = "legacy.r" + str(package["revision"])
            return legacy if legacy in self.records else "p1." + version
        return version

    def record(self, package):
        return self.records.get(self.key(package))

    def check(self, package, publishing=True):
        previous = self.record(package)
        if previous and (previous["body_sha256"] != package["body_sha256"] or previous["title_sha256"] != digest(package["title"])):
            # Preserve the original hashes so the original package can later
            # resolve this conflict by exact readback. Never bless new content.
            previous["state"] = "conflict"
            self._persist()
            raise PublisherError("IMMUTABLE_REVISION_MISMATCH", "conflict")
        for version, record in self.records.items():
            if publishing and version != self.key(package) and record["state"] in ("pending", "uncertain", "conflict"):
                raise PublisherError("UNRESOLVED_PREVIOUS_REVISION", "conflict" if record["state"] == "conflict" else "uncertain")

    def save(self, package, state, note_id=None):
        if len(self.records) >= MAX_REVISIONS and self.key(package) not in self.records:
            raise PublisherError("REVISION_LIMIT_REQUIRES_REVIEW", "blocked")
        record = {"state": state, "body_sha256": package["body_sha256"], "title_sha256": digest(package["title"])}
        if note_id:
            record["note_id"] = note_id
        self.records[self.key(package)] = record
        self._persist()

    def _persist(self):
        data = json.dumps({"schema_version": JOURNAL_SCHEMA_VERSION, "records": self.records}, separators=(",", ":"))
        if len(data.encode("utf-8")) > MAX_JOURNAL_BYTES:
            raise PublisherError("STATE_SIZE_REQUIRES_REVIEW", "blocked")
        temp_name = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, prefix=".notes-state-", delete=False) as stream:
                temp_name = stream.name
                os.fchmod(stream.fileno(), 0o600)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, self.path)
            temp_name = None
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temp_name:
                os.unlink(temp_name)


def _notes(result):
    if not isinstance(result, dict) or result.get("complete") is not True or result.get("truncated") is not False:
        raise NativeError("INCOMPLETE_NATIVE_LOOKUP")
    notes = result.get("notes")
    if not isinstance(notes, list) or len(notes) > 2:
        raise NativeError("INVALID_NATIVE_RESPONSE")
    if len(notes) > 1:
        raise PublisherError("DUPLICATE_VERSION", "conflict")
    return notes


def _result(request, status, code, **extra):
    package = request.get("package", {}) if isinstance(request, dict) else {}
    result = {"ok": status in ("dry_run", "local_verified"), "status": status, "code": code,
              "local_verified": status == "local_verified", "iphone_sync": "unverified"}
    # Only validated identity fields reach this helper after input validation.
    if status != "invalid_request":
        result.update({key: package[key] for key in ("brief_id", "config_revision", "revision", "readiness") if key in package})
        if package:
            result["protocol_version"] = _protocol_version(package)
            if result["protocol_version"] == 1:
                result["legacy_verification_only"] = True
    result.update(extra)
    return result


def execute(request, operation="publish", apply=False, bridge=None):
    """Publish defaults to no native contact and no local writes; verify reads.

    All live callers must share the same confirmed private state_dir. A single
    machine file lock cannot coordinate other machines or a second state_dir.
    Unknown commits are never automatically recreated, even after zero matches.
    """
    mutation_attempted = False
    validated = False
    try:
        if operation not in ("publish", "verify"):
            raise PublisherError("INVALID_OPERATION")
        validate_request(request, allow_legacy=operation == "verify")
        validated = True
        if operation == "verify" and apply:
            raise PublisherError("VERIFY_IS_READ_ONLY")
        if operation == "publish" and not apply:
            # Roundtrip the encoder offline as an early consistency check.
            if html_to_visible(text_to_html(request["package"]["body_text"])) != request["package"]["body_text"]:
                raise PublisherError("HTML_ENCODING_MISMATCH")
            return _result(request, "dry_run", "NO_NOTES_CONTACT", action="none")
        bridge = bridge or NativeBridge()
        package = request["package"]
        with state_lock(request["state_dir"]) as directory:
            journal = Journal(directory, request)
            journal.check(package, publishing=operation == "publish")
            previous = journal.record(package)
            try:
                notes = _notes(bridge.call("lookup", request))
            except NativeError as error:
                status = "uncertain" if previous and previous["state"] in ("pending", "uncertain") else "read_error"
                return _result(request, status, error.code, **_native_diagnostic(error))
            except PublisherError as error:
                # A duplicate discovered before any create still requires human
                # resolution; do not allow a fresh revision to bypass it.
                journal.save(package, "conflict")
                return _result(request, "conflict", error.code, action="inspect_same_revision")
            if notes:
                try:
                    note_id = verify_note(notes[0], package)
                except PublisherError as error:
                    journal.save(package, "conflict")
                    return _result(request, "conflict", error.code)
                journal.save(package, "verified", note_id)
                return _result(request, "local_verified", "READBACK_MATCH", note_id=note_id, action="existing")
            if previous:
                status = "uncertain" if previous["state"] in ("pending", "uncertain") else "not_found"
                return _result(request, status, "RECORDED_VERSION_NOT_VISIBLE", action="none")
            if operation == "verify":
                return _result(request, "not_found", "VERSION_NOT_FOUND", action="none")
            # Durably mark pending *before* the first possible native mutation.
            journal.save(package, "pending")
            mutation_attempted = True
            try:
                created = bridge.call("create", request)  # Native side rechecks exact-title uniqueness.
                _notes(created)
                notes = _notes(bridge.call("lookup", request))
                if not notes:
                    raise NativeError("CREATED_VERSION_NOT_VISIBLE", uncertain=True)
                note_id = verify_note(notes[0], package)
                journal.save(package, "verified", note_id)
                return _result(request, "local_verified", "READBACK_MATCH", note_id=note_id,
                               action="created" if created.get("created") is True else "existing")
            except NativeError as error:
                journal.save(package, "uncertain")
                return _result(request, "uncertain", error.code, action="verify_same_revision", **_native_diagnostic(error))
            except PublisherError as error:
                journal.save(package, "conflict")
                return _result(request, "conflict", error.code, action="inspect_same_revision")
    except PublisherError as error:
        status = "uncertain" if mutation_attempted and error.status == "blocked" else error.status
        return _result(request if validated else {}, status, error.code)
    except OSError:
        return _result(request if validated else {}, "uncertain" if mutation_attempted else "blocked", "LOCAL_STATE_ERROR")
    except Exception:
        # Fixed diagnostics only, including unexpected mock/native failures.
        return _result(request if validated else {}, "uncertain" if mutation_attempted else "read_error", "PUBLISHER_ERROR")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("publish", "verify"))
    parser.add_argument("--apply", action="store_true", help="authorize a publish attempt; omitted means fully offline dry-run")
    parser.add_argument("--timeout", type=float, default=15, help="per-stage native timeout, 1–60 seconds (three stages at most)")
    args = parser.parse_args(argv)
    try:
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            raise PublisherError("INPUT_LIMIT_EXCEEDED")
        request = strict_json_loads(raw)
        result = execute(request, args.operation, args.apply, NativeBridge(args.timeout))
    except (ValueError, UnicodeError):
        result = _result({}, "invalid_request", "INVALID_JSON")
    except PublisherError as error:
        result = _result({}, error.status, error.code)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return {"dry_run": 0, "local_verified": 0, "invalid_request": 2, "uncertain": 3,
            "conflict": 4, "not_found": 5, "read_error": 6, "blocked": 7}.get(result["status"], 7)


if __name__ == "__main__":
    raise SystemExit(main())
