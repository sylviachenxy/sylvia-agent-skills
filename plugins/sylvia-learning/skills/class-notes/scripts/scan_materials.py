"""Read-only classroom inventory. Hints require content review; never move files."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tomllib
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


EXTENSIONS = {
    ".md", ".txt", ".pdf", ".ppt", ".pptx", ".doc", ".docx", ".rtf", ".epub",
    ".m4a", ".mp3", ".wav", ".aac", ".flac", ".ogg", ".opus", ".aiff",
    ".mp4", ".mov", ".webm", ".jpg", ".jpeg", ".png", ".heic", ".webp",
}
PRIORITY = {"needs_update": 0, "needs_repair": 1, "blocked": 2,
            "partial": 3, "needs_archiving": 4, "complete": 5}
LOG_MARKER = "<!-- class-notes:course-log v1 -->"
LOG_START = "<!-- class-notes:generated:start -->"
LOG_END = "<!-- class-notes:generated:end -->"


def managed_log(path):
    if path.is_symlink() or not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    return (text.startswith(LOG_MARKER + "\n") and text.count(LOG_START) == 1
            and text.count(LOG_END) == 1 and text.index(LOG_START) < text.index(LOG_END))


def known_courses(layout, ledger, explicit=()):
    courses = {component(name) for name in explicit}
    courses.update(item["course_folder"] for item in ledger["lessons"])
    courses.update(component(item["course_folder"]) for item in ledger.get("references", [])
                   if item.get("course_folder"))
    for path in layout["root"].iterdir():
        if path.name.startswith(".") or path.is_symlink() or not path.is_dir():
            continue
        if any((path / name).is_dir() for name in (layout["materials"], layout["notes"])):
            courses.add(component(path.name))
    return sorted(courses)


def component(value):
    if (not isinstance(value, str) or not value or value.startswith(".")
            or "/" in value or "\\" in value or "\0" in value):
        raise ValueError("Directory names must be single non-hidden components")
    return value


def relative(value):
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        raise ValueError("Record paths must be nonempty relative POSIX paths")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path == PurePosixPath("."):
        raise ValueError("Record path escapes the classroom root")
    return path


def scoped_path(root, name):
    parts = relative(name).parts
    path = root
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise ValueError("Symbolic links are not followed")
    if not path.resolve().is_relative_to(root):
        raise ValueError("Path escapes the classroom root")
    return path


def load_layout(config_path, root_override=None):
    with Path(config_path).open("rb") as handle:
        config = tomllib.load(handle)
    if config.get("schema_version") != 1:
        raise ValueError("Unsupported configuration schema_version")
    storage = config.get("storage", {})
    if not isinstance(storage, dict):
        raise ValueError("storage must be a TOML table")
    root_value = root_override or storage.get("class_root")
    if not root_value:
        raise ValueError("No classroom root configured; supply the user's chosen root")
    if not isinstance(root_value, (str, Path)):
        raise ValueError("class_root must be a path")
    root = Path(root_value).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("class_root must be a directory")
    materials = component(storage.get("materials_subdir", "笔记素材"))
    notes = component(storage.get("notes_subdir", "正式课堂笔记"))
    if materials == notes:
        raise ValueError("Materials and notes directories must differ")
    archive = storage.get("archive_materials", False)
    if type(archive) is not bool:
        raise ValueError("archive_materials must be boolean")
    if storage.get("batch_selection", "all-pending") != "all-pending":
        raise ValueError("Unsupported batch_selection")
    matching = config.get("matching", {})
    if not isinstance(matching, dict):
        raise ValueError("matching must be a TOML table")
    schedule = matching.get("schedule_dir")
    if schedule is not None:
        schedule = relative(schedule).as_posix()
    zone_name = matching.get("timezone", "UTC")
    if not isinstance(zone_name, str):
        raise ValueError("matching.timezone must be an IANA timezone string")
    try:
        zone = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError("Unknown matching.timezone") from error
    return {"root": root, "materials": materials, "notes": notes, "archive": archive,
            "schedule": schedule, "timezone": zone, "timezone_name": zone_name}


def digest(path):
    before = path.stat()
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    after = path.stat()
    identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
    if identity(before) != identity(after):
        raise ValueError("File changed while reading; retry this material later")
    return hasher.hexdigest(), after.st_size, after


def file_times(stat_result, zone=timezone.utc):
    birth = getattr(stat_result, "st_birthtime", None)
    render = lambda timestamp: datetime.fromtimestamp(timestamp, zone).isoformat(timespec="seconds")
    return {"created_at": render(birth) if birth is not None else None,
            "modified_at": render(stat_result.st_mtime),
            "basis": "filesystem birthtime and mtime, not verified lecture time"}


def validate_source(source):
    if not isinstance(source, dict):
        raise ValueError("Material/reference record must be an object")
    relative(source.get("path"))
    if "original_path" in source:
        relative(source["original_path"])
    if not re.fullmatch(r"[0-9a-f]{64}", str(source.get("sha256", ""))):
        raise ValueError("Material/reference record needs a real SHA-256")


def load_ledger(root):
    path = scoped_path(root, ".class-notes/processing.json")
    if not path.exists():
        return {"schema_version": 1, "lessons": [], "references": []}
    ledger = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(ledger, dict) or ledger.get("schema_version") != 1:
        raise ValueError("Invalid processing record schema; preserve it for repair")
    if not isinstance(ledger.get("lessons"), list) or not isinstance(ledger.get("references", []), list):
        raise ValueError("Processing record needs lesson/reference arrays")
    seen = set()
    for lesson in ledger["lessons"]:
        if not isinstance(lesson, dict):
            raise ValueError("Lesson record must be an object")
        identifier = lesson.get("lesson_id")
        if not isinstance(identifier, str) or not identifier or identifier in seen:
            raise ValueError("Missing or duplicate lesson_id")
        seen.add(identifier)
        component(lesson.get("course_folder"))
        if lesson.get("status") not in {"draft", "partial", "blocked", "complete"}:
            raise ValueError("Unknown lesson status")
        if lesson.get("archive_status") not in {"pending", "blocked", "complete"}:
            raise ValueError("Unknown archive status")
        if type(lesson.get("requires_transcript")) is not bool:
            raise ValueError("Each lesson must declare requires_transcript")
        if "processing_started" in lesson and type(lesson["processing_started"]) is not bool:
            raise ValueError("processing_started must be boolean when supplied")
        if lesson.get("processing_started") is False and lesson["status"] in {"partial", "complete"}:
            raise ValueError("Started/completed work cannot be recorded as not started")
        if not isinstance(lesson.get("materials"), list) or not isinstance(lesson.get("outputs"), list):
            raise ValueError("Lesson needs materials and outputs arrays")
        if lesson["status"] == "complete" and not lesson["materials"]:
            raise ValueError("A completed lesson needs source records")
        for source in lesson["materials"]:
            validate_source(source)
            if source.get("coverage") not in {"full", "partial"}:
                raise ValueError("Unknown material coverage")
        for output in lesson["outputs"]:
            if not isinstance(output, dict):
                raise ValueError("Output record must be an object")
            relative(output.get("path"))
            if not isinstance(output.get("role"), str) or type(output.get("verified")) is not bool:
                raise ValueError("Output needs a role and a boolean verified field")
    for source in ledger.get("references", []):
        validate_source(source)
        if not isinstance(source.get("reason"), str) or not source["reason"].strip():
            raise ValueError("Reference exclusions need a reason")
    return ledger


def outputs_valid(lesson, layout):
    required = {"notes", "transcript"} if lesson["requires_transcript"] else {"notes"}
    valid = {}
    for output in lesson["outputs"]:
        if output.get("verified") is not True:
            continue
        parts = relative(output["path"]).parts
        if len(parts) < 3 or parts[:2] != (lesson["course_folder"], layout["notes"]):
            continue
        try:
            path = scoped_path(layout["root"], output["path"])
            if path.suffix.lower() == ".md" and path.is_file() and path.stat().st_size > 0:
                valid.setdefault(output["role"], set()).add(output["path"])
        except (OSError, ValueError):
            continue
    if not required <= valid.keys():
        return False
    if lesson["requires_transcript"]:
        return any(note != transcript for note in valid["notes"] for transcript in valid["transcript"])
    return True


def source_present(source, layout):
    for name in (source["path"], source.get("original_path")):
        if name is None:
            continue
        try:
            if scoped_path(layout["root"], name).is_file():
                return True
        except (OSError, ValueError):
            pass
    return False


def classify(name, fingerprint, ledger, layout):
    exact, same_content = [], []
    for lesson in ledger["lessons"]:
        for source in lesson["materials"]:
            if name in {source["path"], source.get("original_path")}:
                exact.append((lesson, source))
            elif fingerprint == source["sha256"]:
                same_content.append(lesson["lesson_id"])
    states = []
    for lesson, source in exact:
        if fingerprint != source["sha256"]:
            state = "needs_update"
        elif lesson["status"] == "blocked":
            state = "blocked"
        elif lesson["status"] != "complete" or source["coverage"] != "full":
            state = "partial"
        elif not outputs_valid(lesson, layout) or not all(source_present(s, layout) for s in lesson["materials"]):
            state = "needs_repair"
        elif layout["archive"] and (lesson["archive_status"] != "complete"
                or relative(name).parts[:2] != (lesson["course_folder"], layout["materials"])):
            state = "needs_archiving"
        else:
            state = "complete"
        states.append(state)
    if states:
        return min(states, key=PRIORITY.get), sorted({lesson["lesson_id"] for lesson, _ in exact})
    if same_content:
        return "duplicate_review", sorted(set(same_content))
    for source in ledger.get("references", []):
        if source["path"] == name and source["sha256"] == fingerprint:
            return "reference", []
    return "pending_review", []


def scan(layout):
    root = layout["root"]
    ledger = load_ledger(root)
    materials, contexts, issues, same_hash = [], [], [], {}
    if layout["schedule"]:
        try:
            if not scoped_path(root, layout["schedule"]).is_dir():
                issues.append({"path": layout["schedule"], "reason": "schedule_directory_missing"})
        except (OSError, ValueError) as error:
            issues.append({"path": layout["schedule"], "reason": "schedule_unavailable: " + str(error)})
    for lesson in ledger["lessons"]:
        for source in lesson["materials"]:
            if not source_present(source, layout):
                issues.append({"path": source["path"], "lesson_id": lesson["lesson_id"],
                               "reason": "registered_source_missing_or_unsafe; check rename or move"})
    def walk_error(error):
        issues.append({"path": str(error.filename), "reason": "unreadable_directory"})
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=walk_error):
        parent = Path(directory)
        kept = []
        for name in sorted(dirs):
            child = parent / name
            if child.is_symlink():
                issues.append({"path": child.relative_to(root).as_posix(), "reason": "symlink_not_followed"})
            elif not name.startswith(".") and name != layout["notes"]:
                kept.append(name)
        dirs[:] = kept
        for filename in sorted(files):
            path = parent / filename
            name = path.relative_to(root).as_posix()
            if filename.startswith("."):
                continue
            if path.is_symlink():
                issues.append({"path": name, "reason": "symlink_not_followed"})
                continue
            if path.suffix.lower() not in EXTENSIONS:
                issues.append({"path": name, "reason": "unsupported_type_review_manually"})
                continue
            try:
                safe = scoped_path(root, name)
                if not safe.is_file():
                    issues.append({"path": name, "reason": "not_a_regular_file"})
                    continue
                parts = relative(name).parts
                if (len(parts) == 3 and parts[1:] == (layout["materials"], "log.md")
                        and managed_log(safe)):
                    continue
                fingerprint, size, observed_stat = digest(safe)
                times = file_times(observed_stat, layout["timezone"])
                if layout["schedule"] and relative(name).is_relative_to(relative(layout["schedule"])):
                    contexts.append({"path": name, "role": "schedule", "sha256": fingerprint,
                                     "size_bytes": size, "timestamps": times})
                    continue
                status, lessons = classify(name, fingerprint, ledger, layout)
            except (OSError, ValueError) as error:
                issues.append({"path": name, "reason": str(error)})
                continue
            parts = relative(name).parts
            materials.append({"path": name, "sha256": fingerprint, "size_bytes": size,
                              "timestamps": times,
                              "course_hint": parts[0] if len(parts) > 1 else None,
                              "status": status, "related_lessons": lessons})
            same_hash.setdefault(fingerprint, []).append(name)
    return {"schema_version": 1, "root": str(root), "read_only": True,
            "scope": "all-pending; no filesystem-date filtering",
            "materials": materials, "context_files": contexts, "timezone": layout["timezone_name"],
            "counts": dict(Counter(item["status"] for item in materials)),
            "duplicate_groups": [names for names in same_hash.values() if len(names) > 1],
            "issues": issues, "content_review_required": True}


def main():
    profile = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=profile / "class-notes/config.toml")
    parser.add_argument("--root", type=Path, help="User-authorized root for this read-only scan")
    args = parser.parse_args()
    try:
        result = scan(load_layout(args.config, args.root))
    except (OSError, ValueError) as error:
        print(json.dumps({"error": str(error), "read_only": True}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
