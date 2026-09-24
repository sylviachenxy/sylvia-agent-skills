#!/usr/bin/env python3
"""晨报的离线、无副作用配置/时间/候选校验与确定性正文渲染。

仅证明结构、范围与声明的一致性；不证明摘要事实、主题语义或授权真实性。
Python 3.9+ 标准库；不读来源、不写状态、不访问网络或原生应用。
"""

import copy
import hashlib
import json
import math
import re
import unicodedata
from datetime import date, datetime, time, timedelta, timezone
from pathlib import PurePath
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


SCHEMA_VERSION = 1
PHONE_PROTOCOL_VERSION = 2
MAX_JSON_BYTES = 1_048_576
MODULES = ("weather", "calendar", "reminders", "goals", "updates")
LABELS = {"weather": "天气与出门提醒", "calendar": "今日安排", "reminders": "重要待办",
          "goals": "目标重点", "updates": "关注动态"}
UTC = timezone.utc
ID_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}\Z")
STAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})\Z")


class ValidationError(ValueError):
    """配置或候选不能安全解释。"""


def _fail(path, message):
    raise ValidationError(f"{path}: {message}")


def _obj(value, path, required, optional=()):
    if not isinstance(value, dict):
        _fail(path, "必须是对象")
    missing, extra = set(required) - set(value), set(value) - set(required) - set(optional)
    if missing or extra:
        _fail(path, f"字段不匹配；缺少={sorted(missing)}，未知={sorted(extra)}")
    return value


def _text(value, path, limit=2000, empty=False):
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        _fail(path, "必须是有界、非空文本" if not empty else "必须是有界文本")
    if any(unicodedata.category(ch) in {"Cc", "Cf", "Cs"} or ch in "\u00a0\u2028\u2029" for ch in value):
        _fail(path, "不能包含控制符、格式控制符、NBSP 或换行")
    if "MB:" in value.upper():
        _fail(path, "不能注入保留的 MB: 机器标记")
    return unicodedata.normalize("NFC", value)


def _int(value, path, low=0, high=100000):
    if type(value) is not int or not low <= value <= high:
        _fail(path, f"必须是 {low}..{high} 的整数（不接受 bool）")
    return value


def _bool(value, path):
    if type(value) is not bool:
        _fail(path, "必须是布尔值")
    return value


def _number(value, path, low, high):
    if type(value) not in (int, float) or not low < value <= high or not math.isfinite(value):
        _fail(path, f"必须是有限数字，且 {low} < value <= {high}")
    return value


def _identifier(value, path):
    value = _text(value, path, 80)
    if not ID_RE.fullmatch(value):
        _fail(path, "标识仅允许字母、数字、点、下划线和连字符")
    return value


