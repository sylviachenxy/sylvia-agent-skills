#!/usr/bin/env python3
"""Local IELTS practice records. No network, microphone, scoring model or Apple writes."""

import argparse
import copy
import fcntl
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SKILL = "ielts-speaking-coach"
CRITERIA = ("FC", "LR", "GRA", "P")
START = "<!-- ielts-speaking-coach:generated:start -->"
END = "<!-- ielts-speaking-coach:generated:end -->"
DEFAULTS = {
    "target_band": 8.0, "timezone": "Asia/Shanghai", "normal_minutes": 25,
    "minimum_minutes": 5, "feedback_language": "zh-CN", "interests": [],
    "daily_trigger": None, "exam_date": None, "store_quotes": True, "goal_id": None,
}


class StoreError(ValueError):
    pass


def check(condition, message):
    if not condition:
        raise StoreError(message)


def obj(value, required=(), optional=()):
    check(isinstance(value, dict), "Expected an object")
    check(set(required) <= set(value), "Missing fields: " + ", ".join(set(required) - set(value)))
    check(set(value) <= set(required) | set(optional), "Unknown fields: " + ", ".join(set(value) - set(required) - set(optional)))


def string(value, label="text", empty=False):
    check(isinstance(value, str) and len(value) <= 20000 and "\0" not in value, "Invalid " + label)
    check(empty or bool(value.strip()), "Empty " + label)


def identifier(value):
    check(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,79}", value), "Invalid identifier")
    return value


def number(value, low, high):
    check(type(value) in (int, float) and math.isfinite(value) and low <= value <= high, "Invalid number")


def iso_date(value):
    check(isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value), "Date must be YYYY-MM-DD")
    return date.fromisoformat(value)


def instant(value):
    string(value, "timestamp")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    check(result.utcoffset() is not None, "Timestamp needs a UTC offset")
    return result


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def read_json(path):
    raw = path.read_bytes()
    check(len(raw) <= 2 * 1024 * 1024, "JSON file too large")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            check(key not in result, "Duplicate JSON key")
            result[key] = value
        return result

    def invalid_constant(value):
        raise StoreError("Non-finite JSON number: " + value)

    return json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_constant)


def child(root, relative):
    relative = Path(relative)
    check(not relative.is_absolute() and ".." not in relative.parts, "Expected a relative path")
    result = root / relative
    cursor = root
    for component in relative.parts:
        cursor = cursor / component
        check(not cursor.is_symlink(), "Symlink in managed path")
    check(result.resolve().is_relative_to(root.resolve()), "Path escapes root")
    return result


def vault_path(value):
    raw = Path(value).expanduser()
    check(raw.is_absolute(), "Vault path must be absolute")
    vault = raw.resolve(strict=True)
    check(vault.is_dir() and (vault / ".obsidian").is_dir(), "Select an existing Obsidian vault")
    for parent in (vault, *vault.parents):
        check(not (parent / "SKILL.md").is_file(), "Cannot store learner data inside a skill")
        check(not (parent / "skills" / SKILL / "SKILL.md").is_file(), "Cannot store learner data in the skills repository")
    return vault


def atomic_write(path, raw, create=False, expected=None):
    check(not path.is_symlink(), "Refusing symlink output")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".ielts-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if create:
            os.link(temporary, path)
        else:
            if expected is not None:
                check(path.read_bytes() == expected, "File changed; reread before retrying")
            os.replace(temporary, path)
        check(path.read_bytes() == raw, "Write readback failed")
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def immutable(path, value):
    raw = encoded(value)
    if path.exists():
        check(path.read_bytes() == raw, "Immutable file conflict: " + path.name)
    else:
        atomic_write(path, raw, create=True)


def validate_settings(settings):
    obj(settings, DEFAULTS)
    number(settings["target_band"], 1, 9)
    check(settings["target_band"] * 2 == int(settings["target_band"] * 2), "Target uses half-band steps")
    ZoneInfo(settings["timezone"])
    for key in ("normal_minutes", "minimum_minutes"):
        check(type(settings[key]) is int, "Minutes must be integers")
        number(settings[key], 1, 120)
    check(settings["minimum_minutes"] <= settings["normal_minutes"], "Minimum exceeds normal session")
    string(settings["feedback_language"])
    check(type(settings["store_quotes"]) is bool, "store_quotes must be boolean")
    check(isinstance(settings["interests"], list) and len(settings["interests"]) <= 30, "Invalid interests")
    for item in settings["interests"]:
        string(item)
    if settings["daily_trigger"] is not None:
        string(settings["daily_trigger"])
    if settings["exam_date"] is not None:
        iso_date(settings["exam_date"])
    goal = settings["goal_id"]
    check(goal is None or isinstance(goal, str) and re.fullmatch(r"G-\d{4}-\d{3}", goal), "Invalid goal_id")


