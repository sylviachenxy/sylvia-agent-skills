"""Private operator-reported bindings, independent from saving configuration.

No device, Notes, scheduler, or external account is contacted. Callers performing
acknowledge(apply=True) should hold ConfigStore.locked_resolve across this call;
our repeated read-only resolve checks are not a replacement for that CAS lock.
Integrity hashes detect damaged/inconsistent local records, not a malicious user
who owns the directory and can rewrite both this code and the records.
"""

from __future__ import annotations

from contextlib import contextmanager
import copy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import stat
import tempfile
import unicodedata


TARGETS = ("iphone", "automation")
KIND = "operator_report_not_automatic_proof"
PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
MAX_BYTES = 1024 * 1024
MAX_RECORDS = 500


class BindingError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _digest(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def _strict_loads(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise BindingError("INVALID_BINDING_JSON")
            result[key] = value
        return result

    def constant(_):
        raise BindingError("INVALID_BINDING_JSON")

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise BindingError("INVALID_BINDING_JSON")
        return number

    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant, parse_float=finite_float)
    except (ValueError, UnicodeError, RecursionError):
        raise BindingError("INVALID_BINDING_JSON") from None


def _text(value, maximum=512):
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum or
            value != unicodedata.normalize("NFC", value) or
            any(unicodedata.category(ch).startswith("C") for ch in value)):
        raise BindingError("INVALID_BINDING_TEXT")
    return value


def _path(value):
    path = Path(_text(value, 4096))
    if not path.is_absolute() or ".." in path.parts or path in (Path("/"), Path.home(), Path("/tmp"), Path("/var")):
        raise BindingError("PRIVATE_PATH_REQUIRED")
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists() or (candidate / ".git").is_symlink() or (candidate / "SKILL.md").exists():
            raise BindingError("BINDINGS_OUTSIDE_REPOSITORY_REQUIRED")
        if candidate.is_symlink():
            system_alias = {Path("/tmp"): Path("/private/tmp"), Path("/var"): Path("/private/var")}
            if candidate not in system_alias or candidate.resolve() != system_alias[candidate]:
                raise BindingError("SYMLINK_NOT_ALLOWED")
    return path.resolve(strict=False)


def _private_dir(path, create=False):
    path = _path(str(path))
    if not path.exists():
        if not create:
            return path
        path.mkdir(mode=0o700, exist_ok=False)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise BindingError("DIRECTORY_NOT_PRIVATE")
    return path


def _check_file(fd):
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise BindingError("FILE_NOT_PRIVATE")
    if info.st_size > MAX_BYTES:
        raise BindingError("BINDING_FILE_TOO_LARGE")


def _read(path):
    _path(str(path))
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as handle:
        _check_file(handle.fileno())
        raw = handle.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise BindingError("BINDING_FILE_TOO_LARGE")
    return _strict_loads(raw)


def _current_resolved(registry_dir, profile):
    # Store has no dependency on this module. Resolve is read-only and therefore
    # also usable while the CLI holds Store.locked_resolve's registry write lock.
    from config_store import ConfigStore
    return ConfigStore(registry_dir).resolve(profile)


def _context(resolved):
    required = {"config", "config_path", "fingerprint", "profile", "registry_dir", "profile_dir"}
    if not isinstance(resolved, dict) or not required.issubset(resolved):
        raise BindingError("INVALID_RESOLVED_CONFIG")
    profile = _text(resolved["profile"], 80)
    if not PROFILE.fullmatch(profile):
        raise BindingError("INVALID_PROFILE")
    config = resolved["config"]
    fingerprint = resolved["fingerprint"]
    if not isinstance(config, dict) or not isinstance(fingerprint, str) or not HEX64.fullmatch(fingerprint) or _digest(config) != fingerprint:
        raise BindingError("CONFIG_FINGERPRINT_MISMATCH")
    root = _private_dir(_path(resolved["registry_dir"]))
    profile_dir = _private_dir(_path(resolved["profile_dir"]))
    config_path = _path(resolved["config_path"])
    if not root.exists() or not profile_dir.exists() or not config_path.is_relative_to(profile_dir):
        raise BindingError("INVALID_PROFILE_LOCATION")
    _private_dir(config_path.parent)
    snapshot = _read(config_path)
    if snapshot is None or _digest(snapshot) != fingerprint:
        raise BindingError("SNAPSHOT_FINGERPRINT_MISMATCH")
    current = _current_resolved(str(root), profile)
    if (not isinstance(current, dict) or current.get("fingerprint") != fingerprint or current.get("profile") != profile or
            current.get("registry_dir") != str(root) or current.get("profile_dir") != str(profile_dir) or
            current.get("config_path") != str(config_path) or current.get("config") != config):
        raise BindingError("STALE_RESOLVED_CONFIG")
    # These are the only config fields used by either handoff. Store validates
    # the full config; local shape checks also fail closed for a malformed caller.
    if (type(config.get("config_revision")) is not int or config["config_revision"] < 1 or
            not isinstance(config.get("schedule"), dict) or not isinstance(config.get("storage"), dict)):
        raise BindingError("INVALID_RESOLVED_CONFIG")
    _text(config["config_id"], 80)
    _text(config["timezone"], 128)
    notes = config["storage"].get("notes")
    if not isinstance(notes, dict) or set(notes) != {"account", "folder", "shared"} or notes.get("shared") is not False:
        raise BindingError("INVALID_NOTES_SCOPE")
    _text(notes["account"])
    _text(notes["folder"])
    state_dir = _path(config["storage"]["state_dir"])
    if state_dir.exists():
        _private_dir(state_dir)
    if config["storage"].get("scope") != "private-local":
        raise BindingError("INVALID_STORAGE_SCOPE")
    schedule = config["schedule"]
    if not {"weekdays", "generate_at", "ready_by", "wake_at"}.issubset(schedule):
        raise BindingError("INVALID_SCHEDULE")
    return {"config": config, "config_path": str(config_path), "fingerprint": fingerprint,
            "profile": profile, "registry_dir": str(root), "profile_dir": str(profile_dir)}


