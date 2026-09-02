"""Private, versioned configuration discovery with one atomic registry commit point.

Constructors, reads and dry-runs never create files. Only explicit apply mutates
configuration storage. No source account, scheduler or publisher is contacted.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import time

from brief_core import ValidationError, validate_config


MAX_BYTES = 1_048_576
MAX_PROFILES = 100
MAX_HISTORY = 1000
LOCK_WAIT_SECONDS = 5.0
PROFILE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")
HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
SYSTEM_ALIASES = {Path("/tmp"): Path("/private/tmp"), Path("/var"): Path("/private/var")}


def _error(code):
    raise ValidationError(code)


@contextmanager
def _redacted_errors():
    try:
        yield
    except ValidationError:
        raise
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
        raise ValidationError("config_store_io_or_format_error; private details suppressed") from None


def _encoded(value):
    try:
        raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ValidationError("config_store_invalid_json") from None
    if len(raw) > MAX_BYTES:
        _error("config_store_json_size_limit")
    return raw


def fingerprint(config):
    """Match morning-brief.py's pretty canonical JSON + LF fingerprint exactly."""
    return hashlib.sha256(_encoded(config)).hexdigest()


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            _error("config_store_duplicate_json_key")
        value[key] = item
    return value


def _finite(text):
    number = float(text)
    if not math.isfinite(number):
        _error("config_store_nonfinite_json")
    return number


def _constant(_):
    _error("config_store_nonfinite_json")


def _profile(value):
    if not isinstance(value, str) or not PROFILE_RE.fullmatch(value):
        _error("config_store_invalid_profile")
    return value


def _revision(value, minimum=1):
    if type(value) is not int or not minimum <= value <= 999999:
        _error("config_store_invalid_revision")
    return value


def _flag(value):
    if type(value) is not bool:
        _error("config_store_expected_boolean")
    return value


def _exists(path):
    return os.path.lexists(path)


def _safe_path(value):
    if not isinstance(value, (str, os.PathLike)):
        _error("config_store_invalid_path")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or path in (Path("/"), Path.home(), Path("/tmp"), Path("/var")):
        _error("config_store_requires_dedicated_absolute_path")
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            expected = SYSTEM_ALIASES.get(ancestor)
            if expected is None or ancestor.resolve() != expected:
                _error("config_store_symlink_refused")
        if _exists(ancestor / ".git") or _exists(ancestor / "SKILL.md"):
            _error("config_store_git_or_skill_directory_refused")
    return path.resolve(strict=False)


def _private_directory(value, *, create=False):
    path = _safe_path(value)
    if not _exists(path):
        if not create:
            return path
        missing = []
        cursor = path
        while not _exists(cursor):
            missing.append(cursor)
            cursor = cursor.parent
        for directory in reversed(missing):
            try:
                directory.mkdir(mode=0o700)
            except FileExistsError:
                pass
            _private_directory(directory)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        _error("config_store_private_directory_requires_owner_0700")
    return path


def _checked_file(fd):
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
        _error("config_store_private_file_requires_owner_0600_and_single_link")
    if info.st_size > MAX_BYTES:
        _error("config_store_json_size_limit")


def _read_json(path):
    path = _safe_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        _checked_file(fd)
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            _error("config_store_json_size_limit")
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique, parse_float=_finite, parse_constant=_constant)
        if not isinstance(value, dict):
            _error("config_store_json_object_required")
        return value
    finally:
        os.close(fd)


def _validate_config(value):
    try:
        result = validate_config(value)
        _encoded(result)
    except (ValidationError, ValueError, TypeError, RecursionError):
        raise ValidationError("config_store_invalid_config; run offline configuration validation") from None
    _private_directory(result["storage"]["state_dir"])
    return result


