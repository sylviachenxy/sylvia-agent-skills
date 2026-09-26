"""Read-only structure audit, including formal notes skipped by the material scan."""

from __future__ import annotations

import argparse
from datetime import date
import json
import os
from pathlib import Path
import sys

sys.dont_write_bytecode = True
from scan_materials import (component, known_courses, load_layout, load_ledger,
                            managed_log, relative, scoped_path)


def audit(layout, courses=()):
    root = layout["root"]
    ledger = load_ledger(root)
    courses = known_courses(layout, ledger, courses)
    issues, outputs, expected = [], {}, {}
    sources = {item["path"] for lesson in ledger["lessons"] for item in lesson["materials"]}
    sources.update(item["path"] for item in ledger.get("references", []))

    def issue(path, reason, **details):
        issues.append({"path": path, "reason": reason, **details})

    def is_schedule(name):
        return bool(layout["schedule"] and relative(name).is_relative_to(relative(layout["schedule"])))

    for course in courses:
        for folder in (layout["materials"], layout["notes"]):
            name = f"{course}/{folder}"
            try:
                if not scoped_path(root, name).is_dir():
                    issue(name, "missing_standard_directory")
            except (OSError, ValueError) as error:
                issue(name, "unsafe_directory", detail=str(error))
        name = f"{course}/{layout['materials']}/log.md"
        try:
            path = scoped_path(root, name)
            if not path.exists():
                issue(name, "missing_course_log")
            elif not managed_log(path):
                issue(name, "unmanaged_log_conflict")
        except (OSError, ValueError) as error:
            issue(name, "unavailable_log", detail=str(error))

    for lesson in ledger["lessons"]:
        for output in lesson["outputs"]:
            name = output["path"]
            outputs.setdefault(name, []).append(output["role"])
            if output["role"] != "notes":
                continue
            try:
                day = lesson.get("lecture_date", "")
                if date.fromisoformat(day).isoformat() != day:
                    raise ValueError("Lecture date must use YYYY-MM-DD")
                course_name = component(lesson.get("course_name") or lesson["course_folder"])
                target = f"{lesson['course_folder']}/{layout['notes']}/{course_name}_{day}.md"
                expected.setdefault(target, []).append(name)
                if name != target:
                    issue(name, "noncanonical_main_note", expected=target)
                    if scoped_path(root, target).exists():
                        issue(target, "rename_target_exists")
            except (TypeError, ValueError) as error:
                issue(name, "unconfirmed_lecture_date_or_name", detail=str(error))
    for target, names in expected.items():
        if len(names) > 1:
            issue(target, "multiple_main_notes_for_one_target", sources=names)
    for name in sorted(sources | outputs.keys()):
        try:
            if not scoped_path(root, name).is_file():
                issue(name, "registered_current_path_missing")
        except (OSError, ValueError) as error:
            issue(name, "registered_path_unsafe", detail=str(error))

    for directory, dirs, files in os.walk(root, followlinks=False,
                                          onerror=lambda error: issue(str(error.filename), "unreadable_directory")):
        parent = Path(directory)
        kept = []
        for name in sorted(dirs):
            child = parent / name
            rel = child.relative_to(root).as_posix()
            if child.is_symlink():
                issue(rel, "symlink_not_followed")
            elif not name.startswith(".") and not is_schedule(rel):
                if parent == root and name not in courses:
                    issue(rel, "unclassified_root_directory")
                kept.append(name)
        dirs[:] = kept
        for filename in sorted(files):
            if filename.startswith("."):
                continue
            path = parent / filename
            name = path.relative_to(root).as_posix()
            if path.is_symlink():
                issue(name, "symlink_not_followed")
                continue
            if is_schedule(name):
                continue
            parts = relative(name).parts
            if len(parts) < 3 or parts[0] not in courses or parts[1] not in {layout["materials"], layout["notes"]}:
                issue(name, "outside_standard_directories")
            elif parts[1] == layout["notes"]:
                if name in sources or path.suffix.lower() != ".md":
                    issue(name, "source_in_formal_notes")
                elif name not in outputs:
                    issue(name, "unregistered_formal_file_review_role")
            elif name in outputs:
                issue(name, "output_in_materials")
    return {"read_only": True, "courses": courses, "issues": issues,
            "layout_pass": not issues, "semantic_role_review_required": True,
            "content_completion_independent": True}


def main():
    profile = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=profile / "class-notes/config.toml")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--course", action="append", default=[])
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    try:
        result = audit(load_layout(args.config, args.root), args.course)
    except (OSError, ValueError) as error:
        print(json.dumps({"error": str(error), "read_only": True}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if args.strict and result["issues"] else 0


if __name__ == "__main__":
    sys.exit(main())