def _date(value, path):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        _fail(path, "日期必须是 YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        _fail(path, "无效日期")


def _clock(value, path):
    if not isinstance(value, str) or not re.fullmatch(r"\d{2}:\d{2}", value):
        _fail(path, "时钟必须是 HH:MM")
    try:
        return time.fromisoformat(value)
    except ValueError:
        _fail(path, "无效时钟")


def parse_timestamp(value, path="timestamp"):
    """严格解析带显式偏移、秒精度的 RFC3339 时间；不猜时区。"""
    if not isinstance(value, str) or not STAMP_RE.fullmatch(value):
        _fail(path, "必须是带 Z 或 ±HH:MM 偏移的秒精度 ISO 时间")
    if value.endswith("-00:00"):
        _fail(path, "不接受未知偏移 -00:00")
    if not value.endswith("Z") and (int(value[-5:-3]) > 14 or int(value[-2:]) > 59 or (int(value[-5:-3]) == 14 and int(value[-2:]) != 0)):
        _fail(path, "时区偏移小时/分钟超出范围")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is None or abs(parsed.utcoffset()) > timedelta(hours=14):
            _fail(path, "无效时区偏移")
        return parsed
    except ValueError:
        _fail(path, "无效时间")


def _utc(value):
    try:
        return value.astimezone(UTC)
    except (OverflowError, ValueError):
        _fail("timestamp", "UTC 换算超出支持的日期范围")


def _zone(value):
    value = _text(value, "timezone", 100)
    if value.startswith(("/", ".")) or ".." in value or ("/" not in value and value != "UTC"):
        _fail("timezone", "必须是 IANA 时区名")
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        _fail("timezone", "找不到该 IANA 时区；不回退系统时区")


def _local(day, clock_value, zone, path):
    naive = datetime.combine(day, _clock(clock_value, path))
    candidates = []
    for fold in (0, 1):
        candidate = naive.replace(tzinfo=zone, fold=fold)
        if _utc(candidate).astimezone(zone).replace(tzinfo=None) == naive:
            candidates.append(candidate)
    instants = {_utc(item) for item in candidates}
    if len(instants) != 1:
        _fail(path, "DST 导致本地时刻不存在或有歧义；请调整显式时刻后重试")
    return candidates[0]


def _url(value, path, personal=False):
    value = _text(value, path, 4096)
    if any(ch.isspace() for ch in value) or re.search(r"%(?![0-9a-fA-F]{2})", value):
        _fail(path, "URL 不允许空白或无效百分号转义")
    decoded = unquote(value)
    _text(decoded, path, 4096)
    if any(ch in value for ch in '<>"\\'):
        _fail(path, "URL 含不安全字符")
    try:
        parsed = urlsplit(value)
        schemes = {"https", "http"} | ({"obsidian", "x-apple-reminderkit", "x-apple-calevent"} if personal else set())
        if parsed.scheme not in schemes or parsed.username or parsed.password:
            _fail(path, "不允许该 URI scheme 或凭据")
        if parsed.scheme in {"https", "http"} and (not parsed.hostname or not parsed.netloc):
            _fail(path, "HTTP URL 必须有主机")
        _ = parsed.port
    except ValueError:
        _fail(path, "URL 结构无效")
    return value


def _strings(value, path, minimum=0, limit=40, urls=False):
    if not isinstance(value, list) or not minimum <= len(value) <= limit:
        _fail(path, f"必须是含 {minimum}..{limit} 项的数组")
    result = [(_url(item, path) if urls else _text(item, path, 1000)) for item in value]
    if len(set(result)) != len(result):
        _fail(path, "不允许重复条目")
    return result


def _payload(value):
    """在深拷贝之前限制嵌套、节点、字符串和编码大小。"""
    nodes = [0]
    def walk(item, depth):
        nodes[0] += 1
        if depth > 20 or nodes[0] > 20000:
            _fail("payload", "嵌套或节点过多")
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    _fail("payload", "对象键必须是字符串")
                walk(key, depth + 1)
                walk(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                walk(child, depth + 1)
        elif isinstance(item, str):
            _text(item, "payload text", 8192, empty=True)
        elif item is not None and type(item) not in (int, float, bool):
            _fail("payload", "只能包含 JSON 类型")
        elif type(item) is float and not math.isfinite(item):
            _fail("payload", "不接受 NaN 或 Infinity")
    walk(value, 0)
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, OverflowError, RecursionError) as exc:
        _fail("payload", str(exc))
    if len(encoded) > MAX_JSON_BYTES:
        _fail("payload", "JSON 超过 1 MiB")


def load_json(path):
    """只读有界 JSON 文件；拒绝重复键与非有限 JSON 常量。"""
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                _fail("JSON", f"重复键 {key}")
            result[key] = value
        return result
    with open(path, "rb") as handle:
        raw = handle.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        _fail("JSON", "文件超过 1 MiB")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                           parse_constant=lambda val: _fail("JSON", f"非法常量 {val}"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        _fail("JSON", str(exc))
    _payload(value)
    return value


def _scope(name, value):
    path = f"modules.{name}.scope"
    if name == "updates":
        _obj(value, path, ("topics", "include", "exclude", "preferred_sources", "language", "region"))
        topics = value["topics"]
        if not isinstance(topics, list) or not 1 <= len(topics) <= 20:
            _fail(path, "启用动态必须明确 1..20 个主题，不会默认扩题")
        ids = []
        for topic in topics:
            _obj(topic, path + ".topics", ("id", "query"))
            ids.append(_identifier(topic["id"], path + ".topic.id"))
            _text(topic["query"], path + ".topic.query", 1000)
        if len(set(ids)) != len(ids):
            _fail(path, "主题 ID 必须唯一")
        for key in ("include", "exclude", "preferred_sources"):
            _strings(value[key], path + "." + key, urls=key == "preferred_sources")
        _text(value["language"], path + ".language", 50)
        if value["region"] is not None:
            _text(value["region"], path + ".region", 100)
    elif name == "weather":
        _obj(value, path, ("location", "source_urls"))
        _text(value["location"], path + ".location", 200)
        _strings(value["source_urls"], path + ".source_urls", minimum=1, urls=True)
    elif name == "calendar":
        _obj(value, path, ("calendar_ids",))
        _strings(value["calendar_ids"], path + ".calendar_ids", minimum=1)
    elif name == "reminders":
        _obj(value, path, ("list_ids", "overdue_days", "include_undated_important"))
        _strings(value["list_ids"], path + ".list_ids", minimum=1)
        _int(value["overdue_days"], path + ".overdue_days", 0, 90)
        _bool(value["include_undated_important"], path + ".include_undated_important")
    else:
        _obj(value, path, ("vault_path", "goal_paths"))
        vault = _text(value["vault_path"], path + ".vault_path", 2000)
        if not PurePath(vault).is_absolute() or vault == "/":
            _fail(path, "Vault 必须是具体绝对路径")
        paths = _strings(value["goal_paths"], path + ".goal_paths", minimum=1)
        for relative in paths:
            if PurePath(relative).is_absolute() or ".." in PurePath(relative).parts or not relative.endswith(".md"):
                _fail(path, "Goal 只允许 Vault 内的明确相对 Markdown 路径")


def validate_config(config):
    """验证并返回独立副本；不补默认偏好，不读路径，不把配置当成授权。"""
    _payload(config)
    config = copy.deepcopy(config)
    _obj(config, "config", ("schema_version", "config_id", "config_revision", "timezone", "windows", "schedule", "modules", "storage"))
    if _int(config["schema_version"], "schema_version", 1, 1) != SCHEMA_VERSION:
        _fail("schema_version", "不支持")
    _identifier(config["config_id"], "config_id")
    _int(config["config_revision"], "config_revision", 1)
    _zone(config["timezone"])
    _obj(config["windows"], "windows", ("lookback", "lookahead"))
    nominal = {}
    for name, window in config["windows"].items():
        _obj(window, "windows." + name, ("start", "end"))
        values = []
        for endpoint in ("start", "end"):
            part = _obj(window[endpoint], endpoint, ("day_offset", "time"))
            offset = _int(part["day_offset"], endpoint + ".day_offset", -7, 7)
            clock = _clock(part["time"], endpoint + ".time")
            values.append(offset * 1440 + clock.hour * 60 + clock.minute)
        if values[1] <= values[0]:
            _fail("windows." + name, "名义端点必须递增；实际跨度按适用日 DST 校验 <=24h")
        if name == "lookback" and values[1] > 1440:
            _fail("windows.lookback", "回看终点不能在适用日之后")
        if name == "lookahead" and values[0] < 0:
            _fail("windows.lookahead", "前瞻起点不能在适用日之前")
        nominal[name] = values
    schedule = _obj(config["schedule"], "schedule", ("executor", "weekdays", "generate_at", "ready_by", "wake_at", "generation_buffer_minutes", "sync_buffer_minutes"))
    if schedule["executor"] != "mac":
        _fail("schedule.executor", "只支持起床前在 Mac 生成")
    days = schedule["weekdays"]
    if not isinstance(days, list) or not days or len(days) > 7:
        _fail("schedule.weekdays", "必须明确 ISO 星期 1..7")
    for day in days:
        _int(day, "schedule.weekdays", 1, 7)
    if len(set(days)) != len(days):
        _fail("schedule.weekdays", "星期不得重复")
    minutes = []
    for key in ("generate_at", "ready_by", "wake_at"):
        clock = _clock(schedule[key], "schedule." + key)
        minutes.append(clock.hour * 60 + clock.minute)
    generation = _int(schedule["generation_buffer_minutes"], "generation_buffer_minutes", 1, 720)
    sync = _int(schedule["sync_buffer_minutes"], "sync_buffer_minutes", 1, 720)
    if not minutes[0] < minutes[1] < minutes[2] or minutes[1] - minutes[0] < generation or minutes[2] - minutes[1] < sync:
        _fail("schedule", "必须 generate_at < ready_by < wake_at，并保留两项缓冲")
    if nominal["lookback"][1] > minutes[0]:
        _fail("schedule", "正常 Mac 采集必须在回看窗口结束后开始")
    _obj(config["modules"], "modules", MODULES)
    for name, module in config["modules"].items():
        _obj(module, "modules." + name, ("enabled", "required", "max_age_hours", "max_items"), ("scope",))
        _bool(module["enabled"], name + ".enabled")
        _bool(module["required"], name + ".required")
        _number(module["max_age_hours"], name + ".max_age_hours", 0, 720)
        _int(module["max_items"], name + ".max_items", 1, 50)
        if module["enabled"]:
            if "scope" not in module:
                _fail(name, "启用模块必须有明确 scope")
            _scope(name, module["scope"])
        elif "scope" in module:
            if not isinstance(module["scope"], dict):
                _fail(name + ".scope", "必须是对象")
            if module["scope"]:
                _scope(name, module["scope"])
    if not any(module["enabled"] for module in config["modules"].values()):
        _fail("modules", "至少启用一个内容模块")
    storage = _obj(config["storage"], "storage", ("scope", "state_dir", "retention_days", "notes"))
    if storage["scope"] != "private-local":
        _fail("storage.scope", "只支持经确认的本地私有状态范围")
    directory = _text(storage["state_dir"], "storage.state_dir", 2000)
    if not PurePath(directory).is_absolute() or directory == "/" or ".." in PurePath(directory).parts:
        _fail("storage.state_dir", "必须是具体、无 .. 的绝对路径；不会自动创建")
    _int(storage["retention_days"], "storage.retention_days", 1, 3650)
    notes = _obj(storage["notes"], "storage.notes", ("account", "folder", "shared"))
    _text(notes["account"], "notes.account", 200)
    _text(notes["folder"], "notes.folder", 200)
    if _bool(notes["shared"], "notes.shared"):
        _fail("notes.shared", "不允许共享 Notes 目标")
    return config


def resolve_windows(config, applicable_date):
    """将相对端点解析为具体半开区间；实际经过时间严格 <=24h。"""
    config = validate_config(config)
    day, zone = _date(applicable_date, "applicable_date"), _zone(config["timezone"])
    if day.isoweekday() not in config["schedule"]["weekdays"]:
        _fail("applicable_date", "不在已配置执行星期内")
    result = {"applicable_date": applicable_date, "timezone": config["timezone"]}
    for name, window in config["windows"].items():
        values = []
        for endpoint in ("start", "end"):
            part = window[endpoint]
            try:
                endpoint_day = day + timedelta(days=part["day_offset"])
            except OverflowError:
                _fail(name, "日期越界")
            values.append(_local(endpoint_day, part["time"], zone, name + "." + endpoint))
        seconds = (_utc(values[1]) - _utc(values[0])).total_seconds()
        if not 0 < seconds <= 86400:
            _fail(name, "实际经过跨度必须 >0 且 <=24h（含 DST 变化）")
        result[name] = {"start_at": values[0].isoformat(), "end_at": values[1].isoformat(), "duration_hours": seconds / 3600}
    schedule = config["schedule"]
    clocks = {key: _local(day, schedule[key], zone, "schedule." + key) for key in ("generate_at", "ready_by", "wake_at")}
    if (_utc(clocks["ready_by"]) - _utc(clocks["generate_at"])).total_seconds() < schedule["generation_buffer_minutes"] * 60 or (_utc(clocks["wake_at"]) - _utc(clocks["ready_by"])).total_seconds() < schedule["sync_buffer_minutes"] * 60:
        _fail("schedule", "实际时刻不满足生成/同步缓冲")
    if _utc(parse_timestamp(result["lookback"]["end_at"])) > _utc(clocks["generate_at"]):
        _fail("schedule", "回看结束晚于 Mac 采集时间")
    result["schedule"] = {key: value.isoformat() for key, value in clocks.items()}
    return result


def _window(value, path):
    _obj(value, path, ("start_at", "end_at"))
    start, end = (parse_timestamp(value[key], path + "." + key) for key in ("start_at", "end_at"))
    if _utc(start) >= _utc(end):
        _fail(path, "窗口必须有正跨度")
    return start, end


def _item(name, item, index, config, windows, generated):
    path = f"modules.{name}.items[{index}]"
    common = ("title", "summary", "source_url")
    extra = ("source_label", "occurred_at", "inference", "managed")
    fields = {"updates": ("topic_id", "published_at"),
              "weather": ("location", "valid_from", "valid_until"),
              "calendar": ("start_at", "end_at", "all_day", "status", "availability"),
              "reminders": ("due_date", "due_at", "important"),
              "goals": ("goal_id", "action_id", "approved", "status")}[name]
    _obj(item, path, common + fields, extra)
    item = copy.deepcopy(item)
    for key in ("title", "summary", "source_label", "inference"):
        if key in item:
            item[key] = _text(item[key], path + "." + key, 2000 if key != "title" else 300)
    if item["source_url"] is None:
        if name in ("updates", "weather"):
            _fail(path + ".source_url", "公开信息必须保留来源 URL")
    else:
        item["source_url"] = _url(item["source_url"], path + ".source_url", personal=name not in ("updates", "weather"))
    if "occurred_at" in item:
        parse_timestamp(item["occurred_at"], path + ".occurred_at")
    if "managed" in item and item["managed"] is not None:
        managed = _obj(item["managed"], path + ".managed", ("goal_id", "projection_id"), ("action_id",))
        for key, value in managed.items():
            if key != "action_id" or value is not None:
                _identifier(value, path + ".managed." + key)
    if name == "updates":
        topics = {topic["id"] for topic in config["modules"][name]["scope"]["topics"]}
        if _identifier(item["topic_id"], path + ".topic_id") not in topics:
            _fail(path, "topic_id 不在明确选择的主题中；不能自动扩题")
        published = parse_timestamp(item["published_at"], path + ".published_at")
        start, end = _window({key: windows["lookback"][key] for key in ("start_at", "end_at")}, "lookback")
        if not _utc(start) <= _utc(published) < _utc(end) or _utc(published) > _utc(generated):
            _fail(path, "发布时间不在已发生的回看窗口内")
    elif name == "calendar":
        start, end = _window({"start_at": item["start_at"], "end_at": item["end_at"]}, path)
        left, right = _window({key: windows["lookahead"][key] for key in ("start_at", "end_at")}, "lookahead")
        if _utc(end) <= _utc(left) or _utc(start) >= _utc(right):
            _fail(path, "事件不与前瞻窗口重叠")
        _bool(item["all_day"], path + ".all_day")
        if item["status"] not in ("confirmed", "tentative", "none"):
            _fail(path, "取消事件不能作为确定安排；无效 status")
        if item["availability"] not in ("busy", "free", "tentative", "unavailable", "not_supported"):
            _fail(path, "无效 availability")
    elif name == "reminders":
        _bool(item["important"], path + ".important")
        if item["due_date"] is not None and item["due_at"] is not None:
            _fail(path, "仅日期到期与具体时刻到期不能混用")
        if item["due_date"] is not None:
            _date(item["due_date"], path + ".due_date")
        if item["due_at"] is not None:
            parse_timestamp(item["due_at"], path + ".due_at")
        zone = _zone(config["timezone"])
        lower_day = _date(windows["applicable_date"], "applicable_date") - timedelta(days=config["modules"][name]["scope"]["overdue_days"])
        end = parse_timestamp(windows["lookahead"]["end_at"])
        if item["due_date"] is not None:
            due_day = _date(item["due_date"], path + ".due_date")
            last_day = end.astimezone(zone).date() - (timedelta(days=1) if end.astimezone(zone).time() == time(0) else timedelta(0))
            if not lower_day <= due_day <= last_day:
                _fail(path, "仅日期待办超出批准的逾期/前瞻范围")
        elif item["due_at"] is not None:
            due = parse_timestamp(item["due_at"], path + ".due_at")
            if due.astimezone(zone).date() < lower_day or _utc(due) >= _utc(end):
                _fail(path, "定时待办超出批准的逾期/前瞻范围")
        if item["due_date"] is None and item["due_at"] is None and not (item["important"] and config["modules"][name]["scope"]["include_undated_important"]):
            _fail(path, "无日期事项仅在明确启用且标为重要时允许")
    elif name == "goals":
        _identifier(item["goal_id"], path + ".goal_id")
        _identifier(item["action_id"], path + ".action_id")
        if not _bool(item["approved"], path + ".approved") or item["status"] != "active":
            _fail(path, "目标重点只接受已批准且活动中的可执行行动")
    else:
        if _text(item["location"], path + ".location", 200) != config["modules"][name]["scope"]["location"]:
            _fail(path, "天气地点不匹配所选 scope")
        _window({"start_at": item["valid_from"], "end_at": item["valid_until"]}, path)
    return item


def _module(name, value, config, windows, generated):
    path = "modules." + name
    _obj(value, path, ("coverage", "as_of", "collected_through", "query_window", "result_count", "truncated_reason", "error", "items"))
    coverage = value["coverage"]
    if not isinstance(coverage, str) or coverage not in ("complete", "partial", "unavailable", "declined"):
        _fail(path, "未知 coverage")
    for key in ("truncated_reason", "error"):
        if value[key] is not None:
            _text(value[key], path + "." + key, 1000)
    if coverage == "complete" and (value["truncated_reason"] or value["error"]):
        _fail(path, "complete 不能同时声明错误或截断")
    if not isinstance(value["items"], list) or len(value["items"]) > config["modules"][name]["max_items"]:
        _fail(path, "items 超过已配置上限或不是数组")
    count = _int(value["result_count"], path + ".result_count", 0, 100000)
    if count < len(value["items"]):
        _fail(path, "result_count 小于实际条目数")
    if coverage in ("unavailable", "declined") and (value["items"] or count):
        _fail(path, "来源未读到时不能混入缓存内容或声称读取结果")
    issues = [] if coverage == "complete" else ["来源覆盖=" + coverage]
    stamps = {}
    for key in ("as_of", "collected_through"):
        stamp = None if value[key] is None else parse_timestamp(value[key], path + "." + key)
        stamps[key] = stamp
        if stamp is not None and _utc(stamp) > _utc(generated):
            _fail(path, key + " 晚于生成时刻")
    if stamps["as_of"] is None:
        issues.append("来源读取时间未知")
    elif (_utc(generated) - _utc(stamps["as_of"])).total_seconds() > config["modules"][name]["max_age_hours"] * 3600:
        issues.append("来源超过最大数据年龄")
    if stamps["collected_through"] is None:
        issues.append("实际采集截止未知")
    elif stamps["as_of"] is None or _utc(stamps["collected_through"]) > _utc(stamps["as_of"]):
        _fail(path, "实际采集截止不能晚于实际读取时刻")
    if value["query_window"] is not None:
        actual = _window(value["query_window"], path + ".query_window")
        if name in ("updates", "calendar", "reminders"):
            expected_key = "lookback" if name == "updates" else "lookahead"
            expected = tuple(parse_timestamp(windows[expected_key][key]) for key in ("start_at", "end_at"))
            if name == "reminders":
                lower_day = _date(windows["applicable_date"], "applicable_date") - timedelta(days=config["modules"][name]["scope"]["overdue_days"])
                expected = (_local(lower_day, "00:00", _zone(config["timezone"]), path + ".query_window.start_at"), expected[1])
            if _utc(actual[0]) < _utc(expected[0]) or _utc(actual[1]) > _utc(expected[1]):
                _fail(path, "声明的查询超出所选窗口")
            if tuple(map(_utc, actual)) != tuple(map(_utc, expected)):
                issues.append("只覆盖了部分查询窗口")
    elif name in ("updates", "calendar", "reminders"):
        issues.append("未记录查询窗口")
    if name == "updates":
        end = parse_timestamp(windows["lookback"]["end_at"])
        if _utc(end) > _utc(generated) or stamps["collected_through"] is None or _utc(stamps["collected_through"]) < _utc(end):
            issues.append("回看窗口尚未完整采集")
    items = [_item(name, item, index, config, windows, generated) for index, item in enumerate(value["items"])]
    if name == "updates" and stamps["as_of"] is not None:
        if any(_utc(parse_timestamp(item["published_at"])) > _utc(stamps["as_of"]) for item in items):
            _fail(path, "条目发布时间晚于声明的实际读取时刻")
    if name == "updates":
        for item in items:
            published = _utc(parse_timestamp(item["published_at"]))
            if stamps["collected_through"] is not None and published > _utc(stamps["collected_through"]):
                _fail(path, "条目发布时间晚于实际采集截止")
            if value["query_window"] is not None:
                actual_start, actual_end = _window(value["query_window"], path + ".query_window")
                if not _utc(actual_start) <= published < _utc(actual_end):
                    _fail(path, "条目发布时间不在声明的实际查询窗口内")
    if name == "weather":
        if not items:
            issues.append("没有可用天气预报")
        wake = parse_timestamp(windows["schedule"]["wake_at"])
        for item in items:
            if not _utc(parse_timestamp(item["valid_from"])) <= _utc(wake) < _utc(parse_timestamp(item["valid_until"])):
                issues.append("天气预报有效期不覆盖起床时刻")
    elif name == "calendar":
        items.sort(key=lambda item: (not item["all_day"], _utc(parse_timestamp(item["start_at"])), item["title"]))
    elif name == "reminders":
        def due_key(item):
            if item["due_at"]:
                due = parse_timestamp(item["due_at"]).astimezone(_zone(config["timezone"]))
                return (due.date().isoformat(), 0, due.isoformat(), item["title"])
            return (item["due_date"] or "9999-12-31", 1, "", item["title"])
        items.sort(key=due_key)
    result = copy.deepcopy(value)
    result.update({"items": items, "quality_issues": list(dict.fromkeys(issues)), "required": config["modules"][name]["required"]})
    return result


def _item_lines(name, item):
    lines = ["• " + item["title"], item["summary"]]
    if name == "updates":
        lines.append(f"主题：{item['topic_id']}；发布时间：{item['published_at']}")
    elif name == "calendar":
        label = "全天（不等于整天忙碌）" if item["all_day"] else "时间"
        lines.append(f"{label}：{item['start_at']} → {item['end_at']}；{item['status']} / {item['availability']}")
    elif name == "reminders":
        lines.append("到期：" + (item["due_at"] or (item["due_date"] + "（仅日期，不代表 00:00 逾期）" if item["due_date"] else "未设日期；已标重要")))
    elif name == "goals":
        lines.append(f"活动目标 / 已批准行动：{item['goal_id']} / {item['action_id']}")
    else:
        lines.append(f"地点：{item['location']}；预报有效期：{item['valid_from']} → {item['valid_until']}")
    if "occurred_at" in item:
        lines.append("事件发生时间：" + item["occurred_at"])
    if "inference" in item:
        lines.append("推断（非来源事实）：" + item["inference"])
    if item.get("managed"):
        managed = item["managed"]
        lines.append("稳定关联：" + managed["goal_id"] + " / " + (managed.get("action_id") or "未关联行动") + " / " + managed["projection_id"])
    if item["source_url"]:
        lines.append("来源" + ("（" + item["source_label"] + "）" if item.get("source_label") else "") + "：" + item["source_url"])
    elif item.get("source_label"):
        lines.append("来源：" + item["source_label"])
    return lines


def build_package(config, candidate, now=None):
    """返回已校验候选包，不宣称发布、同步或事实核实成功。

    now 可用 aware datetime 或严格 ISO 字符串作离线确定性测试；默认当前 UTC。
    revision 由调用方的私有版本状态提供，本函数不推断或递增版本。
    手机协议独立于配置/候选 JSON schema；手机按 (config_revision, revision)
    排序，不以固定的配置修订拒绝同一 config_id 的新偏好正文。
    """
    config = validate_config(config)
    _payload(candidate)
    _obj(candidate, "candidate", ("schema_version", "config_id", "config_revision", "applicable_date", "revision", "generated_at", "modules"))
    for field in ("schema_version", "config_revision", "revision"):
        _int(candidate[field], field, 1, 1 if field == "schema_version" else 100000)
    if candidate["config_id"] != config["config_id"] or candidate["config_revision"] != config["config_revision"]:
        _fail("candidate", "候选配置身份或修订不匹配")
    windows = resolve_windows(config, candidate["applicable_date"])
    generated = parse_timestamp(candidate["generated_at"], "generated_at")
    current = datetime.now(UTC) if now is None else (parse_timestamp(now, "now") if isinstance(now, str) else now)
    if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
        _fail("now", "必须是 aware datetime 或带时区 ISO 时间")
    if _utc(generated) > _utc(current):
        _fail("generated_at", "不能声称未来已生成")
    if generated.astimezone(_zone(config["timezone"])).date().isoformat() != candidate["applicable_date"]:
        _fail("generated_at", "必须在所选时区的适用日内生成，不支持前晚版")
    ready_by = parse_timestamp(windows["schedule"]["ready_by"])
    if _utc(generated) > _utc(ready_by):
        _fail("generated_at", "超出确认的起床前就绪期限；需显式重新配置或仅作历史材料")
    enabled = {name for name, module in config["modules"].items() if module["enabled"]}
    if not isinstance(candidate["modules"], dict) or set(candidate["modules"]) - enabled:
        _fail("candidate.modules", "关闭或未知模块的输入不得混入（即使为空）")
    normalized, issues = {}, []
    if _utc(generated) < _utc(parse_timestamp(windows["schedule"]["generate_at"])):
        issues.append("提前手动预览：未进入配置的正常生成时段")
    if _utc(parse_timestamp(windows["lookback"]["end_at"])) > _utc(generated):
        issues.append("回看窗口尚未结束，不能声称完整 READY")
    for name in MODULES:
        if name not in enabled:
            continue
        value = candidate["modules"].get(name, {"coverage": "unavailable", "as_of": None, "collected_through": None, "query_window": None, "result_count": 0, "truncated_reason": None, "error": "没有提交该启用模块的来源记录", "items": []})
        normalized[name] = _module(name, value, config, windows, generated)
        if normalized[name]["required"] and normalized[name]["quality_issues"]:
            issues.append(LABELS[name] + "：" + "；".join(normalized[name]["quality_issues"]))
    readiness = "PARTIAL" if issues else "READY"
    zone = _zone(config["timezone"])
    try:
        applicable_day = _date(candidate["applicable_date"], "applicable_date")
        next_day = applicable_day + timedelta(days=1)
    except OverflowError:
        _fail("applicable_date", "无法计算适用日结束边界")
    valid_from = _local(applicable_day, "00:00", zone, "valid_from").isoformat()
    valid_until = _local(next_day, "00:00", zone, "valid_until").isoformat()
    freshness_limits = [_utc(parse_timestamp(valid_until, "valid_until"))]
    for name, source in normalized.items():
        if not source["required"]:
            continue
        if source["as_of"] is not None:
            freshness_limits.append(_utc(parse_timestamp(source["as_of"])) + timedelta(hours=config["modules"][name]["max_age_hours"]))
        if name == "weather":
            freshness_limits.extend(_utc(parse_timestamp(item["valid_until"])) for item in source["items"])
    fresh_until = min(freshness_limits).replace(microsecond=0).astimezone(zone).isoformat()
    identity = {"config_id": config["config_id"], "scope": config["storage"]["scope"], "notes": config["storage"]["notes"], "applicable_date": candidate["applicable_date"]}
    brief_id = "mb-" + hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]
    title = f"晨间简报 · {candidate['applicable_date']} · {brief_id} · c{config['config_revision']:02d} · r{candidate['revision']:02d}"
    metadata = {"SCHEMA": PHONE_PROTOCOL_VERSION, "CONFIG": config["config_id"], "CONFIG-REVISION": config["config_revision"], "DATE": candidate["applicable_date"], "TIMEZONE": config["timezone"], "BRIEF": brief_id, "REVISION": candidate["revision"], "STATUS": readiness, "GENERATED": candidate["generated_at"], "FRESH-UNTIL": fresh_until, "VALID-FROM": valid_from, "VALID-UNTIL": valid_until}
    lines = [f"适用日期：{candidate['applicable_date']}；时区：{config['timezone']}"]
    lines.extend([f"生成时状态：{readiness}；起床前 Mac 生成；时间：{candidate['generated_at']}",
                  f"新鲜度界限：{fresh_until}；读取时超过此界限应降为 PARTIAL，不表示正文无效或不存在。",
                  f"回看：{windows['lookback']['start_at']} → {windows['lookback']['end_at']}（{windows['lookback']['duration_hours']:g}h，结束端不含）",
                  f"前瞻：{windows['lookahead']['start_at']} → {windows['lookahead']['end_at']}（{windows['lookahead']['duration_hours']:g}h，结束端不含）"])
    lines.extend("提示：" + issue for issue in issues)
    for name, source in normalized.items():
        lines.extend(["", LABELS[name], f"覆盖：{source['coverage']}；{'必需' if source['required'] else '可选'}；来源时间：{source['as_of'] or '未知'}；实际截止：{source['collected_through'] or '未知'}；查询结果 {source['result_count']} 项"])
        if source["query_window"] is not None:
            lines.append(f"实际查询：{source['query_window']['start_at']} → {source['query_window']['end_at']}（结束端不含）")
        lines.extend("缺口：" + issue for issue in source["quality_issues"])
        for key in ("truncated_reason", "error"):
            if source[key]:
                lines.append(("截断：" if key == "truncated_reason" else "读取说明：") + source[key])
        if not source["items"]:
            lines.append("所选范围内未发现可展示条目。" if source["coverage"] == "complete" and not source["quality_issues"] else "本模块无可展示条目；不能据此断言没有事项。")
        for item in source["items"]:
            lines.extend(_item_lines(name, item))
    lines.extend(["", "这是所列采集时刻的信息快照，不包含此后变化。结构校验不证明来源声明、摘要事实或主题语义已被核实。"])
    lines.extend(["", "校验信息（供快捷指令读取）"])
    lines.extend(f"MB:{key}={value}" for key, value in metadata.items())
    content_text = unicodedata.normalize("NFC", "\n".join(lines))
    content_sha256 = hashlib.sha256(content_text.encode("utf-8")).hexdigest()
    body_text = title + "\nMB:BEGIN\nMB:CONTENT-BEGIN\n" + content_text + "\nMB:CONTENT-END\nMB:CONTENT-SHA256=" + content_sha256 + "\nMB:END"
    return {"schema_version": SCHEMA_VERSION, "protocol_version": PHONE_PROTOCOL_VERSION, "config_id": config["config_id"], "config_revision": config["config_revision"], "brief_id": brief_id, "revision": candidate["revision"], "applicable_date": candidate["applicable_date"], "timezone": config["timezone"], "generated_at": candidate["generated_at"], "fresh_until": fresh_until, "valid_from": valid_from, "valid_until": valid_until, "readiness": readiness, "windows": windows, "modules": normalized, "quality_issues": issues, "title": title, "content_text": content_text, "content_sha256": content_sha256, "body_text": body_text, "body_sha256": hashlib.sha256(body_text.encode("utf-8")).hexdigest()}