def validate_profile(profile, name):
    obj(profile, ("schema_version", "profile_id", "archive_id", "revision", "settings"))
    check(profile["schema_version"] == 1 and profile["profile_id"] == name, "Profile identity/schema conflict")
    identifier(profile["archive_id"])
    check(type(profile["revision"]) is int and profile["revision"] >= 1, "Invalid profile revision")
    validate_settings(profile["settings"])


def independent(attempt):
    return (attempt["modality"] in ("live_audio", "recording")
            and attempt["support"] == "none" and attempt["familiarity"] == "unseen"
            and attempt["kind"] in ("cold", "transfer"))


def validate_session(session):
    obj(session, ("schema_version", "session_id", "profile_revision", "occurred_at", "mode",
                  "status", "duration_minutes", "conditions", "attempts", "criteria",
                  "focus_next", "review_items", "review_results", "limitations"),
        ("goal_id", "action_id", "self_reflection", "repairs", "supersedes"))
    check(session["schema_version"] == 1, "Unknown session schema")
    identifier(session["session_id"])
    check(type(session["profile_revision"]) is int and session["profile_revision"] >= 1, "Invalid revision")
    instant(session["occurred_at"])
    check(session["mode"] in ("baseline", "mock", "daily", "drill", "recording_review", "review_update"), "Invalid mode")
    check(session["status"] in ("completed", "partial"), "Invalid status")
    number(session["duration_minutes"], 0, 240)
    if "supersedes" in session:
        identifier(session["supersedes"])
        check(session["supersedes"] != session["session_id"], "Cannot supersede self")
    if "goal_id" in session:
        check(isinstance(session["goal_id"], str) and re.fullmatch(r"G-\d{4}-\d{3}", session["goal_id"]), "Invalid goal_id")
    if "action_id" in session:
        check("goal_id" in session and isinstance(session["action_id"], str)
              and re.fullmatch(re.escape(session["goal_id"]) + r"-A\d{3}", session["action_id"]), "action_id needs matching goal_id")
    conditions = session["conditions"]
    obj(conditions, ("full_test", "uninterrupted", "timing_verified", "timekeeper", "timings", "source"))
    for key in ("full_test", "uninterrupted", "timing_verified"):
        check(type(conditions[key]) is bool, "Condition must be boolean")
    string(conditions["source"])
    check(conditions["timekeeper"] is None or isinstance(conditions["timekeeper"], str), "Invalid timekeeper")
    timings = conditions["timings"]
    obj(timings, (), ("part1", "part2", "part3", "part2_preparation", "part2_speech"))
    for value in timings.values():
        number(value, 0, 3600)
    check(isinstance(session["attempts"], list) and len(session["attempts"]) <= 150, "Invalid attempts")
    check(bool(session["attempts"]) or session["mode"] == "review_update", "No practice evidence")
    attempts = {}
    for attempt in session["attempts"]:
        obj(attempt, ("id", "part", "kind", "prompt_id", "topic", "prompt", "familiarity",
                      "support", "modality", "evidence", "evidence_kind"), ("audio_ref",))
        identifier(attempt["id"])
        identifier(attempt["prompt_id"])
        check(attempt["id"] not in attempts, "Duplicate attempt ID")
        attempts[attempt["id"]] = attempt
        check(type(attempt["part"]) is int and attempt["part"] in (1, 2, 3) or attempt["part"] == "micro", "Invalid part")
        check(attempt["kind"] in ("cold", "retry", "transfer", "drill"), "Invalid attempt kind")
        check(attempt["familiarity"] in ("unseen", "seen", "unknown"), "Invalid familiarity")
        check(attempt["support"] in ("none", "intent", "structure", "model"), "Invalid support")
        check(attempt["modality"] in ("live_audio", "recording", "transcript", "text"), "Invalid modality")
        check(attempt["evidence_kind"] in ("quote", "observation"), "Invalid evidence kind")
        for key in ("topic", "prompt", "evidence"):
            string(attempt[key])
        if "audio_ref" in attempt:
            string(attempt["audio_ref"])
            check(not Path(attempt["audio_ref"]).is_absolute() and "://" not in attempt["audio_ref"]
                  and ".." not in Path(attempt["audio_ref"]).parts, "Audio reference must be vault-relative")
    parts = [a["part"] for a in attempts.values()]
    full_parts = set(parts) == {1, 2, 3} and parts == sorted(parts, key=str)
    valid_timing = (
        set(timings) == {"part1", "part2", "part3", "part2_preparation", "part2_speech"}
        and 240 <= timings["part1"] <= 300 and 180 <= timings["part2"] <= 240
        and 240 <= timings["part3"] <= 300 and 59 <= timings["part2_preparation"] <= 61
        and 0 <= timings["part2_speech"] <= 120
        and timings["part2_preparation"] + timings["part2_speech"] <= timings["part2"] + 1
        and 660 <= sum(timings[k] for k in ("part1", "part2", "part3")) <= 840
    )
    strict = (session["mode"] in ("baseline", "mock") and session["status"] == "completed"
              and conditions["full_test"] and conditions["uninterrupted"] and full_parts
              and conditions["timing_verified"] and bool(conditions["timekeeper"])
              and valid_timing and all(independent(a) for a in attempts.values()))
    obj(session["criteria"], CRITERIA)
    for key, value in session["criteria"].items():
        obj(value, ("status", "evidence_ids", "note"), ("band_estimate",))
        check(value["status"] in ("observed", "partial", "gap", "not_observed"), "Invalid criterion status")
        check(isinstance(value["evidence_ids"], list), "evidence_ids must be a list")
        check(all(isinstance(i, str) and i in attempts for i in value["evidence_ids"]), "Unknown evidence ID")
        string(value["note"])
        if value["status"] != "not_observed":
            check(bool(value["evidence_ids"]), "Observed criterion needs evidence")
            if key in ("FC", "P"):
                check(all(attempts[i]["modality"] in ("live_audio", "recording")
                          for i in value["evidence_ids"]), "FC/P needs audible evidence")
        if "band_estimate" in value:
            bounds = value["band_estimate"]
            check(strict and value["status"] != "not_observed", "Band estimate requires strict full-test evidence")
            check({attempts[i]["part"] for i in value["evidence_ids"]} == {1, 2, 3},
                  "Band estimate needs evidence across all three parts")
            check(isinstance(bounds, list) and len(bounds) == 2, "Band estimate needs a range")
            for bound in bounds:
                number(bound, 1, 9)
                check(bound * 2 == int(bound * 2), "Band estimate uses half-band steps")
            check(bounds[0] <= bounds[1], "Reversed band interval")
    for key in ("focus_next", "limitations"):
        check(isinstance(session[key], list), "Expected a list")
        for value in session[key]:
            string(value)
    check(len(session["focus_next"]) <= 2, "Choose at most two priorities")
    if "self_reflection" in session:
        string(session["self_reflection"], empty=True)
    for repair in session.get("repairs", []):
        obj(repair, ("attempt_id", "original", "alternative", "reason"))
        check(repair["attempt_id"] in attempts, "Repair references unknown attempt")
        for key in ("original", "alternative", "reason"):
            string(repair[key])
    for key in ("review_items", "review_results"):
        check(isinstance(session[key], list), "Review fields must be lists")
    for item in session["review_items"]:
        obj(item, ("review_id", "criterion", "task", "due_date"))
        identifier(item["review_id"])
        check(item["criterion"] in CRITERIA, "Invalid review criterion")
        string(item["task"])
        iso_date(item["due_date"])
    for result in session["review_results"]:
        obj(result, ("review_id", "outcome", "reason"), ("attempt_id",))
        identifier(result["review_id"])
        string(result["reason"])
        check(result["outcome"] in ("independent_success", "supported", "needs_work", "not_tested", "retired"), "Invalid review outcome")
        if result["outcome"] in ("independent_success", "supported", "needs_work"):
            check(result.get("attempt_id") in attempts, "Review result needs an attempt")
        if "attempt_id" in result:
            check(result["attempt_id"] in attempts, "Unknown review attempt")
        if result["outcome"] == "independent_success":
            check(independent(attempts[result["attempt_id"]]), "Independent transfer requires unseen unassisted audio")
    if session["mode"] == "review_update":
        check(not attempts and session["duration_minutes"] == 0 and not session["review_items"]
              and bool(session["review_results"]), "review_update is a queue edit, not practice")
    if strict:
        return "strict_mock"
    if not attempts:
        return "queue_update"
    if all(a["modality"] in ("transcript", "text") for a in attempts.values()):
        return "text_practice"
    return "rehearsal" if session["mode"] in ("baseline", "mock") else "practice"


