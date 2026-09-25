#!/usr/bin/env python3
"""Local tutoring evidence, immutable item keys, exposure tracking and review views.

Python 3.10+ standard library; one Mac writer. No network, audio capture or Apple writes.
"""
import argparse
import copy
import fcntl
import hashlib
import html
import json
import math
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if __name__ == '__main__':
    # Works from a copied skill even under Python -I and an unrelated cwd.
    sys.path.insert(0, str(Path(__file__).resolve().parent))

REGISTRY = Path.home() / 'Library/Application Support/cet4-english-coach/profiles.json'
DOMAINS = {'vocabulary', 'grammar', 'reading', 'banked_cloze', 'matching', 'translation', 'writing', 'listening', 'speaking'}
DEFAULTS = {'exam_year': None, 'exam_session': 'unknown', 'target_score': None,
            'oral_target': None, 'timezone': 'Asia/Shanghai', 'daily_minutes': 30,
            'feedback_language': 'zh-CN', 'store_quotes': False, 'goal_id': None,
            'baseline_note': ''}
BEGIN = '<!-- CET4:BEGIN '
END = '<!-- CET4:END -->'


def require(ok, message):
    if not ok:
        raise ValueError(message)


def identifier(value):
    require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,99}', value), 'Invalid stable ID')
    return value


def nonempty(value, name):
    require(isinstance(value, str) and bool(value.strip()), f'{name} must be nonempty text')


def exact_keys(data, required, optional=()):
    require(isinstance(data, dict), 'Expected JSON object')
    require(set(required) <= data.keys(), f'Missing fields: {set(required)-data.keys()}')
    require(data.keys() <= set(required) | set(optional), f'Unknown fields: {data.keys()-set(required)-set(optional)}')


def timestamp(value):
    require(isinstance(value, str), 'Timestamp must be text')
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    require(dt.tzinfo is not None, 'Timestamp requires timezone offset')
    return dt


def validate_settings(settings):
    exact_keys(settings, DEFAULTS)
    require(settings['exam_year'] is None or type(settings['exam_year']) is int and 2000 <= settings['exam_year'] <= 2100, 'Invalid exam year')
    require(settings['exam_session'] in {'unknown', 'june', 'december'}, 'Invalid exam session')
    require(settings['target_score'] is None or type(settings['target_score']) is int and 0 <= settings['target_score'] <= 710, 'Invalid target score')
    require(type(settings['daily_minutes']) is int and 1 <= settings['daily_minutes'] <= 480, 'Invalid daily minutes')
    require(type(settings['store_quotes']) is bool, 'store_quotes must be boolean')
    require(settings['oral_target'] in {None, '优秀', '良好', '合格'}, 'Invalid oral target')
    for name in ('feedback_language', 'baseline_note'):
        require(isinstance(settings[name], str), f'{name} must be text')
    try:
        ZoneInfo(settings['timezone'])
    except (ZoneInfoNotFoundError, TypeError) as exc:
        raise ValueError('Unknown IANA timezone') from exc
    require(settings['goal_id'] is None or isinstance(settings['goal_id'], str) and re.fullmatch(r'G-\d{4}-\d{3,}', settings['goal_id']), 'Invalid goal_id')


def new_state(profile, settings=None):
    config = DEFAULTS | (settings or {})
    validate_settings(config)
    return {'schema_version': 1, 'profile_id': identifier(profile), 'revision': 1,
            'settings': config, 'settings_history': [{'revision': 1, 'settings': copy.deepcopy(config)}],
            'items': {}, 'presentations': {}, 'attempts': {}, 'invalidations': {}, 'requests': {}}


def fingerprint(stem):
    # Conservative duplicate detection across item IDs; semantic near-duplicates need teacher review.
    return hashlib.sha256(re.sub(r'\s+', ' ', stem).strip().casefold().encode()).hexdigest()


def active_attempts(state):
    invalid = {x['attempt_id'] for x in state['invalidations'].values()}
    return sorted((a for k, a in state['attempts'].items() if k not in invalid), key=lambda a: timestamp(a['occurred_at']))