def _handoff_parameters(context, target):
    config = context["config"]
    notes = dict(config["storage"]["notes"])
    if target == "iphone":
        # The phone identifies one installed delivery channel, not a mutable
        # preference version. ConfigStore prevents changing this Notes target.
        return {"protocol_version": 2, "config_id": config["config_id"], "storage": {"notes": notes}}
    return {"protocol_version": 1, "config_id": config["config_id"],
            "registry_dir": context["registry_dir"], "profile": context["profile"], "timezone": config["timezone"],
            "schedule": copy.deepcopy(config["schedule"]),
            "storage": {"scope": "private-local", "state_dir": config["storage"]["state_dir"], "notes": notes}}


def _signature_input(context, target):
    if target == "iphone":
        return {"schema_version": 2, "target": target, "parameters": _handoff_parameters(context, target)}
    value = {"schema_version": 1, "target": target, "profile": context["profile"],
             "registry_dir": context["registry_dir"], "profile_dir": context["profile_dir"],
             "parameters": _handoff_parameters(context, target)}
    return value


def _validate_binding_parameters(context, target, record):
    parameters = record["binding_parameters"]
    if not isinstance(parameters, dict) or type(parameters.get("schema_version")) is not int:
        raise BindingError("INVALID_BINDING_RECORD")
    if target == "iphone" and parameters["schema_version"] == 2:
        if set(parameters) != {"schema_version", "target", "parameters"} or parameters["target"] != "iphone":
            raise BindingError("INVALID_BINDING_RECORD")
        payload = parameters["parameters"]
        if (not isinstance(payload, dict) or set(payload) != {"protocol_version", "config_id", "storage"} or
                type(payload["protocol_version"]) is not int or payload["protocol_version"] != 2 or
                payload["config_id"] != record["config_id"] or not isinstance(payload["storage"], dict) or
                set(payload["storage"]) != {"notes"}):
            raise BindingError("INVALID_BINDING_RECORD")
        notes = payload["storage"]["notes"]
        if not isinstance(notes, dict) or set(notes) != {"account", "folder", "shared"} or notes["shared"] is not False:
            raise BindingError("INVALID_BINDING_RECORD")
        _text(notes["account"])
        _text(notes["folder"])
    else:
        # Historical v1 phone evidence remains hash-checked and readable, but
        # never verifies the v2 channel implicitly. Automation retains v1.
        parameter_keys = {"schema_version", "target", "profile", "registry_dir", "profile_dir", "parameters"}
        if target == "iphone":
            parameter_keys.add("config_fingerprint")
        if (set(parameters) != parameter_keys or parameters["schema_version"] != 1 or parameters["target"] != target or
                parameters["profile"] != context["profile"] or parameters["registry_dir"] != context["registry_dir"] or
                parameters["profile_dir"] != record["profile_dir"] or not isinstance(parameters["parameters"], dict) or
                parameters["parameters"].get("config_id") != record["config_id"]):
            raise BindingError("INVALID_BINDING_RECORD")
        if target == "iphone":
            payload = parameters["parameters"]
            if (parameters["config_fingerprint"] != record["config_fingerprint"] or
                    payload.get("config_revision") != record["config_revision"] or
                    type(payload.get("protocol_version")) is not int or payload["protocol_version"] != 1):
                raise BindingError("INVALID_BINDING_RECORD")
    if _digest(parameters) != record["binding_signature"]:
        raise BindingError("INVALID_BINDING_RECORD")