def active_records(records):
    superseded = {r["session"]["supersedes"] for r in records if "supersedes" in r["session"]}
    return sorted((r for r in records if r["session"]["session_id"] not in superseded),
                  key=lambda r: (instant(r["session"]["occurred_at"]), r["session"]["session_id"]))


def reviews(records):
    queue = {}
    for record in active_records(records):
        session = record["session"]
        local_day = record["local_date"]
        for item in session["review_items"]:
            key = item["review_id"]
            check(key not in queue, "Duplicate review ID")
            check(item["due_date"] >= local_day, "Review due date precedes its evidence")
            queue[key] = dict(item, origin=session["session_id"], last_outcome="pending", independent_dates=[])
        touched = set()
        for result in session["review_results"]:
            key = result["review_id"]
            check(key in queue and key not in touched, "Unknown or repeated review result")
            check(queue[key]["origin"] != session["session_id"], "Review an earlier session's item")
            touched.add(key)
            item = queue[key]
            outcome = result["outcome"]
            if outcome == "not_tested":
                continue
            item["last_outcome"] = outcome
            item["last_session_id"] = session["session_id"]
            item["reason"] = result["reason"]
            if outcome == "retired":
                item["due_date"] = None
            else:
                delay = 7 if outcome == "independent_success" else 2
                item["due_date"] = (iso_date(local_day) + timedelta(days=delay)).isoformat()
                if outcome == "independent_success" and local_day not in item["independent_dates"]:
                    item["independent_dates"].append(local_day)
    return list(queue.values())


