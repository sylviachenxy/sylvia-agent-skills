#!/usr/bin/env python3
"""Local entrypoint. Only publish --apply can create a Note; verify is explicit."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

from brief_core import ValidationError, build_package, resolve_windows, validate_config
from config_store import ConfigStore
import config_bindings

HERE = Path(__file__).resolve().parent
LIMIT = 1024 * 1024
STAGES = ("config", "offline", "sources", "local_publish", "iphone_read", "iphone_alarm", "timed_run")


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError("Duplicate JSON key")
        result[key] = value
    return result


def reject_constant(_):
    raise ValidationError("Non-finite JSON number")


def load_json(path):
    with open(path, "rb") as handle:
        data = handle.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise ValidationError("Input exceeds 1 MiB")
    return json.loads(data, object_pairs_hook=strict_object, parse_constant=reject_constant)


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def fingerprint(config):
    return hashlib.sha256(encoded(config)).hexdigest()


def private_dir(path, create=False):
    path = Path(path)
    if not path.is_absolute() or path in (Path("/"), Path.home(), Path("/tmp")):
        raise ValidationError("Use a dedicated absolute private state directory")
    for parent in (path, *path.parents):
        if (parent / ".git").exists():
            raise ValidationError("Generated user state must remain outside Git repositories")
        if parent.is_symlink():
            # macOS /tmp and /var are system aliases, not user-controlled state links.
            if parent not in (Path("/tmp"), Path("/var")):
                raise ValidationError("State paths must not traverse user-controlled symlinks")
    if not path.exists():
        if not create:
            return path
        path.mkdir(mode=0o700, parents=True, exist_ok=False)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValidationError("State directory must be user-owned with mode 0700")
    return path


def write_new_or_identical(path, data):
    """Never overwrite a conflicting version; publish atomically on one filesystem."""
    private_dir(path.parent, create=True)
    if path.is_symlink():
        raise ValidationError("State files must not be symlinks")
    if path.exists():
        if path.read_bytes() != data:
            raise ValidationError("Version already exists with different content; inspect before choosing a new revision")
        return
    fd, temporary = tempfile.mkstemp(prefix=".brief-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.is_symlink() or path.read_bytes() != data:
                raise ValidationError("Concurrent version conflict")
    finally:
        os.unlink(temporary)


def doctor():
    return {"ok": True, "python": list(sys.version_info[:3]), "platform": sys.platform,
            "tools": {name: shutil.which(name) is not None for name in ("osascript", "swift", "shortcuts")},
            "native_apps_contacted": False, "permissions_checked": False,
            "note": "Only availability checked; enabled-source and device qualification is separate."}


def make_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor")
    configuration = commands.add_parser("config", help="Persistent private profiles; mutations preview unless --apply")
    actions = configuration.add_subparsers(dest="config_command", required=True)
    for name in ("list", "show", "save", "use", "history", "restore", "status", "handoff", "acknowledge"):
        sub = actions.add_parser(name)
        sub.add_argument("--registry-dir", help="Dedicated registry override; normally use the fixed user-level default")
        if name != "list":
            sub.add_argument("--profile", required=name == "use")
        if name == "save":
            sub.add_argument("--input", required=True, help="Complete proposed configuration JSON, not a patch")
            sub.add_argument("--profile-dir", help="Custom directory for a new profile; registered at the fixed registry")
            sub.add_argument("--make-default", action="store_true")
        if name in ("save", "restore"):
            sub.add_argument("--expect-revision", type=int, required=True, help="0 means unregistered; otherwise the current saved revision")
        if name == "restore":
            sub.add_argument("--revision", type=int, required=True, help="Historical revision to restore as a new revision")
        if name == "status":
            sub.add_argument("--require-ready", action="store_true", help="Exit nonzero until both downstream bindings have matching evidence")
        if name == "acknowledge":
            sub.add_argument("--target", choices=("iphone", "automation"), required=True)
            sub.add_argument("--expect-fingerprint", required=True)
            sub.add_argument("--binding-id", required=True, help="Actual automation ID or device/shortcut locator")
            sub.add_argument("--evidence", required=True, help="Observed verification, not inferred from saving the configuration")
        if name in ("save", "use", "restore", "acknowledge"):
            sub.add_argument("--apply", action="store_true")
    for name in ("validate-config", "plan", "render", "publish", "verify", "setup-status", "checkpoint"):
        sub = commands.add_parser(name)
        selection = sub.add_mutually_exclusive_group()
        selection.add_argument("--profile", help="Resolve the current saved profile on every invocation")
        selection.add_argument("--config", help="Explicit JSON for offline preview or exact-version recovery; not live publication")
        sub.add_argument("--registry-dir", help="Registry override; omitted uses the fixed user-level default")
        if name == "plan":
            sub.add_argument("--date", required=True, help="Applicable local date YYYY-MM-DD")
        if name in ("render", "publish", "verify"):
            sub.add_argument("--candidate", required=True)
        if name == "publish":
            sub.add_argument("--apply", action="store_true", help="Explicit authorization to write this exact Notes target")
            sub.add_argument("--setup-test", action="store_true", help="Explicit one-off setup publication before downstream qualification; never for scheduled runs")
        if name == "checkpoint":
            sub.add_argument("--stage", choices=STAGES, required=True)
            sub.add_argument("--evidence", required=True, help="Concise observed result; no private source body")
    return parser


def deployment_after_save(store, result):
    """A committed config is durable even if a separate binding record is damaged."""
    if result.get("status") not in ("saved", "unchanged"):
        return {**result, "configuration_saved": False, "deployment_assessed": False}
    try:
        current = store.resolve(result["profile"])
        if current["fingerprint"] != result["fingerprint"]:
            return {**result, "configuration_saved": True, "deployment_assessed": False,
                    "deployment": {"deployment_ready": False, "error": "configuration_superseded; reload current profile"}}
        deployment = config_bindings.status(current)
    except (ValidationError, OSError, ValueError):
        deployment = {"ok": False, "deployment_ready": False, "error": "binding_state_requires_inspection"}
    return {**result, "configuration_saved": True, "deployment": deployment}


def configuration_command(args):
    store = ConfigStore(args.registry_dir)
    command = args.config_command
    if command == "list":
        return store.list_profiles()
    if command == "save":
        result = store.save(load_json(args.input), expected_revision=args.expect_revision,
                            profile=args.profile, profile_dir=args.profile_dir,
                            make_default=args.make_default, apply=args.apply)
        return deployment_after_save(store, result)
    if command == "use":
        return store.use(args.profile, apply=args.apply)
    if command == "history":
        return store.history(args.profile)
    if command == "restore":
        return deployment_after_save(store, store.restore(args.revision, args.expect_revision,
                                                          profile=args.profile, apply=args.apply))
    if command == "acknowledge" and args.apply:
        with store.locked_resolve(args.profile, expected_fingerprint=args.expect_fingerprint) as resolved:
            return config_bindings.acknowledge(resolved, args.target, args.expect_fingerprint,
                                              args.binding_id, args.evidence, apply=True)
    resolved = store.resolve(args.profile)
    if command == "show":
        return {"ok": True, **resolved}
    if command == "handoff":
        return config_bindings.handoff(resolved)
    if command == "acknowledge":
        return config_bindings.acknowledge(resolved, args.target, args.expect_fingerprint,
                                          args.binding_id, args.evidence, apply=False)
    result = config_bindings.status(resolved)
    if args.require_ready and not result["deployment_ready"]:
        result = {**result, "ok": False, "error": "configuration_not_deployed"}
    return result


def run(args):
    if args.command == "doctor":
        return doctor()
    if args.command == "config":
        return configuration_command(args)
    resolved = None
    if args.config:
        config = validate_config(load_json(args.config))
    else:
        resolved = ConfigStore(args.registry_dir).resolve(args.profile)
        config = resolved["config"]
    digest = fingerprint(config)
    if args.command == "validate-config":
        return {"ok": True, "config_id": config["config_id"], "config_fingerprint": digest,
                "configuration_managed": resolved is not None,
                "enabled_modules": [key for key, value in config["modules"].items() if value["enabled"]],
                "authorization_verified": False}
    if args.command == "plan":
        return {"ok": True, "windows": resolve_windows(config, args.date),
                "enabled_modules": [key for key, value in config["modules"].items() if value["enabled"]],
                "native_apps_contacted": False}
    root = private_dir(config["storage"]["state_dir"], create=False)
    if args.command in ("setup-status", "checkpoint"):
        directory = root / "setup" / digest
        if args.command == "checkpoint":
            evidence = args.evidence.strip()
            if not evidence or len(evidence) > 500 or any(ord(ch) < 32 for ch in evidence):
                raise ValidationError("Evidence must be one nonempty line of at most 500 characters")
            record = {"stage": args.stage, "evidence": evidence, "recorded_at": datetime.now(timezone.utc).isoformat(),
                      "config_fingerprint": digest, "kind": "operator_report_not_automatic_proof"}
            private_dir(root, create=True)
            private_dir(directory, create=True)
            # Append a new evidence record rather than overwrite earlier observations.
            target = directory / (args.stage + "-" + hashlib.sha256(encoded(record)).hexdigest()[:16] + ".json")
            write_new_or_identical(target, encoded(record))
        records = []
        if directory.exists():
            private_dir(directory)
            for item in sorted(directory.glob("*.json")):
                if item.is_symlink():
                    raise ValidationError("Checkpoint symlink is not allowed")
                record = load_json(item)
                if record.get("config_fingerprint") == digest and record.get("stage") in STAGES:
                    records.append(record)
        reported = {item["stage"] for item in records}
        deployment = config_bindings.status(resolved) if resolved else {"deployment_ready": False, "reason": "unmanaged_config"}
        phone = deployment.get("targets", {}).get("iphone", {})
        # Phone qualification belongs to the stable receiver, not to preferences.
        # Preserve the observed record; do not synthesize new checkpoint evidence.
        inherited = {"iphone_read", "iphone_alarm"} - reported if phone.get("status") == "verified" else set()
        return {"ok": True, "config_fingerprint": digest, "reported_stages": sorted(reported),
                "inherited_stages": sorted(inherited),
                "inheritance_basis": {"iphone_binding": phone} if inherited else {},
                "remaining": [stage for stage in STAGES if stage not in reported | inherited],
                "all_stages_reported": all(stage in reported for stage in STAGES),
                "deployment": deployment,
                "note": "Records are operator evidence, not automated proof; check the actual results."}
    package = build_package(config, load_json(args.candidate))
    if args.command == "render":
        private_dir(root, create=True)
        directory = root / "runs" / config["config_id"] / package["applicable_date"] / ("c" + str(package["config_revision"])) / ("r" + str(package["revision"]))
        private_dir(directory, create=True)
        write_new_or_identical(directory / "package.json", encoded(package))
        write_new_or_identical(directory / "brief.txt", package["body_text"].encode("utf-8"))
        return {"ok": True, "readiness": package["readiness"], "package_path": str(directory / "package.json"),
                "draft_path": str(directory / "brief.txt"), "notes_contacted": False}
    target = config["storage"]["notes"]
    request = {"authorized": True, "state_dir": str(root), "account": target["account"],
               "folder": target["folder"], "package": package}
    command = [sys.executable, str(HERE / "notes_publisher.py"), args.command]
    if args.command == "publish" and args.apply:
        if resolved is None:
            raise ValidationError("Register this configuration with config save before live publication; --config is for preview/recovery")
        command.append("--apply")
        # Keep the current registry revision stable across the external write.
        with ConfigStore(args.registry_dir).locked_resolve(resolved["profile"], expected_fingerprint=digest) as current:
            deployment = config_bindings.status(current)
            if not deployment.get("ok"):
                return {"ok": False, "error": "binding_state_requires_inspection", "deployment": deployment,
                        "notes_contacted": False}
            if not deployment["deployment_ready"] and not args.setup_test:
                return {"ok": False, "error": "configuration_not_deployed", "deployment": deployment,
                        "notes_contacted": False, "next": "Finish downstream verification, or explicitly authorize one setup-test publication"}
            private_dir(root, create=True)
            result = invoke_publisher(command, request)
            result.update({"deployment_ready": deployment["deployment_ready"], "setup_test": args.setup_test})
            return result
    if args.command == "publish" and args.setup_test:
        raise ValidationError("--setup-test is only meaningful with publish --apply")
    return invoke_publisher(command, request)


def invoke_publisher(command, request):
    completed = subprocess.run(command, input=encoded(request), stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, timeout=55, check=False)
    if len(completed.stdout) > LIMIT:
        raise ValidationError("Publisher response exceeds limit; outcome must be inspected")
    try:
        result = json.loads(completed.stdout, object_pairs_hook=strict_object, parse_constant=reject_constant)
    except (ValueError, UnicodeError):
        raise ValidationError("Publisher returned an invalid response; outcome may be uncertain") from None
    if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
        raise ValidationError("Publisher returned an invalid envelope")
    return result


def main(argv=None):
    args = make_parser().parse_args(argv)
    try:
        result = run(args)
    except subprocess.TimeoutExpired:
        result = {"ok": False, "error": "publisher_timeout", "outcome": "uncertain", "retry": "verify same candidate; do not create another revision"}
    except ValidationError as error:
        if str(error).startswith("config_store_commit_outcome_uncertain"):
            result = {"ok": False, "error": "configuration_commit_uncertain", "configuration_saved": None,
                      "retry": "Read config show/history to determine the current revision before retrying; do not blindly increment or restore"}
        else:
            result = {"ok": False, "error": "validation_error", "message": str(error)[:500]}
    except (ValueError, UnicodeError, OSError, RecursionError):
        # Avoid emitting file bodies, private paths or native exception descriptions.
        result = {"ok": False, "error": "validation_or_local_io_error", "message": "Check config, candidate, private directory and version conflicts; no raw exception is logged."}
    sys.stdout.buffer.write(encoded(result))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