def apply(state, command, payload, now=None):
    """Return a new state and response; validation failure never mutates caller state."""
    now = now or datetime.now(timezone.utc)
    state = copy.deepcopy(state)
    require(state.get('schema_version') == 1, 'Unsupported state version')
    require(isinstance(payload, dict), 'Expected JSON object')
    identifier(payload.get('event_id'))
    request = {'command': command, 'payload': payload}
    prior = state['requests'].get(payload['event_id'])
    if prior:
        require(prior['request'] == request, 'Same event_id with different content')
        return state, prior['result'] | {'replayed': True}
    event = payload['event_id']
    result = {'event_id': event, 'replayed': False}
    if command == 'configure':
        exact_keys(payload, ['event_id', 'expected_revision', 'patch'])
        require(type(payload['expected_revision']) is int, 'expected_revision must be an integer')
        require(payload['expected_revision'] == state['revision'], 'Stale revision; read current settings before merging')
        require(isinstance(payload['patch'], dict) and payload['patch'].keys() <= DEFAULTS.keys(), 'Unknown settings')
        state['settings'].update(payload['patch'])
        validate_settings(state['settings'])
        state['settings_history'].append({'revision': state['revision'] + 1, 'settings': copy.deepcopy(state['settings'])})
    elif command == 'prepare':
        exact_keys(payload, ['event_id', 'item'])
        item = payload['item']
        exact_keys(item, ['item_id', 'domain', 'focus', 'demand', 'stem', 'key', 'rubric', 'source'], ['time_limit_seconds'])
        identifier(item['item_id'])
        require(item['domain'] in DOMAINS, 'Invalid domain')
        require(item['demand'] in {'understand', 'produce'}, 'Invalid demand')
        for key in ('focus', 'stem', 'key'):
            nonempty(item[key], key)
        require(isinstance(item['rubric'], list) and 1 <= len(item['rubric']) <= 8 and all(isinstance(x, str) and x.strip() for x in item['rubric']), 'Require 1–8 defensible rubric criteria')
        exact_keys(item['source'], ['kind', 'locator', 'answer_status'])
        require(item['source']['kind'] in {'original', 'user_material', 'official_verified', 'publisher_reconstruction'}, 'Invalid source kind')
        nonempty(item['source']['locator'], 'source locator')
        require(item['source']['answer_status'] in {'tutor_checked', 'official_key_verified'}, 'An unchecked key cannot be frozen')
        limit = item.get('time_limit_seconds')
        require(limit is None or type(limit) is int and limit > 0, 'Invalid time limit')
        require(item['item_id'] not in state['items'], 'Immutable item ID exists; use a new ID for revisions')
        state['items'][item['item_id']] = copy.deepcopy(item) | {'fingerprint': fingerprint(item['stem']), 'prepared_at': now.isoformat()}
        result['item_id'] = item['item_id']
    elif command == 'present':
        exact_keys(payload, ['event_id', 'item_id', 'prior_exposure'])
        require(type(payload['prior_exposure']) is bool, 'prior_exposure must be boolean (ask learner when unknown)')
        item = state['items'][payload['item_id']]
        seen = any(p['fingerprint'] == item['fingerprint'] for p in state['presentations'].values())
        p = {'presentation_id': event, 'item_id': item['item_id'], 'fingerprint': item['fingerprint'],
             'unseen': not seen and not payload['prior_exposure'], 'presented_at': now.isoformat(),
             'settings_snapshot': copy.deepcopy(state['settings'])}
        state['presentations'][event] = p
        result.update(presentation_id=event, stem=item['stem'], unseen=p['unseen'])
    elif command == 'record':
        exact_keys(payload, ['event_id', 'presentation_id', 'session_id', 'occurred_at', 'support', 'result',
                             'modality', 'audio_observed', 'evidence_kind', 'evidence', 'reason', 'errors',
                             'next_step', 'new_context', 'transfer_note', 'timer_verified', 'elapsed_seconds'], ['action_id', 'play_count', 'transcript_seen', 'audio_matched'])
        identifier(payload['session_id'])
        p = state['presentations'][payload['presentation_id']]
        item = state['items'][p['item_id']]
        instant = timestamp(payload['occurred_at'])
        require(timestamp(p['presented_at']) <= instant <= now + timedelta(minutes=5), 'Answer timestamp outside presentation/current-time bounds')
        require(payload['support'] in {'none', 'location', 'cue', 'rule', 'model'}, 'Invalid support')
        require(payload['result'] in {'pass', 'partial', 'fail', 'unassessed'}, 'Invalid result')
        require(payload['modality'] in {'text', 'transcript', 'audio'}, 'Invalid modality')
        for name in ('audio_observed', 'new_context', 'timer_verified'):
            require(type(payload[name]) is bool, f'{name} must be boolean')
        require(not payload['audio_observed'] or payload['modality'] == 'audio', 'Audio observation requires actual audio modality')
        require(payload['evidence_kind'] in {'quote', 'observation'}, 'Invalid evidence kind')
        if not state['settings']['store_quotes'] or not p['settings_snapshot']['store_quotes']:
            require(payload['evidence_kind'] == 'observation', 'Raw quotes disabled: supply observation only')
        for name in ('evidence', 'reason', 'next_step'):
            nonempty(payload[name], name)
        require(isinstance(payload['errors'], list) and all(isinstance(e, str) and e.strip() for e in payload['errors']), 'errors must be text list')
        require(isinstance(payload['transfer_note'], str), 'transfer_note must be text')
        require(not payload['new_context'] or bool(payload['transfer_note'].strip()), 'Transfer needs description of the changed context')
        seconds = payload['elapsed_seconds']
        require(seconds is None or type(seconds) in (int, float) and math.isfinite(seconds) and seconds > 0, 'Invalid elapsed time')
        require(not payload['timer_verified'] or seconds is not None, 'Verified timer needs measured seconds')
        goal = p['settings_snapshot']['goal_id']
        if payload.get('action_id'):
            require(goal is not None and isinstance(payload['action_id'], str) and re.fullmatch(re.escape(goal) + r'-A\d{3,}', payload['action_id']), 'action_id must belong to the bound Goal')
        first = not any(a['presentation_id'] == payload['presentation_id'] for a in state['attempts'].values())
        audio_ok = item['domain'] not in {'listening', 'speaking'} or payload['modality'] == 'audio' and payload['audio_observed']
        if item['domain'] == 'listening':
            require(type(payload.get('play_count')) is int and payload['play_count'] >= 0, 'Listening requires actual play_count')
            require(type(payload.get('transcript_seen')) is bool and type(payload.get('audio_matched')) is bool, 'Listening requires transcript/audio conditions')
            audio_ok = audio_ok and payload['play_count'] == 1 and not payload['transcript_seen'] and payload['audio_matched']
        independent = p['unseen'] and first and payload['support'] == 'none' and audio_ok
        independent_pass = independent and payload['result'] == 'pass'
        timed = payload['timer_verified'] and item.get('time_limit_seconds') is not None and seconds <= item['time_limit_seconds']
        state['attempts'][event] = copy.deepcopy(payload) | {'attempt_id': event, 'item_id': item['item_id'],
            'domain': item['domain'], 'focus': item['focus'], 'demand': item['demand'],
            'qualifies_independent': independent, 'independent_pass': independent_pass, 'within_verified_limit': bool(timed),
            'settings_snapshot': copy.deepcopy(p['settings_snapshot']), 'training_only': True}
        result.update(attempt_id=event, result=payload['result'], qualifies_independent=independent,
                      independent_pass=independent_pass, within_verified_limit=bool(timed))
    elif command == 'invalidate':
        exact_keys(payload, ['event_id', 'attempt_id', 'reason'])
        require(payload['attempt_id'] in state['attempts'], 'Unknown attempt')
        nonempty(payload['reason'], 'invalidation reason')
        state['invalidations'][event] = copy.deepcopy(payload) | {'at': now.isoformat()}
    elif command.startswith('assessment-') or command == 'oral-record':
        from assessment import mutate
        result.update(mutate(state, command, payload, now))
    else:
        raise ValueError('Unsupported mutation')
    state['revision'] += 1
    result['revision'] = state['revision']
    state['requests'][event] = {'request': copy.deepcopy(request), 'result': copy.deepcopy(result)}
    return state, result