def _directories(context, create=False):
    path = _private_dir(context["registry_dir"])
    for component in ("bindings", context["profile"]):
        path = _private_dir(path / component, create=create)
    return path


def _record_path(context, target):
    return _directories(context) / (target + ".json")


def _load_records(context, target):
    document = _read(_record_path(context, target))
    if document is None:
        return {"schema_version": 1, "profile": context["profile"], "registry_dir": context["registry_dir"], "target": target, "records": []}
    try:
        if (not isinstance(document, dict) or set(document) != {"schema_version", "profile", "registry_dir", "target", "records"} or
                type(document["schema_version"]) is not int or document["schema_version"] != 1 or
                document["profile"] != context["profile"] or document["registry_dir"] != context["registry_dir"] or document["target"] != target or
                not isinstance(document["records"], list) or not 1 <= len(document["records"]) <= MAX_RECORDS):
            raise BindingError("INVALID_BINDING_RECORD")
        previous = None
        fields = {"target", "binding_signature", "binding_id", "evidence", "recorded_at", "config_fingerprint",
                  "config_id", "config_revision", "profile", "registry_dir", "profile_dir", "kind",
                  "previous_record_sha256", "record_sha256", "binding_parameters"}
        for record in document["records"]:
            if (not isinstance(record, dict) or set(record) != fields or record["target"] != target or
                    not isinstance(record["binding_signature"], str) or not HEX64.fullmatch(record["binding_signature"]) or record["kind"] != KIND or
                    record["profile"] != context["profile"] or record["registry_dir"] != context["registry_dir"] or
                    record["config_id"] != context["config"]["config_id"] or
                    record["previous_record_sha256"] != previous or type(record["config_revision"]) is not int or record["config_revision"] < 1 or
                    not isinstance(record["config_fingerprint"], str) or not HEX64.fullmatch(record["config_fingerprint"])):
                raise BindingError("INVALID_BINDING_RECORD")
            _text(record["binding_id"], 512)
            _text(record["evidence"], 2000)
            _text(record["profile_dir"], 4096)
            _text(record["config_id"], 80)
            when = datetime.fromisoformat(_text(record["recorded_at"], 64))
            if when.utcoffset() is None:
                raise BindingError("INVALID_BINDING_RECORD")
            _validate_binding_parameters(context, target, record)
            unsigned = {key: value for key, value in record.items() if key != "record_sha256"}
            if record["record_sha256"] != _digest(unsigned):
                raise BindingError("BINDING_INTEGRITY_MISMATCH")
            previous = record["record_sha256"]
    except (KeyError, TypeError, ValueError):
        raise BindingError("INVALID_BINDING_RECORD") from None
    return document


def _target_status(context, target):
    document = _load_records(context, target)
    signature_input = _signature_input(context, target)
    signature = _digest(signature_input)
    result = {"status": "pending", "signature": signature, "live_state_checked": False}
    if target == "iphone":
        result["protocol_version"] = 2
    if document["records"]:
        record = document["records"][-1]
        result.update({"last_observed_signature": record["binding_signature"], "last_recorded_at": record["recorded_at"],
                       "last_binding_id": record["binding_id"]})
        if target == "iphone" and record["binding_parameters"]["schema_version"] == 1:
            result.update(pending_reason="phone_protocol_upgrade_required", legacy_protocol_version=1)
            return result
        # Only the most recent observation describes the downstream binding.
        # An older matching A must not revive after A -> B -> desired A.
        if record["binding_signature"] != signature or record["binding_parameters"] != signature_input:
            return result
        reused = record["config_fingerprint"] != context["fingerprint"]
        result.update({"status": "verified", "kind": KIND, "binding_id": record["binding_id"], "recorded_at": record["recorded_at"],
                       "verified_config_fingerprint": record["config_fingerprint"], "evidence_count": len(document["records"]),
                       "reused_for_current_config": reused,
                       "verification_basis": ("verified_delivery_channel" if target == "iphone" else
                                              "same_binding_signature" if reused else "current_config_fingerprint")})
    return result


def _status(context):
    targets = {target: _target_status(context, target) for target in TARGETS}
    pending = [target for target in TARGETS if targets[target]["status"] != "verified"]
    return {"ok": True, "configuration_saved": True, "deployment_ready": not pending, "profile": context["profile"],
            "config_fingerprint": context["fingerprint"], "targets": targets, "pending_targets": pending,
            "kind": KIND, "live_state_checked": False}


def _failure(error):
    return {"ok": False, "deployment_ready": False, "code": error.code if isinstance(error, BindingError) else "BINDING_STATE_ERROR",
            "live_state_checked": False}


def status(resolved):
    try:
        return _status(_context(resolved))
    except Exception as error:
        return _failure(error)


