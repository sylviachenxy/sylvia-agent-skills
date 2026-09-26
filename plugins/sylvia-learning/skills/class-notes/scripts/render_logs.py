"""Preview course log.md projections; --write updates only marked log regions."""

from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import sys
import tempfile
from urllib.parse import quote

sys.dont_write_bytecode = True
from scan_materials import (LOG_END, LOG_MARKER, LOG_START, digest, known_courses,
                            load_layout, load_ledger, managed_log, outputs_valid,
                            relative, scan, scoped_path)


def plain(value):
    if isinstance(value, (list, dict)):
        value = json.dumps(value, ensure_ascii=False)
    return str(value).replace("\r", " ").replace("\n", " ").replace("|", "\\|").replace("<", "&lt;").replace(">", "&gt;")


def link(name, log_dir, label=None):
    target = os.path.relpath(name, log_dir)
    label = plain(label or relative(name).name).replace("[", "\\[").replace("]", "\\]")
    return f"[{label}]({quote(target, safe='/')})"


def integrity_issues(lesson, layout):
    problems = []
    if not outputs_valid(lesson, layout):
        problems.append("正式笔记或必需完整听写尚未验收／文件缺失")
    for source in lesson["materials"]:
        try:
            if digest(scoped_path(layout["root"], source["path"]))[0] != source["sha256"]:
                problems.append("来源内容已变化，需要重新核对覆盖")
        except (OSError, ValueError):
            problems.append("已登记来源的当前路径缺失或不可读")
        if source["coverage"] != "full":
            problems.append("来源尚未完整覆盖")
    for output in lesson["outputs"]:
        if output.get("verified") and output.get("sha256"):
            try:
                if digest(scoped_path(layout["root"], output["path"]))[0] != output["sha256"]:
                    problems.append("已验收输出内容已变化，需要复核")
            except (OSError, ValueError):
                problems.append("已验收输出缺失或不可读")
    return list(dict.fromkeys(problems))


def content_status(lesson, layout):
    if lesson["status"] == "complete":
        problems = integrity_issues(lesson, layout)
        return ("incomplete", problems) if problems else ("complete", [])
    if lesson.get("processing_started") is False:
        return "pending", []
    output_exists = any(scoped_path(layout["root"], output["path"]).is_file() for output in lesson["outputs"])
    if (lesson["status"] in {"partial", "blocked"} or lesson.get("processing_started") is True
            or lesson.get("work_started_at") or output_exists):
        return "incomplete", integrity_issues(lesson, layout)
    return "待核定", ["旧记录没有足够的开始处理证据，需核定而非直接认定未开始"]