def context(state, on=None, focus=None):
    from assessment import history, oral_history
    tz = ZoneInfo(state['settings']['timezone'])
    today = date.fromisoformat(on) if on else datetime.now(tz).date()
    attempts = [a for a in active_attempts(state) if timestamp(a['occurred_at']).astimezone(tz).date() <= today]
    groups = {}
    for a in attempts:
        groups.setdefault((a['domain'], a['focus'], a['demand']), []).append(a)
    skills = []
    for (domain, key, demand), samples in sorted(groups.items()):
        status, successful, open_errors = 'exposed', [], []
        for a in samples:
            if a['result'] == 'unassessed':
                continue
            if a['result'] in {'fail', 'partial'}:
                status, successful = 'needs_work', []
                open_errors = sorted(set(open_errors + a['errors']))
            elif a['qualifies_independent']:
                previous_status = status
                previous = successful[:]
                successful.append(a)
                status = 'independent'
                if previous and a['new_context']:
                    status = 'transferable'
                    if timestamp(a['occurred_at']) - timestamp(previous[0]['occurred_at']) >= timedelta(days=7):
                        status = 'durable'
                elif len(previous) and any(x['new_context'] for x in previous[1:]):
                    status = 'transferable'
                if previous_status == 'durable':
                    status = 'durable'
                open_errors = []
            elif status in {'needs_work', 'exposed'}:
                status = 'guided'
        latest = samples[-1]
        interval = {'exposed': 1, 'needs_work': 1, 'guided': 1, 'independent': 3, 'transferable': 7, 'durable': 21}[status]
        # Unassessed or assisted repetitions must not postpone a pending independent check.
        qualifying = [a for a in samples if a['qualifies_independent'] and a['result'] != 'unassessed' or a['result'] in {'fail', 'partial'}]
        anchor = qualifying[-1] if qualifying else samples[0]
        due = timestamp(anchor['occurred_at']).astimezone(tz).date() + timedelta(days=interval)
        skills.append({'domain': domain, 'focus': key, 'demand': demand, 'status': status,
                       'due_date': due.isoformat(), 'due': due <= today,
                       'evidence_ids': [a['attempt_id'] for a in successful],
                       'open_errors': open_errors, 'next_step': latest['next_step'],
                       'timed_evidence': [a['attempt_id'] for a in successful if a['within_verified_limit']]})
    selected = [a for a in attempts if focus is None or a['focus'] == focus]
    assessments = history(state, today.isoformat())
    return {'profile_id': state['profile_id'], 'revision': state['revision'], 'settings': state['settings'],
            'as_of': today.isoformat(), 'training_only': True,
            'skills': [s for s in skills if focus is None or s['focus'] == focus],
            'recent_attempts': selected[-8:], 'active_attempt_count': len(attempts),
            'session_count': len({a['session_id'] for a in attempts}),
            'assessments': assessments[-8:],
            'assessment_index': [{'record_id': r['record_id'], 'title': r['title'],
                                  'occurred_at': r['occurred_at'], 'supersedes': r.get('supersedes')}
                                 for r in assessments],
            'oral_evidence': oral_history(state, today.isoformat()),
            'exposed_items': [{'item_id': p['item_id'], 'fingerprint': p['fingerprint']} for p in state['presentations'].values()]}