def handoff(resolved):
    try:
        context = _context(resolved)
        result = _status(context)
        for target in TARGETS:
            result["targets"][target]["payload"] = _handoff_parameters(context, target)
        selector = "--profile " + shlex.quote(context["profile"]) + " --registry-dir " + shlex.quote(context["registry_dir"])
        result["targets"]["iphone"]["instruction"] = (
            "首次设置或从协议 v1 升级时，将此固定投递参数配置到该 iPhone 的通知与阅读两条快捷指令，并验证实际设备；"
            "协议 v2 通道确认后，普通配置更新与历史恢复不要求修改手机或重新确认。"
            "仅在用户报告或实际验证后记录确认。普通快捷指令同步不代表个人起床自动化已设置。")
        result["targets"]["automation"]["prompt"] = (
            "使用 $morning-brief，按固定定位参数 " + selector + " 重新加载最新已保存配置，不固定引用历史配置快照。"
            "每次先用该 skill 安装目录中的 scripts/morning-brief.py（以 python3 运行）执行 config status " + selector + " --require-ready；"
            "只有就绪检查通过后，才按该 profile 当前时区、schedule、已授权来源与精确 Notes 输出范围采集、生成并发布晨报。"
            "不得创建、修改或完成任务、日历安排和 Goals；失败、冲突、缺权限或绑定未就绪时停止并报告。"
            "不得自动扩大范围、修改绑定或改变排程；本提示不表示已创建或验证自动化。")
        result["external_actions_performed"] = False
        return result
    except Exception as error:
        return _failure(error)


@contextmanager
def _binding_lock(context):
    root = _private_dir(context["registry_dir"])
    directory = _private_dir(root / "bindings", create=True)
    fd = os.open(directory / ".bindings.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        _check_file(fd)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BindingError("BINDING_WRITER_BUSY") from None
        yield
    finally:
        os.close(fd)


def _append_document(path, document):
    data = _encoded(document)
    if len(data) > MAX_BYTES or len(document["records"]) > MAX_RECORDS:
        raise BindingError("BINDING_HISTORY_LIMIT")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=".binding-", delete=False) as handle:
            temporary = handle.name
            os.fchmod(handle.fileno(), 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary:
            os.unlink(temporary)


def acknowledge(resolved, target, expected_fingerprint, binding_id, evidence, apply=False):
    try:
        if target not in TARGETS:
            raise BindingError("INVALID_BINDING_TARGET")
        if type(apply) is not bool:
            raise BindingError("INVALID_APPLY_FLAG")
        if not isinstance(expected_fingerprint, str) or not HEX64.fullmatch(expected_fingerprint):
            raise BindingError("INVALID_EXPECTED_FINGERPRINT")
        if not isinstance(resolved, dict) or resolved.get("fingerprint") != expected_fingerprint:
            raise BindingError("STALE_ACKNOWLEDGEMENT")
        _text(binding_id, 512)
        _text(evidence, 2000)
        context = _context(resolved)
        current_status = _status(context)
        if not apply:
            current_status.update({"applied": False, "recorded": False, "operation": "dry_run", "target": target,
                                   "binding_signature": current_status["targets"][target]["signature"]})
            return current_status
        with _binding_lock(context):
            context = _context(resolved)
            document = _load_records(context, target)
            signature_input = _signature_input(context, target)
            signature = _digest(signature_input)
            latest = document["records"][-1] if document["records"] else None
            identical = latest if latest and latest["binding_id"] == binding_id and latest["evidence"] == evidence and latest["config_fingerprint"] == expected_fingerprint and latest["binding_signature"] == signature else None
            if identical is None:
                record = {"target": target, "binding_signature": signature, "binding_parameters": signature_input, "binding_id": binding_id,
                          "evidence": evidence, "recorded_at": datetime.now(timezone.utc).isoformat(),
                          "config_fingerprint": expected_fingerprint, "config_id": context["config"]["config_id"],
                          "config_revision": context["config"]["config_revision"], "profile": context["profile"],
                          "registry_dir": context["registry_dir"], "profile_dir": context["profile_dir"], "kind": KIND,
                          "previous_record_sha256": document["records"][-1]["record_sha256"] if document["records"] else None}
                record["record_sha256"] = _digest(record)
                document["records"].append(record)
                directory = _directories(context, create=True)
                _append_document(directory / (target + ".json"), document)
            # A fresh resolve prevents reporting a stale snapshot as current.
            # The CLI's outer Store.locked_resolve supplies transaction isolation.
            result = _status(_context(resolved))
            result.update({"applied": True, "recorded": identical is None, "operation": "acknowledge", "target": target,
                           "binding_signature": signature})
            return result
    except Exception as error:
        return _failure(error)