def markdown(value):
    return str(value).replace("<", "&lt;").replace(">", "&gt;")


def managed_note(path, body, header):
    block = START + "\n" + body.rstrip() + "\n" + END
    if not path.exists():
        atomic_write(path, (header + "\n" + block + "\n\n## 我的补充\n\n").encode(), create=True)
        return
    previous = path.read_bytes()
    text = previous.decode()
    check(text.count(START) == 1 and text.count(END) == 1 and text.index(START) < text.index(END),
          "Managed note markers conflict: " + path.name)
    begin, end = text.index(START), text.index(END) + len(END)
    updated = text[:begin] + block + text[end:]
    if updated != text:
        atomic_write(path, updated.encode(), expected=previous)


class Store:
    def __init__(self, registry_dir=None):
        self.registry = Path(registry_dir).expanduser().resolve() if registry_dir else Path.home() / "Library/Application Support" / SKILL

    @contextmanager
    def locked(self):
        self.registry.mkdir(parents=True, exist_ok=True)
        with child(self.registry, ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def registry_data(self):
        path = child(self.registry, "registry.json")
        data = read_json(path) if path.exists() else {"schema_version": 1, "profiles": {}}
        obj(data, ("schema_version", "profiles"))
        check(data["schema_version"] == 1 and isinstance(data["profiles"], dict), "Invalid registry")
        return data

    def locate(self, name=None):
        data = self.registry_data()
        if name is None:
            check(len(data["profiles"]) == 1, "Choose --profile; zero or multiple profiles are registered")
            name = next(iter(data["profiles"]))
        identifier(name)
        check(name in data["profiles"], "Profile is not registered")
        entry = data["profiles"][name]
        vault = vault_path(entry["vault"])
        root = child(vault, "Learning/IELTS-Speaking/" + name)
        profile = read_json(child(root, "profile.json"))
        validate_profile(profile, name)
        check(profile["archive_id"] == entry["archive_id"], "Wrong archive; rebind the intended vault")
        return vault, root, profile

    def init(self, name, vault_value, zone):
        check(re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) and len(name) <= 40, "Use a short lowercase profile ID")
        vault = vault_path(vault_value)
        root = child(vault, "Learning/IELTS-Speaking/" + name)
        data = self.registry_data()
        if name in data["profiles"]:
            check(data["profiles"][name]["vault"] == str(vault), "Already bound elsewhere; use rebind")
        path = child(root, "profile.json")
        if path.exists():
            profile = read_json(path)
            validate_profile(profile, name)
        else:
            check(name not in data["profiles"], "Registered archive is missing; do not reinitialize")
            check(not root.exists() or not any(root.iterdir()), "Existing folder is not an initialized archive")
            settings = dict(DEFAULTS, timezone=zone)
            validate_settings(settings)
            profile = {"schema_version": 1, "profile_id": name, "archive_id": "IA-" + uuid.uuid4().hex,
                       "revision": 1, "settings": settings}
            immutable(child(root, "profile-history/000001.json"), profile)
            immutable(path, profile)
        if name in data["profiles"]:
            check(data["profiles"][name]["archive_id"] == profile["archive_id"], "Archive identity conflict")
        data["profiles"][name] = {"vault": str(vault), "archive_id": profile["archive_id"]}
        atomic_write(child(self.registry, "registry.json"), encoded(data))
        return {"profile": profile, "root": str(root), **self.rebuild(name)}

    def configure(self, name, patch, expected):
        _, root, profile = self.locate(name)
        obj(patch, (), DEFAULTS)
        check(profile["revision"] == expected, "Stale profile revision; read current preferences")
        updated = copy.deepcopy(profile)
        updated["settings"].update(patch)
        validate_settings(updated["settings"])
        if updated["settings"] == profile["settings"]:
            return {"changed": False, "profile": profile}
        updated["revision"] += 1
        immutable(child(root, f"profile-history/{updated['revision']:06d}.json"), updated)
        path = child(root, "profile.json")
        atomic_write(path, encoded(updated), expected=encoded(profile))
        try:
            views = self.rebuild(name)
        except (ValueError, OSError) as error:
            views = {"views_verified": False, "error": str(error), "recovery": "Preferences saved; run rebuild"}
        return {"changed": True, "profile": updated, **views}

    def rebind(self, name, value):
        data = self.registry_data()
        check(name in data["profiles"], "Unknown profile; use init on a new machine")
        vault = vault_path(value)
        root = child(vault, "Learning/IELTS-Speaking/" + identifier(name))
        profile = read_json(child(root, "profile.json"))
        validate_profile(profile, name)
        check(profile["archive_id"] == data["profiles"][name]["archive_id"], "Destination is not the same archive")
        data["profiles"][name]["vault"] = str(vault)
        atomic_write(child(self.registry, "registry.json"), encoded(data))
        return {"profile_id": name, "root": str(root)}

    def records(self, root):
        result = []
        for path in sorted(child(root, "sessions").glob("*.json")):
            path = child(root, "sessions/" + path.name)
            record = read_json(path)
            obj(record, ("schema_version", "session", "input_sha256", "evidence_grade", "local_date",
                         "settings_snapshot", "recorded_at"))
            session = record["session"]
            grade = validate_session(session)
            check(record["schema_version"] == 1 and path.stem == session["session_id"], "Record identity conflict")
            check(record["input_sha256"] == digest(session) and grade == record["evidence_grade"], "Record changed or inconsistent")
            validate_settings(record["settings_snapshot"])
            historical = read_json(child(root, f"profile-history/{session['profile_revision']:06d}.json"))
            check(record["settings_snapshot"] == historical["settings"], "Historical preferences conflict")
            day = instant(session["occurred_at"]).astimezone(ZoneInfo(record["settings_snapshot"]["timezone"])).date().isoformat()
            check(day == record["local_date"], "Local date conflict")
            result.append(record)
        ids = {r["session"]["session_id"] for r in result}
        replaced = []
        for record in result:
            old = record["session"].get("supersedes")
            if old:
                check(old in ids and old not in replaced, "Unknown or branched correction")
                replaced.append(old)
        # Follow correction chains to reject hand-edited cycles.
        parents = {r["session"]["session_id"]: r["session"].get("supersedes") for r in result}
        for key in parents:
            seen = set()
            while key:
                check(key not in seen, "Correction cycle")
                seen.add(key)
                key = parents[key]
        reviews(result)
        return result

    def archive(self, name, session):
        vault, root, profile = self.locate(name)
        grade = validate_session(session)
        path = child(root, "sessions/" + session["session_id"] + ".json")
        existing = self.records(root)
        if path.exists():
            check(read_json(path)["input_sha256"] == digest(session), "Session ID conflict; use a correction with supersedes")
            try:
                views = self.rebuild(name)
            except (ValueError, OSError) as error:
                views = {"views_verified": False, "error": str(error), "recovery": "Run rebuild; do not use a new session ID"}
            return {"already_archived": True, "receipt": str(path.with_suffix(".md").relative_to(vault)), **views}
        check(session["profile_revision"] <= profile["revision"], "Unknown future profile revision")
        captured = read_json(child(root, f"profile-history/{session['profile_revision']:06d}.json"))
        validate_profile(captured, profile["profile_id"])
        check(captured["archive_id"] == profile["archive_id"], "Profile history identity conflict")
        settings = captured["settings"]
        if not settings["store_quotes"] or not profile["settings"]["store_quotes"]:
            check(all(a["evidence_kind"] != "quote" for a in session["attempts"]) and not session.get("repairs"),
                  "store_quotes is false; save observations, not verbatim answers")
        if settings["goal_id"] is not None:
            check(session.get("goal_id") == settings["goal_id"], "Use the profile's confirmed Goal ID")
        if "supersedes" in session:
            check(session["supersedes"] in {r["session"]["session_id"] for r in active_records(existing)},
                  "Correction must replace an active record")
        lineage = set()
        parent = session.get("supersedes")
        by_id = {r["session"]["session_id"]: r for r in existing}
        while parent:
            lineage.add(parent)
            parent = by_id[parent]["session"].get("supersedes")
        prior = [r for r in existing if r["session"]["session_id"] not in lineage
                 and instant(r["session"]["occurred_at"]) <= instant(session["occurred_at"])]
        seen_ids = {a["prompt_id"] for r in prior for a in r["session"]["attempts"]}
        normalized = lambda value: " ".join(value.casefold().split())
        seen_text = {normalized(a["prompt"]) for r in prior for a in r["session"]["attempts"]}
        for attempt in session["attempts"]:
            if attempt["familiarity"] == "unseen":
                check(attempt["prompt_id"] not in seen_ids and normalized(attempt["prompt"]) not in seen_text,
                      "Previously exposed prompt cannot be marked unseen")
            seen_ids.add(attempt["prompt_id"])
            seen_text.add(normalized(attempt["prompt"]))
        for attempt in session["attempts"]:
            if "audio_ref" in attempt:
                ref = attempt["audio_ref"].split("#", 1)[0]
                check(bool(ref) and child(vault, ref).is_file(), "Audio reference does not exist in vault")
        local_day = instant(session["occurred_at"]).astimezone(ZoneInfo(settings["timezone"])).date().isoformat()
        record = {"schema_version": 1, "session": session, "input_sha256": digest(session),
                  "evidence_grade": grade, "local_date": local_day, "settings_snapshot": copy.deepcopy(settings),
                  "recorded_at": datetime.now(timezone.utc).isoformat()}
        reviews(existing + [record])
        immutable(path, record)
        try:
            views = self.rebuild(name)
        except (ValueError, OSError) as error:
            return {"archived": True, "views_verified": False, "session_id": session["session_id"],
                    "error": str(error), "recovery": "Run rebuild with the same profile; do not create another session ID"}
        return {"archived": True, "receipt": str(path.with_suffix(".md").relative_to(vault)), **views}

    def context(self, name, on=None):
        _, root, profile = self.locate(name)
        records = self.records(root)
        day = on or datetime.now(ZoneInfo(profile["settings"]["timezone"])).date().isoformat()
        iso_date(day)
        records = [r for r in records if r["local_date"] <= day]
        active = active_records(records)
        queue = reviews(records)
        practices = [r for r in active if r["session"]["attempts"]]
        baselines = [r for r in active if r["session"]["mode"] == "baseline"]
        full_tests = [r for r in active if r["evidence_grade"] == "strict_mock"]
        cutoff = iso_date(day) - timedelta(days=13)
        coverage = {str(part): 0 for part in (1, 2, 3)}
        for record in practices:
            if iso_date(record["local_date"]) >= cutoff:
                for part in {a["part"] for a in record["session"]["attempts"]
                             if a["modality"] in ("live_audio", "recording")}:
                    if str(part) in coverage:
                        coverage[str(part)] += 1
        return {
            "as_of": day, "profile": profile, "root": str(root),
            "recent_sessions": active[-6:],
            "comparison_samples": {"first_baseline": baselines[0] if baselines else None,
                                   "recent_strict_mocks": full_tests[-3:]},
            "spoken_part_coverage_last_14_days": coverage,
            "seen_prompts": [{"prompt_id": a["prompt_id"], "topic": a["topic"], "prompt": a["prompt"]}
                             for r in records for a in r["session"]["attempts"]],
            "due_reviews": sorted([q for q in queue if q["due_date"] and q["due_date"] <= day],
                                  key=lambda q: (q["due_date"], q["review_id"])),
            "all_reviews": queue,
            "counts": {"practice_sessions": len(practices), "strict_mocks": sum(r["evidence_grade"] == "strict_mock" for r in active),
                       "practice_days": len({r["local_date"] for r in practices}),
                       "audio_sessions": sum(any(a["modality"] in ("live_audio", "recording")
                                                 for a in r["session"]["attempts"]) for r in practices)},
            "interpretation": "Counts describe activity and conditions, not an IELTS score or mastery.",
        }

    def rebuild(self, name):
        vault, root, profile = self.locate(name)
        records = self.records(root)
        replaced = {r["session"].get("supersedes") for r in records}
        for record in records:
            session = record["session"]
            sid = session["session_id"]
            fields = {"schema_version": 1, "type": "ielts-speaking-evidence", "source_skill": SKILL,
                      "session_id": sid, "occurred_at": session["occurred_at"]}
            for key in ("goal_id", "action_id"):
                if key in session:
                    fields[key] = session[key]
            header = "---\n" + "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in fields.items()) + "\n---\n"
            lines = [f"# 口语训练 {sid}", "", f"日期：{record['local_date']} · 模式：{session['mode']} · 条件：{record['evidence_grade']}",
                     f"状态：{session['status']} · 实际用时：{session['duration_minutes']} 分钟",
                     "本回执是练习证据；不代表官方成绩。"]
            if sid in replaced:
                lines.append("此回执已被更正记录替代；不再计入当前进展。")
            if "supersedes" in session:
                lines.append(f"更正自：[[{root.relative_to(vault)}/sessions/{session['supersedes']}]]")
            if "goal_id" in session:
                gid = session["goal_id"]
                lines.append(f"Goal：[[Goals/{gid}/{gid}]]")
            lines += ["", "## 实际条件", markdown(json.dumps(session["conditions"], ensure_ascii=False)),
                      "", "## 原始尝试"]
            for a in session["attempts"]:
                lines += [f"### {a['id']} · Part {a['part']} · {a['kind']}",
                          f"{a['modality']} / {a['familiarity']} / support:{a['support']}",
                          "题目：" + markdown(a["prompt"]), "证据（" + a["evidence_kind"] + "）：" + markdown(a["evidence"])]
                if "audio_ref" in a:
                    lines.append("音频引用：" + markdown(a["audio_ref"]))
            lines += ["", "## 四维观察"]
            for key, value in session["criteria"].items():
                lines.append(f"- {key} · {value['status']} · {', '.join(value['evidence_ids'])}：{markdown(value['note'])}")
                if "band_estimate" in value:
                    lines.append(f"  非官方练习估计区间：{value['band_estimate']}")
            for repair in session.get("repairs", []):
                lines += ["", "原话：" + markdown(repair["original"]), "可选改法：" + markdown(repair["alternative"]),
                          "原因：" + markdown(repair["reason"])]
            if session.get("self_reflection"):
                lines += ["", "## 自评", markdown(session["self_reflection"])]
            lines += ["", "## 下次重点"] + ["- " + markdown(x) for x in session["focus_next"]]
            for item in session["review_items"]:
                lines.append(f"- {item['due_date']} · {item['review_id']}：{markdown(item['task'])}")
            for result in session["review_results"]:
                lines.append(f"- 复测 {result['review_id']} · {result['outcome']}：{markdown(result['reason'])}")
            lines += ["", "## 限制"] + ["- " + markdown(x) for x in session["limitations"]]
            managed_note(child(root, "sessions/" + sid + ".md"), "\n".join(lines), header)
        context = self.context(name)
        preferences = profile["settings"]
        lines = ["# 雅思口语复习台", "", f"训练目标：Speaking {preferences['target_band']}+",
                 f"通常练 {preferences['normal_minutes']} 分钟 · 忙时 {preferences['minimum_minutes']} 分钟",
                 f"本页更新日：{context['as_of']}；开始练习时会刷新。",
                 "对教练说“继续上次练习”即可开始；想改时间或反馈语言，直接告诉教练。",
                 "", "## 下次重点"]
        latest_practice = next((r for r in reversed(active_records(records)) if r["session"]["attempts"]), None)
        if latest_practice:
            lines += ["- " + markdown(item) for item in latest_practice["session"]["focus_next"]]
        else:
            lines.append("先做无提示基线；时间少时先取一个分项样本。")
        lines += ["", "## 到期复测"]
        for item in context["due_reviews"][:8]:
            lines.append(f"- {item['due_date']} · {item['review_id']}：{markdown(item['task'])}")
        if not context["due_reviews"]:
            lines.append("截至本页更新日，没有到期复测。")
        if len(context["due_reviews"]) > 8:
            lines.append(f"另有 {len(context['due_reviews']) - 8} 项到期，教练会按优先级选择，不需一次补完。")
        upcoming = sorted([item for item in context["all_reviews"]
                           if item["due_date"] and item["due_date"] > context["as_of"]],
                          key=lambda item: (item["due_date"], item["review_id"]))
        if upcoming:
            lines += ["", "## 接下来"]
            for item in upcoming[:5]:
                lines.append(f"- {item['due_date']}：{markdown(item['task'])}")
        lines += ["", "## 最近记录"]
        for record in reversed(active_records(records)[-14:]):
            sid = record["session"]["session_id"]
            lines.append(f"- [[{root.relative_to(vault)}/sessions/{sid}|{record['local_date']} {record['session']['mode']}]] · {record['evidence_grade']}")
        lines += ["", "## 活动覆盖", f"- 练习：{context['counts']['practice_sessions']} 次 / {context['counts']['practice_days']} 天",
                  f"- 其中含可听语音：{context['counts']['audio_sessions']} 次",
                  f"- 满足严格条件的全套样本：{context['counts']['strict_mocks']}",
                  "- 以上数量不能证明提分或稳定达到 8；需比较实际表现。"]
        managed_note(child(root, "Practice.md"), "\n".join(lines), "")
        return {"views_verified": True, "dashboard": str((root / "Practice.md").relative_to(vault))}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry-dir", help="Override machine registry (e.g. isolated tests)")
    parser.add_argument("--profile", help="Stable learner profile ID")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("profiles")
    init = sub.add_parser("init")
    init.add_argument("--vault", required=True)
    init.add_argument("--timezone", default="Asia/Shanghai")
    sub.add_parser("show")
    config = sub.add_parser("configure")
    config.add_argument("--input", required=True, help="JSON preference patch")
    config.add_argument("--expected-revision", required=True, type=int)
    rebind = sub.add_parser("rebind")
    rebind.add_argument("--vault", required=True)
    context = sub.add_parser("context")
    context.add_argument("--on", help="YYYY-MM-DD; defaults to profile's current local date")
    archive = sub.add_parser("archive")
    archive.add_argument("--input", required=True)
    sub.add_parser("rebuild")
    args = parser.parse_args(argv)
    store = Store(args.registry_dir)
    try:
        if args.command == "profiles":
            result = store.registry_data()
        else:
            with store.locked():
                if args.command == "init":
                    check(args.profile is not None, "init needs --profile")
                    result = store.init(args.profile, args.vault, args.timezone)
                elif args.command == "show":
                    vault, root, profile = store.locate(args.profile)
                    result = {"profile": profile, "vault": str(vault), "root": str(root)}
                elif args.command == "configure":
                    result = store.configure(args.profile, read_json(Path(args.input)), args.expected_revision)
                elif args.command == "rebind":
                    result = store.rebind(args.profile, args.vault)
                elif args.command == "context":
                    result = store.context(args.profile, args.on)
                elif args.command == "archive":
                    result = store.archive(args.profile, read_json(Path(args.input)))
                else:
                    result = store.rebuild(args.profile)
        print(json.dumps({"ok": True, **result}, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