def view_content(state):
    data = context(state)
    lines = ['# 四级英语练习台', '', f"档案：{state['profile_id']} · revision {state['revision']}", '',
             '以下为训练证据，不是官方成绩或达标证书。到期日是复测建议，不是系统提醒。', '',
             '| 微能力 | 要求 | 状态 | 建议复测日 |', '| --- | --- | --- | --- |']
    esc = lambda x: html.escape(str(x)).replace('|', '&#124;').replace('\n', ' ')
    for s in data['skills']:
        lines.append(f"| {esc(s['domain'] + ': ' + s['focus'])} | {s['demand']} | {s['status']} | {s['due_date']} |")
    lines += ['', '## 最近证据', '']
    for a in data['recent_attempts']:
        lines += [f"### {a['attempt_id']} · {a['occurred_at']}", '',
                  f"{esc(a['focus'])} · {a['result']} · support={a['support']} · {a['modality']}", '',
                  '证据：' + esc(a['evidence']), '', '判断依据：' + esc(a['reason']), '',
                  '下一步：' + esc(a['next_step']), '']
    settings = state['settings']
    lines += ['', '## 当前设置', '',
              '笔试目标：' + esc(settings['target_score'] if settings['target_score'] is not None else '未设置') +
              '；口试目标：' + esc(settings['oral_target'] or '未设置') +
              '；通常每日：' + str(settings['daily_minutes']) + ' 分钟。', '',
              '## 最近评估记录', '', '显示最近八份；较旧记录仍在档案中。710 报道分与练习原始表现分轨，不能互相换算。', '']
    labels = {'news': '听力·新闻', 'conversations': '听力·长对话', 'passages': '听力·篇章',
              'banked_cloze': '阅读·选词填空', 'matching': '阅读·长篇匹配', 'careful_reading': '阅读·仔细阅读',
              'writing': '写作', 'translation': '翻译', 'total': '总分', 'listening': '听力',
              'reading': '阅读', 'writing_translation': '写作与翻译合并'}
    def shown(value):
        if value is None:
            return '未知'
        if isinstance(value, list):
            return str(value[0]) if value[0] == value[1] else f'{value[0]}–{value[1]}'
        return str(value)
    from assessment import PARTS, SCORE_MAX
    for assessment in data['assessments']:
        lines += [f"### {assessment['record_id']} · {esc(assessment['title'])}", '',
                  esc(assessment['occurred_at']), '']
        if assessment['kind'] == 'official_report':
            r = assessment['reported']
            lines += ['类型：已核原成绩报告' if r['verification'] == 'verified' else '类型：用户自报，尚未核对原报告', '',
                      '| 分项 | 报道分 | 上限 |', '| --- | ---: | ---: |']
            for k, maximum in SCORE_MAX.items():
                lines.append(f'| {labels[k]} | {shown(r[k])} | {maximum} |')
            lines += ['', '口试单列：' + esc(r['oral_grade'] or '未知') + '（不加入710）。',
                      '距当前目标的记录差值：' + shown(assessment['observed_gap']) + '；不是未来预测。', '']
        else:
            lines += ['类型：现场练习' if assessment['kind'] == 'coach_run' else '类型：历史练习导入，不冒充现场冷测', '',
                      '| 分项 | 原始表现 | 单位 | 依据 |', '| --- | --- | --- | --- |']
            for k, (maximum, _) in PARTS.items():
                entry = assessment['raw_sections'].get(k)
                value = entry['value'] if entry else None
                unit = '15分原始判档区间' if k in {'writing', 'translation'} else f'正确题数／{maximum}'
                basis = {'key_checked': '已核答案', 'self_report': '自报未核', 'rubric_estimate': '教练判档'}[entry['basis']] if entry else '未知'
                lines.append(f'| {labels[k]} | {shown(value)} | {unit} | {basis} |')
            lines += ['', '完整教学加权百分比：' + shown(assessment['weighted_practice_percent']) + '（不是CET报道分）。',
                      '未知项：' + esc('、'.join(labels[k] for k in assessment['unknown_components']) or '无') + '。',
                      '各块冷测条件：' + '；'.join(labels[k] + ('符合' if v else '未取得') for k, v in assessment['cold_blocks'].items()) + '。',
                      '同一次完整笔试冷测：' + ('已取得条件证据' if assessment['complete_cold_written'] else '未取得') + '。', '']
        if assessment['supersedes']:
            lines += ['评分更正，替代 ' + esc(assessment['supersedes']) + '；不是新的学习进步。', '']
        lines += ['局限：' + esc('；'.join(assessment['limitations']) or '按本记录来源与条件解释'), '',
                  '下一步：' + esc(assessment['next_step']), '']
    oral = data['oral_evidence']
    stability = {'insufficient': '证据不足／需要补测', 'single_observation': '单次独立观察',
                 'repeated_ai_consistent': '跨日多画像AI证据一致', 'human_partner_observed': '另有真人互动观察'}
    alignment = {'demonstrated_once': '本次优秀对齐', 'partial': '部分对齐',
                 'not_demonstrated': '有机会但未表现', 'indeterminate': '不能判断'}
    lines += ['', '## 口语证据索引', '', '近期稳定性：' + stability[oral['stability']] + '（内部30天复核窗，非官方等级）。',
              '内部优秀证据门槛：' + ('通过；仍不保证正式等级。' if oral['excellent_evidence_gate'] else '尚未通过。'), '',
              '| 证据 | 日期 | 主题／搭档 | 本次对齐 | 回执位置 |', '| --- | --- | --- | --- | --- |']
    for r in oral['records']:
        lines.append(f"| {esc(r['record_id'])} | {esc(r['occurred_at'])} | {esc(r['topic'])} / {esc(r['partner'])} | {alignment[r['alignment']]} | {esc(r['receipt_locator'])} |")
    return '\n'.join(lines).rstrip() + '\n'