def render_course(course, layout, ledger, inventory):
    log_dir = f"{course}/{layout['materials']}"
    lessons = [item for item in ledger["lessons"] if item["course_folder"] == course]
    entries = [(item, *content_status(item, layout)) for item in lessons]
    counts = Counter(state for _, state, _ in entries)
    lines = [f"# {plain(course)} · 素材整理日志", "",
             f"记录更新时间：{plain(ledger.get('updated_at', '未记录'))}", "",
             "本页由统一处理记录生成；只在“手工备注”区记补充，不直接改自动状态。",
             "complete＝内容已验收；incomplete＝已开始但未完成；pending＝明确尚未开始；待核定＝证据不足。素材归位不等于内容完成。", "",
             "课次统计：" + "；".join(f"{key} {counts[key]}" for key in ("complete", "incomplete", "pending", "待核定")) + "。", "",
             "## 课次整理", ""]
    if not entries:
        lines.extend(["尚无已登记课次；以下参考／占位文件不自动算作待整理的一节课。", ""])
    for lesson, state, problems in entries:
        placed = all(relative(item["path"]).parts[:2] == (course, layout["materials"])
                     and scoped_path(layout["root"], item["path"]).is_file() for item in lesson["materials"])
        lines.extend([f"### {plain(lesson.get('lecture_date') or '日期待核定')} — {state}", "",
                      f"- 课次标识：`{plain(lesson['lesson_id'])}`",
                      f"- 素材归位：{'complete' if placed and lesson['archive_status'] == 'complete' else '尚未通过'}；内容状态单独判断。"])
        done = lesson.get("completed_scope") or lesson.get("scope_note") or ("已通过登记范围内的验收；不表示未提供教材或全考试已覆盖。" if state == "complete"
                                                     else "见下方已保存成果；未验收部分不可当作完整成稿。")
        lines.append("- 已完成部分：" + plain(done))
        actions = lesson.get("pending_actions") or []
        if not isinstance(actions, list):
            actions = [actions]
        if state == "incomplete":
            specific = lesson.get("incomplete_reasons") or lesson.get("course_match", {}).get("unresolved", [])
            if not isinstance(specific, list):
                specific = [specific]
            reasons = list(dict.fromkeys([plain(item) for item in specific] + problems))
            lines.append("- 未完成原因／缺口：")
            for reason in reasons or ["记录表明已开始但未完成；具体原因尚未登记，需补记。"]:
                lines.append("  - " + reason)
            lines.append("- 下一步：")
            for action in actions or ["核对上述缺口、补全处理记录，再验收内容。"]:
                lines.append("  - " + plain(action))
        elif state == "pending":
            lines.append("- 下一步：尚未开始，按已确认范围开展整理。")
        elif state == "待核定":
            lines.append("- 下一步：核查既有内容及处理证据，确认是否已经开始。")
        else:
            lines.append("- 下一步：本次登记范围无未完成事项；有新增来源时增量复核。")
        lines.append("- 已保存成果：")
        if not lesson["outputs"]:
            lines.append("  - 尚无。")
        for output in lesson["outputs"]:
            lines.append("  - " + link(output["path"], log_dir) + f"（{plain(output['role'])}；{'已验收' if output['verified'] else '未验收'}）")
        lines.append("- 素材：")
        for source in lesson["materials"]:
            lines.append("  - " + link(source["path"], log_dir) + f"（覆盖：{source['coverage']}）")
        lines.append("")
    references = [item for item in ledger.get("references", [])
                  if item.get("course_folder") == course or relative(item["path"]).parts[0] == course]
    lines.extend(["## 参考资料／占位", ""])
    for item in references:
        label = item.get("display_name") or relative(item["path"]).name
        lines.append("- " + link(item["path"], log_dir, label) + "：" + plain(item["reason"]))
    if not references:
        lines.append("暂无单独登记的参考／占位资料。")
    lines.extend(["", "## 未登记或需复核的材料", ""])
    unknown = [item for item in inventory["materials"] if relative(item["path"]).parts[0] == course
               and item["status"] in {"pending_review", "duplicate_review", "needs_update"}]
    for item in unknown:
        lines.append("- " + link(item["path"], log_dir) + f"：待核定（{item['status']}），不自动当作未开始。")
    if not unknown:
        lines.append("本次扫描未发现此类材料。")
    return "\n".join(lines) + "\n"


def build_logs(layout, courses=(), write=False):
    root = layout["root"]
    ledger = load_ledger(root)
    inventory = scan(layout)
    results, plans = [], []
    for course in known_courses(layout, ledger, courses):
        name = f"{course}/{layout['materials']}/log.md"
        path = scoped_path(root, name)
        if not path.parent.is_dir():
            raise ValueError(f"Create the confirmed materials directory first: {name}")
        previous = path.read_text(encoding="utf-8") if path.exists() else None
        if previous is not None and not managed_log(path):
            raise ValueError(f"Existing log.md is not a managed log; refusing overwrite: {name}")
        body = render_course(course, layout, ledger, inventory)
        region = LOG_START + "\n" + body + LOG_END
        if previous is None:
            updated = LOG_MARKER + "\n" + region + "\n\n## 手工备注\n\n"
        else:
            updated = previous[:previous.index(LOG_START)] + region + previous[previous.index(LOG_END) + len(LOG_END):]
        plans.append((path, previous, updated))
        results.append({"path": name, "changed": previous != updated, "content": updated})
    if write:
        for path, previous, updated in plans:
            if previous == updated:
                continue
            # Detect a concurrent edit before replacement; never replace symlinks.
            scoped_path(root, path.relative_to(root).as_posix())
            current = path.read_text(encoding="utf-8") if path.exists() else None
            if current != previous:
                raise ValueError(f"Log changed during generation: {path.name}")
            fd, temp_name = tempfile.mkstemp(prefix=".class-notes-log-", dir=path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(updated)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_name, path)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
    return {"written": write, "logs": results, "scan_issues": inventory["issues"]}


def main():
    profile = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=profile / "class-notes/config.toml")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--course", action="append", default=[])
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    try:
        result = build_logs(load_layout(args.config, args.root), args.course, args.write)
    except (OSError, ValueError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