def _diff(before, after, prefix=""):
    if before == after:
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        paths = []
        for key in sorted(set(before) | set(after)):
            if not prefix and key == "config_revision":
                continue
            name = (prefix + "." if prefix else "") + key
            if key not in before or key not in after:
                paths.append(name)
            else:
                paths.extend(_diff(before[key], after[key], name))
        return paths
    return [prefix] if prefix else sorted(key for key in after if key != "config_revision")


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class ConfigStore:
    def __init__(self, registry_dir=None):
        # Calculate the default only. Do not inspect, create or migrate user data.
        self.registry_dir = Path(registry_dir) if registry_dir is not None else Path.home() / "Library/Application Support/morning-brief"

    @property
    def registry_path(self):
        return self.registry_dir / "registry.json"

    def _root(self, create=False):
        return _private_directory(self.registry_dir, create=create)

    def _profile_directory(self, profile, requested=None):
        root = self._root()
        directory = _private_directory(requested if requested is not None else root / "profiles" / profile)
        default = root / "profiles" / profile
        if directory == root or directory in root.parents or (root in directory.parents and directory != default):
            _error("config_store_profile_directory_overlaps_registry")
        return directory

    @staticmethod
    def _snapshot_path(directory, record):
        return directory / "revisions" / (f"r{record['revision']:06d}-{record['fingerprint']}.json")

    def _load_registry(self):
        root = self._root()
        path = root / "registry.json"
        if not _exists(path):
            return {"schema_version": 1, "default_profile": None, "profiles": {}}
        registry = _read_json(path)
        if set(registry) != {"schema_version", "default_profile", "profiles"} or type(registry["schema_version"]) is not int or registry["schema_version"] != 1:
            _error("config_store_corrupt_registry_schema")
        profiles = registry["profiles"]
        if not isinstance(profiles, dict) or len(profiles) > MAX_PROFILES:
            _error("config_store_corrupt_registry_profiles")
        if registry["default_profile"] is not None and (not isinstance(registry["default_profile"], str) or registry["default_profile"] not in profiles):
            _error("config_store_corrupt_default_profile")
        names, directories = set(), []
        for name, entry in profiles.items():
            _profile(name)
            if name.casefold() in names:
                _error("config_store_ambiguous_profile_case")
            names.add(name.casefold())
            if not isinstance(entry, dict) or set(entry) != {"directory", "history"} or not isinstance(entry["directory"], str):
                _error("config_store_corrupt_profile_entry")
            directory = self._profile_directory(name, entry["directory"])
            if str(directory) != entry["directory"] or not directory.exists():
                _error("config_store_corrupt_profile_directory")
            for existing in directories:
                if directory == existing or directory in existing.parents or existing in directory.parents or str(directory).casefold() == str(existing).casefold():
                    _error("config_store_profile_directories_overlap")
            directories.append(directory)
            revisions = _private_directory(directory / "revisions")
            history = entry["history"]
            if not isinstance(history, list) or not 1 <= len(history) <= MAX_HISTORY:
                _error("config_store_corrupt_history")
            prior = None
            for record in history:
                if not isinstance(record, dict) or set(record) != {"revision", "fingerprint"}:
                    _error("config_store_corrupt_history_record")
                _revision(record["revision"])
                if not isinstance(record["fingerprint"], str) or not HASH_RE.fullmatch(record["fingerprint"]):
                    _error("config_store_corrupt_fingerprint")
                if prior is not None and record["revision"] != prior + 1:
                    _error("config_store_nonsequential_history")
                prior = record["revision"]
                snapshot = self._snapshot_path(directory, record)
                # Inspect committed paths only. Never discover or adopt orphan files.
                _safe_path(snapshot)
                if not revisions.exists() or not snapshot.is_file():
                    _error("config_store_missing_committed_snapshot")
        return registry

    @staticmethod
    def _selected(registry, profile):
        if profile is not None:
            name = _profile(profile)
            if name not in registry["profiles"]:
                _error("config_store_profile_not_found")
            return name
        if registry["default_profile"] is not None:
            return registry["default_profile"]
        names = list(registry["profiles"])
        if len(names) == 1:
            return names[0]
        _error("config_store_default_required" if names else "config_store_no_profiles")

    def _read_record(self, profile, entry, record):
        directory = self._profile_directory(profile, entry["directory"])
        _private_directory(directory / "revisions")
        path = self._snapshot_path(directory, record)
        config = _validate_config(_read_json(path))
        if config["config_id"] != profile:
            _error("config_store_profile_identity_mismatch")
        if config["config_revision"] != record["revision"] or fingerprint(config) != record["fingerprint"]:
            _error("config_store_snapshot_integrity_mismatch")
        return {"config": config, "config_path": str(path), "fingerprint": record["fingerprint"], "profile": profile, "registry_dir": str(self._root()), "profile_dir": str(directory)}

    def _resolve(self, registry, profile=None):
        name = self._selected(registry, profile)
        entry = registry["profiles"][name]
        return self._read_record(name, entry, entry["history"][-1])

    def resolve(self, profile=None):
        with _redacted_errors():
            return self._resolve(self._load_registry(), profile)

    def list_profiles(self):
        with _redacted_errors():
            registry = self._load_registry()
            profiles = []
            for name in sorted(registry["profiles"]):
                resolved = self._resolve(registry, name)
                profiles.append({key: resolved[key] for key in ("profile", "profile_dir", "config_path", "fingerprint")})
                profiles[-1].update(config_id=resolved["config"]["config_id"], config_revision=resolved["config"]["config_revision"], is_default=name == registry["default_profile"])
            return {"ok": True, "registry_dir": str(self._root()), "default_profile": registry["default_profile"], "profiles": profiles}

    def history(self, profile=None):
        with _redacted_errors():
            registry = self._load_registry()
            name = self._selected(registry, profile)
            entry = registry["profiles"][name]
            current = self._resolve(registry, name)
            records = []
            for record in entry["history"]:
                historical = self._read_record(name, entry, record)
                if historical["config"]["config_id"] != current["config"]["config_id"] or historical["config"]["storage"]["state_dir"] != current["config"]["storage"]["state_dir"]:
                    _error("config_store_history_identity_changed")
                records.append(dict(record, config_path=historical["config_path"], current=record == entry["history"][-1]))
            return {"ok": True, "profile": name, "config_id": current["config"]["config_id"], "registry_dir": current["registry_dir"], "profile_dir": current["profile_dir"], "current_revision": current["config"]["config_revision"], "history": records}

    @contextmanager
    def _locked(self, create=False):
        root = self._root(create=create)
        if not root.exists():
            _error("config_store_no_profiles")
        path = root / ".registry.lock"
        _safe_path(path)
        flags = os.O_RDWR | os.O_NOFOLLOW | (os.O_CREAT if create else 0)
        fd = os.open(path, flags, 0o600)
        acquired = False
        try:
            _checked_file(fd)
            deadline = time.monotonic() + LOCK_WAIT_SECONDS
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        _error("config_store_busy; retry after the current operation finishes")
                    time.sleep(min(0.025, max(0, deadline - time.monotonic())))
            # A replaced lock pathname must not turn two writers into independent owners.
            if os.stat(path, follow_symlinks=False).st_ino != os.fstat(fd).st_ino:
                _error("config_store_lock_replaced")
            yield
        finally:
            if acquired:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    @contextmanager
    def locked_resolve(self, profile=None, expected_fingerprint=None):
        with _redacted_errors(), self._locked():
            resolved = self._resolve(self._load_registry(), profile)
            if expected_fingerprint is not None:
                if not isinstance(expected_fingerprint, str) or not HASH_RE.fullmatch(expected_fingerprint) or resolved["fingerprint"] != expected_fingerprint:
                    _error("config_store_stale_fingerprint")
            yield resolved

    def _write_snapshot(self, path, config):
        directory = _private_directory(path.parent, create=True)
        raw = _encoded(config)
        if _exists(path):
            if fingerprint(_read_json(path)) != fingerprint(config):
                _error("config_store_orphan_snapshot_conflict")
            return
        fd, temporary = tempfile.mkstemp(prefix=".snapshot-", dir=directory)
        try:
            with os.fdopen(fd, "wb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, path, follow_symlinks=False)
            except FileExistsError:
                if fingerprint(_read_json(path)) != fingerprint(config):
                    _error("config_store_orphan_snapshot_conflict")
            os.unlink(temporary)
            temporary = None
            _sync_directory(directory)
        finally:
            if temporary is not None:
                os.unlink(temporary)

    def _commit_registry(self, registry):
        """os.replace is the only commit point; no config pointer is written elsewhere."""
        root = self._root()
        raw = _encoded(registry)
        fd, temporary = tempfile.mkstemp(prefix=".registry-", dir=root)
        try:
            with os.fdopen(fd, "wb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            _sync_directory(root)
            os.replace(temporary, root / "registry.json")
            temporary = None
            try:
                _sync_directory(root)
            except OSError:
                # Replacement has already committed in this running system. Do not
                # claim the previous profile is active or attempt a blind rollback.
                _error("config_store_commit_outcome_uncertain; resolve/history before retrying")
        finally:
            if temporary is not None:
                os.unlink(temporary)

    def _prepare_save(self, registry, config, expected_revision, profile, profile_dir, make_default, force_new=False):
        config = _validate_config(config)
        expected_revision = _revision(expected_revision, minimum=0)
        _flag(make_default)
        if profile is None:
            name = config["config_id"] if expected_revision == 0 else self._selected(registry, None)
        else:
            name = _profile(profile)
        _profile(name)
        if name != config["config_id"]:
            _error("config_store_profile_must_equal_config_id")
        entry = registry["profiles"].get(name)
        if entry is None:
            if expected_revision != 0:
                _error("config_store_stale_revision")
            if len(registry["profiles"]) >= MAX_PROFILES or name.casefold() in {key.casefold() for key in registry["profiles"]}:
                _error("config_store_profile_limit_or_case_conflict")
            directory = self._profile_directory(name, profile_dir)
            for other in registry["profiles"].values():
                other_dir = Path(other["directory"])
                if directory == other_dir or directory in other_dir.parents or other_dir in directory.parents or str(directory).casefold() == str(other_dir).casefold():
                    _error("config_store_profile_directories_overlap")
            changes = _diff(None, config)
            changed_config = True
        else:
            current = self._resolve(registry, name)
            current_config = current["config"]
            if expected_revision != current_config["config_revision"] or config["config_revision"] != expected_revision:
                _error("config_store_stale_revision")
            if config["config_id"] != current_config["config_id"]:
                _error("config_store_config_id_immutable")
            if config["storage"]["state_dir"] != current_config["storage"]["state_dir"]:
                _error("config_store_state_directory_immutable; migration requires a separate workflow")
            registered = self._read_record(name, entry, entry["history"][0])["config"]
            if (config["storage"]["notes"] != current_config["storage"]["notes"] or
                    config["storage"]["notes"] != registered["storage"]["notes"]):
                # A restore is a new save too; historical preferences cannot
                # silently retarget a once-verified phone delivery channel.
                _error("config_store_notes_target_immutable; delivery migration requires a separate workflow")
            directory = Path(current["profile_dir"])
            if profile_dir is not None and self._profile_directory(name, profile_dir) != directory:
                _error("config_store_profile_directory_immutable")
            changes = _diff(current_config, config)
            changed_config = bool(changes) or force_new
            if changed_config:
                if len(entry["history"]) >= MAX_HISTORY:
                    _error("config_store_history_limit")
                config["config_revision"] += 1
                config = _validate_config(config)
        default_changed = make_default and registry["default_profile"] != name
        if default_changed:
            changes.append("default_profile")
        changed = changed_config or default_changed
        if force_new and not changes:
            changes = ["restored_revision"]
        record = {"revision": config["config_revision"], "fingerprint": fingerprint(config)}
        path = self._snapshot_path(directory, record)
        updated = copy.deepcopy(registry)
        if entry is None:
            updated["profiles"][name] = {"directory": str(directory), "history": [record]}
        elif changed_config:
            updated["profiles"][name]["history"].append(record)
        if make_default:
            updated["default_profile"] = name
        _encoded(updated)
        summary = {"ok": True, "status": "preview" if changed else "unchanged", "changed": changed, "config_revision": config["config_revision"], "config_path": str(path), "fingerprint": record["fingerprint"], "changed_fields": sorted(changes), "profile": name, "profile_dir": str(directory), "registry_dir": str(self._root())}
        return summary, config, updated, changed_config

    def _save(self, config, expected_revision, profile, profile_dir, make_default, apply):
        registry = self._load_registry()
        summary, saved_config, updated, changed_config = self._prepare_save(registry, config, expected_revision, profile, profile_dir, make_default)
        if apply and summary["changed"]:
            if changed_config:
                directory = Path(summary["profile_dir"])
                if directory.parent == self._root() / "profiles":
                    _private_directory(directory.parent, create=True)
                _private_directory(directory, create=True)
                self._write_snapshot(Path(summary["config_path"]), saved_config)
            self._commit_registry(updated)
            summary["status"] = "saved"
        return summary

    def save(self, config, expected_revision, profile=None, profile_dir=None, make_default=False, apply=False):
        with _redacted_errors():
            _flag(apply)
            if not apply:
                return self._save(config, expected_revision, profile, profile_dir, make_default, False)
            # Validate the request and intended locations before creating even the lock/root.
            self._prepare_save(self._load_registry(), config, expected_revision, profile, profile_dir, make_default)
            with self._locked(create=True):
                return self._save(config, expected_revision, profile, profile_dir, make_default, True)

    def use(self, profile, apply=False):
        with _redacted_errors():
            _flag(apply)
            def perform():
                registry = self._load_registry()
                resolved = self._resolve(registry, profile)
                changed = registry["default_profile"] != resolved["profile"]
                summary = {"ok": True, "status": "preview" if changed else "unchanged", "changed": changed, "profile": resolved["profile"], "default_profile": resolved["profile"], "registry_dir": resolved["registry_dir"]}
                if changed and apply:
                    registry["default_profile"] = resolved["profile"]
                    self._commit_registry(registry)
                    summary["status"] = "saved"
                return summary
            if not apply:
                return perform()
            with self._locked():
                return perform()

    def restore(self, revision, expected_revision, profile=None, apply=False):
        with _redacted_errors():
            _flag(apply)
            _revision(revision)
            _revision(expected_revision)
            def perform():
                registry = self._load_registry()
                name = self._selected(registry, profile)
                entry = registry["profiles"][name]
                current = self._resolve(registry, name)
                if expected_revision != current["config"]["config_revision"]:
                    _error("config_store_stale_revision")
                records = [record for record in entry["history"] if record["revision"] == revision]
                if len(records) != 1:
                    _error("config_store_history_revision_not_found")
                historical = self._read_record(name, entry, records[0])["config"]
                historical["config_revision"] = expected_revision
                summary, saved_config, updated, changed_config = self._prepare_save(registry, historical, expected_revision, name, None, False, force_new=revision != expected_revision)
                if apply and summary["changed"]:
                    if changed_config:
                        self._write_snapshot(Path(summary["config_path"]), saved_config)
                    self._commit_registry(updated)
                    summary["status"] = "saved"
                return summary
            if not apply:
                return perform()
            with self._locked():
                return perform()