def render_view(path, state):
    body = view_content(state)
    block = BEGIN + hashlib.sha256(body.encode()).hexdigest() + ' -->\n' + body + END
    if not path.exists():
        return block + '\n\n## 我的笔记\n\n这里的内容由你维护，不作为自动评分证据。\n'
    old = path.read_text()
    require(old.count(BEGIN) == 1 and old.count(END) == 1, 'Practice.md markers missing/duplicated; preserve user file and reconcile')
    match = re.search(re.escape(BEGIN) + r'([0-9a-f]{64}) -->\n(.*?)' + re.escape(END), old, re.S)
    require(match is not None, 'Malformed managed view')
    require(hashlib.sha256(match[2].encode()).hexdigest() == match[1], 'Managed view edited; reconcile instead of overwriting')
    return old[:match.start()] + block + old[match.end():]


def safe_file(path):
    require(not path.is_symlink(), f'Refusing symlink file: {path.name}')
    require(not any(p.is_symlink() for p in path.parents if p.exists()), 'Refusing symlinked store path')


def atomic(path, body):
    safe_file(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def locked(path):
    safe_file(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def load_state(root):
    safe_file(root / 'state.json')
    require(not any('conflict' in p.name.casefold() or '冲突' in p.name or p.name.startswith('state') and p.suffix == '.json' and p.name != 'state.json' for p in root.iterdir()), 'Possible sync conflict: reconcile copies first')
    state = json.loads((root / 'state.json').read_text())
    require(isinstance(state, dict), 'State must be a JSON object')
    checksum = state.pop('integrity', None)
    require(checksum == hashlib.sha256(json.dumps(state, sort_keys=True, ensure_ascii=False).encode()).hexdigest(), 'State changed outside store or sync corruption; reconcile before writing')
    require(state.get('schema_version') == 1, 'Unsupported state schema')
    require(type(state.get('revision')) is int and state['revision'] > 0, 'Invalid revision')
    validate_settings(state['settings'])
    for field in ('items', 'presentations', 'attempts', 'invalidations', 'requests'):
        require(isinstance(state.get(field), dict), f'Corrupt {field}')
    for field in ('assessment_starts', 'assessments', 'assessment_voids', 'oral_records'):
        require(field not in state or isinstance(state[field], dict), f'Corrupt {field}')
    return state


def persist(root, state):
    safe_file(root / 'Practice.md')
    view = render_view(root / 'Practice.md', state)  # preflight before committing JSON
    stored = copy.deepcopy(state)
    stored.pop('integrity', None)
    stored['integrity'] = hashlib.sha256(json.dumps(stored, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    encoded = json.dumps(stored, ensure_ascii=False, indent=2) + '\n'
    atomic(root / 'state.json', encoded)
    require((root / 'state.json').read_text() == encoded, 'State readback failed')
    try:
        atomic(root / 'Practice.md', view)
        require((root / 'Practice.md').read_text() == view, 'View readback failed')
        return {'saved': True, 'views_verified': True, 'practice_path': str(root / 'Practice.md')}
    except OSError as exc:
        return {'saved': True, 'views_verified': False, 'error': str(exc), 'recovery': 'rebuild; do not invent a new event ID'}


def registry_data(path):
    safe_file(path)
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    require(isinstance(data, dict), 'Invalid registry')
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--registry', type=Path, default=REGISTRY)
    parser.add_argument('--profile')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('profiles')
    init = sub.add_parser('init')
    init.add_argument('--vault', type=Path, required=True)
    init.add_argument('--rebind', action='store_true')
    init.add_argument('--input', type=Path)
    sub.add_parser('show')
    ctx = sub.add_parser('context')
    ctx.add_argument('--on')
    ctx.add_argument('--focus')
    sub.add_parser('rebuild')
    sub.add_parser('materials')
    for command in ('assessment-show', 'assessment-report'):
        sub.add_parser(command).add_argument('--record', required=True)
    comparison = sub.add_parser('assessment-compare')
    comparison.add_argument('--first', required=True)
    comparison.add_argument('--second', required=True)
    scenario = sub.add_parser('assessment-scenario')
    scenario.add_argument('--record', required=True)
    scenario.add_argument('--input', type=Path, required=True)
    for command in ('configure', 'prepare', 'present', 'record', 'invalidate', 'assessment-start',
                    'assessment-record', 'assessment-import', 'assessment-correct', 'assessment-void', 'oral-record'):
        sub.add_parser(command).add_argument('--input', type=Path, required=True)
    args = parser.parse_args()
    try:
        raw_registry = args.registry.expanduser().absolute()
        require(not raw_registry.is_symlink(), 'Registry file itself must not be a symlink')
        # User-selected directory aliases (including macOS /tmp) are canonicalized once.
        # Files and internal managed-store paths still reject symlinks.
        reg = raw_registry.parent.resolve() / raw_registry.name
        if args.command == 'materials':
            from assessment import catalogue
            output = catalogue()
        elif args.command == 'profiles':
            output = registry_data(reg)
        elif args.command == 'init':
            profile = identifier(args.profile)
            vault = args.vault.expanduser().resolve(strict=True)
            require(vault.is_dir(), 'Vault must be an existing selected directory')
            require(not any((p / '.git').exists() for p in (vault, *vault.parents)), 'Personal evidence must not be created in a Git checkout')
            root = vault / 'Learning/CET4-English' / profile
            with locked(reg.with_suffix('.lock')):
                records = registry_data(reg)
                require(profile not in records or records[profile] == str(root) or args.rebind, 'Profile locator differs; explicit rebind required')
                with locked(root / '.lock'):
                    if (root / 'state.json').exists():
                        state = load_state(root)
                        require(state['profile_id'] == profile, 'Profile identity mismatch')
                        require(args.input is None, 'Existing profile: use configure, not init settings')
                    else:
                        require(not args.rebind, 'Rebind requires an existing synced archive')
                        settings = json.loads(args.input.read_text()) if args.input else None
                        state = new_state(profile, settings)
                    goal = state['settings']['goal_id']
                    if goal is not None:
                        require((vault / 'Goals' / goal / (goal + '.md')).is_file(), 'Bound Goal document not found')
                    output = persist(root, state)
                records[profile] = str(root)
                atomic(reg, json.dumps(records, ensure_ascii=False, indent=2) + '\n')
                require(registry_data(reg) == records, 'Registry readback failed')
                output.update(profile_id=profile, revision=state['revision'])
        else:
            records = registry_data(reg)
            profile = args.profile or (next(iter(records)) if len(records) == 1 else None)
            require(profile is not None and profile in records, 'Choose an existing profile; never guess among learners')
            root = Path(records[profile])
            require(root.is_absolute() and root.is_dir(), 'Profile path moved/unavailable; rebind explicitly')
            require(not any((p / '.git').exists() for p in (root, *root.parents)), 'Personal evidence must remain outside Git checkouts')
            with locked(root / '.lock'):
                state = load_state(root)
                require(state['profile_id'] == profile, 'Profile identity mismatch')
                if args.command == 'show':
                    output = {k: state[k] for k in ('profile_id', 'revision', 'settings')}
                elif args.command == 'context':
                    output = context(state, args.on, args.focus)
                elif args.command == 'rebuild':
                    output = persist(root, state)
                elif args.command in {'assessment-show', 'assessment-report', 'assessment-compare', 'assessment-scenario'}:
                    from assessment import active, report, compare, scenario
                    records = active(state)
                    if args.command == 'assessment-compare':
                        output = compare(records[args.first], records[args.second])
                    elif args.command == 'assessment-show':
                        all_records = state.get('assessments', {}) | state.get('oral_records', {})
                        oral_active = args.record in state.get('oral_records', {}) and not any(v['record_id'] == args.record for v in state.get('assessment_voids', {}).values())
                        output = all_records[args.record] | {'active': args.record in records or oral_active,
                            'withdrawals': [v for v in state.get('assessment_voids', {}).values() if v['record_id'] == args.record],
                            'superseded_by': [r['record_id'] for r in state.get('assessments', {}).values() if r.get('supersedes') == args.record]}
                    elif args.command == 'assessment-report':
                        output = report(records[args.record], state['settings']['target_score'])
                    else:
                        output = scenario(records[args.record], json.loads(args.input.read_text()), state['settings']['target_score'])
                else:
                    payload = json.loads(args.input.read_text())
                    state, output = apply(state, args.command, payload)
                    goal = state['settings']['goal_id']
                    if goal is not None:
                        require((root.parents[2] / 'Goals' / goal / (goal + '.md')).is_file(), 'Bound Goal document not found; verify Goal owner first')
                    output.update(persist(root, state))
        print(json.dumps(output, ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f'Tutor store: {exc}\n')


if __name__ == '__main__':
    main()
