"""Score evidence for the 115 + 25 + 10 working blueprint, not an auto-grader.

Pure state operations; tutor_store owns locks, checksums, privacy and persistence.
Unknown scores are bounds, never zeroes. Different attempts are never combined.
"""
import copy
import json
import math
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

CAPS = {'grammar': 10, 'vocabulary': 10, 'cloze': 15, 'reading_abc': 22,
        'reading_gap': 8, 'summary': 10, 'translation': 15, 'writing': 25,
        'listening': 25, 'oral': 10}
WRITTEN = tuple(CAPS)[:8]
TREE = {'written': WRITTEN, 'listening_speaking': ('listening', 'oral'),
        'total': ('written', 'listening_speaking')}
MAXIMA = CAPS | {'written': 115, 'listening_speaking': 35, 'total': 150}
GROUPS = {key: (key,) for key in CAPS} | TREE | {
    'reading': ('reading_abc', 'reading_gap'), 'reading_comprehension': ('cloze', 'reading_abc', 'reading_gap')}
COUNTS = {'grammar': [1] * 10, 'vocabulary': [1] * 10, 'cloze': [1] * 15,
          'reading_abc': [2] * 11, 'reading_gap': [2] * 4, 'summary': [10],
          'translation': [3, 3, 4, 5], 'writing': [25]}
Q_RANGES = {'grammar': range(1, 11), 'vocabulary': range(11, 21), 'cloze': range(21, 36),
            'reading_abc': range(36, 47), 'reading_gap': range(47, 51), 'summary': range(51, 52),
            'translation': range(52, 56), 'writing': range(56, 57)}
LABELS = {'grammar': '语法', 'vocabulary': '选词', 'cloze': '完形', 'reading_abc': '阅读 A/B/C',
          'reading_gap': '六选四', 'summary': '概要', 'translation': '翻译', 'writing': '作文',
          'listening': '听力理解', 'oral': '原听说部分（不是纯口语）', 'written': '笔试',
          'listening_speaking': '听说合计', 'total': '总分'}
UNSET = object()


def require(ok, message):
    if not ok:
        raise ValueError(message)


def keys(data, required, optional=()):
    require(isinstance(data, dict), 'Expected JSON object')
    require(set(required) <= data.keys() <= set(required) | set(optional), 'Missing or unknown assessment fields')


def text(value, label):
    require(isinstance(value, str) and bool(value.strip()), f'{label} requires nonempty text')


def number(value, maximum):
    require(type(value) in (int, float) and math.isfinite(value) and 0 <= value <= maximum, 'Invalid score or duration')


def interval(value, maximum):
    require(isinstance(value, list) and len(value) == 2, 'Expected [low, high]')
    for n in value:
        number(n, maximum)
        require(Decimal(str(n)) * 100 == (Decimal(str(n)) * 100).to_integral_value(), 'Scores support at most two decimal places; do not round evidence into meeting a target')
    require(value[0] <= value[1], 'Reversed score interval')
    return value


def stamp(value):
    from tutor_store import timestamp
    return timestamp(value)


def material_check(material, historical=False):
    keys(material, ['id', 'title', 'kind', 'locator', 'sha256', 'year', 'session', 'normalization'])
    from tutor_store import identifier
    identifier(material['id'])
    for field in ('title', 'locator', 'normalization'):
        text(material[field], field)
    require(material['kind'] in {'district_mock', 'past_exam', 'school_exam', 'original', 'unknown'}, 'Unknown material kind')
    digest = material['sha256']
    require(historical and digest is None or isinstance(digest, str) and len(digest) == 64 and all(c in '0123456789abcdef' for c in digest), 'Material needs an exact SHA256 identity (historical evidence may be unknown/null)')
    require(historical and material['year'] is None or type(material['year']) is int and 2000 <= material['year'] <= 2100, 'Invalid paper year')
    require(material['session'] in {'january', 'june', 'mock', 'unknown'}, 'Invalid paper session')


def protocol_check(protocol):
    keys(protocol, ['baseline_status', 'baseline_source', 'checked_on', 'scoring_plan'])
    require(protocol['baseline_status'] in {'provisional', 'verified_for_named_session'}, 'Invalid baseline status')
    for field in ('baseline_source', 'scoring_plan'):
        text(protocol[field], field)
    date.fromisoformat(protocol['checked_on'])


def catalogue():
    path = Path(__file__).resolve().parents[1] / 'references/assessment-materials.json'
    data = json.loads(path.read_text())
    require(data['schema_version'] == 1, 'Unknown material catalogue schema')
    return data


def material_limits(material):
    materials = catalogue()['materials']
    same_file = [x for x in materials if x['question_sha256'] == material['sha256']]
    require(not same_file or same_file[0]['id'] == material['id'], 'Known file SHA belongs to a different catalogue ID; reconcile paper identity')
    rows = [x for x in materials if x['id'] == material['id']]
    if not rows:
        return {'status': 'not_in_reviewed_catalogue', 'issues': [], 'requires_fresh_acceptance': True}
    row = rows[0]
    match = material['sha256'] == row['question_sha256']
    require(not match or 'year' not in row or material['year'] == row['year'], 'Known paper year disagrees with catalogue identity')
    return {'status': row['status'], 'issues': row['issues'], 'identity_matches': match,
            'section_gates': row['section_gates'],
            'blocked_items': row['blocked_items'],
            'requires_fresh_acceptance': not match, 'notes': row['use_limits']}


def validate_conditions(conditions, scope):
    keys(conditions, scope)
    for domain, c in conditions.items():
        keys(c, ['support', 'timing', 'elapsed_seconds', 'audio', 'evidence'])
        require(c['support'] in {'none', 'hints', 'answers', 'unknown'}, 'Invalid assistance condition')
        require(c['timing'] in {'verified', 'reported', 'untimed', 'unknown'}, 'Invalid timing condition')
        require(c['audio'] in {'observed', 'transcript', 'none', 'unknown'}, 'Invalid audio condition')
        text(c['evidence'], 'conditions evidence')
        elapsed = c['elapsed_seconds']
        require(elapsed is None or type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed > 0, 'Invalid elapsed time')
        require(c['timing'] not in {'verified', 'reported'} or elapsed is not None, 'Timed work needs duration')
        require(domain != 'written' or c['audio'] == 'none', 'Written component must not claim audio observation')


def validate_scores(scores, quotes, conditions, mode):
    require(isinstance(scores, dict) and scores and scores.keys() <= MAXIMA.keys(), 'Unknown or empty score map')
    all_items = set()
    for section, score in scores.items():
        keys(score, ['range', 'basis', 'source', 'rubric', 'items'])
        low, high = interval(score['range'], MAXIMA[section])
        require(score['basis'] in {'key_checked', 'rubric_estimate', 'verified_report', 'user_report'}, 'Unknown score basis')
        for field in ('source', 'rubric'):
            text(score[field], field)
        items = score['items']
        require(isinstance(items, list), 'items must be a list')
        assessed = score['basis'] in {'key_checked', 'rubric_estimate'}
        require(not assessed or section in CAPS and items, 'Coach scoring requires complete section-level item evidence')
        require(mode != 'coach_run' or assessed, 'Use assessment-import for externally awarded or reported grades')
        require(score['basis'] != 'key_checked' or section not in {'summary', 'translation', 'writing', 'oral'}, 'Productive tasks require a rubric, not string matching')
        require(score['basis'] != 'rubric_estimate' or low < high, 'Uncalibrated rubric estimates must preserve uncertainty')
        if assessed and section in {'listening', 'oral'}:
            require(conditions.get('listening_speaking', {}).get('audio') == 'observed', 'Scored listening/speaking requires actual audio, not a transcript')
        for item in items:
            keys(item, ['id', 'max', 'range', 'evidence_kind', 'response', 'reason', 'errors', 'next_step'])
            from tutor_store import identifier
            identifier(item['id'])
            require(item['id'] not in all_items, 'Duplicate item: lost points must not be counted twice')
            all_items.add(item['id'])
            number(item['max'], MAXIMA[section])
            require(item['max'] > 0, 'Item maximum must be positive')
            interval(item['range'], item['max'])
            if assessed and section in WRITTEN[:5]:
                require(all(n in (0, item['max']) for n in item['range']), 'Objective item cannot receive invented fractional credit')
            require(item['evidence_kind'] in {'quote', 'observation'}, 'Invalid evidence kind')
            require(quotes or item['evidence_kind'] == 'observation', 'Raw quotes disabled')
            for field in ('response', 'reason', 'next_step'):
                text(item[field], field)
            require(isinstance(item['errors'], list) and all(isinstance(e, str) and e.strip() for e in item['errors']), 'Invalid error tags')
        if items:
            require(section in CAPS, 'Aggregates cannot contain duplicate item evidence')
            require(abs(sum(i['max'] for i in items) - MAXIMA[section]) < 1e-8, 'Item maxima do not cover the whole section')
            for edge in (0, 1):
                require(abs(sum(i['range'][edge] for i in items) - score['range'][edge]) < 1e-8, 'Section score differs from item sum')
            if section in COUNTS:
                require(sorted(i['max'] for i in items) == sorted(COUNTS[section]), 'Item weights differ from the working blueprint')
                expected = dict(zip((f'Q{q}' for q in Q_RANGES[section]), COUNTS[section]))
                require({i['id']: i['max'] for i in items} == expected, 'Canonical Q numbers/weights do not belong to this section')
    bounds(scores)  # reject mutually inconsistent parent/child scores


def bounds(scores):
    """Feasible intervals in an additive tree; inferred values remain unobserved."""
    b = {k: list(scores[k]['range']) if k in scores else [0, cap] for k, cap in MAXIMA.items()}
    for _ in range(12):
        old = copy.deepcopy(b)
        for parent, children in TREE.items():
            b[parent] = [max(b[parent][0], sum(b[c][0] for c in children)),
                         min(b[parent][1], sum(b[c][1] for c in children))]
            require(b[parent][0] <= b[parent][1] + 1e-8, f'Inconsistent scores: {parent}')
            for child in children:
                others = [c for c in children if c != child]
                b[child] = [max(b[child][0], b[parent][0] - sum(b[c][1] for c in others)),
                            min(b[child][1], b[parent][1] - sum(b[c][0] for c in others))]
                require(b[child][0] <= b[child][1] + 1e-8, f'Inconsistent scores: {child}')
        if old == b:
            break
    return {k: [round(n, 8) for n in v] for k, v in b.items()}


def known_score(scores, section):
    if section in scores:
        return scores[section]['range']
    children = GROUPS.get(section, ())
    if children == (section,):
        return None
    values = [known_score(scores, c) for c in children]
    return [round(sum(v[e] for v in values), 8) for e in (0, 1)] if values and all(v is not None for v in values) else None


def active(state):
    void = {r['record_id'] for r in state.get('assessment_voids', {}).values()}
    return {k: v for k, v in state.get('assessments', {}).items() if k not in void}


def check_notes(payload):
    require(isinstance(payload['limitations'], list) and all(isinstance(s, str) and s.strip() for s in payload['limitations']), 'Invalid limitations')
    text(payload['next_step'], 'next_step')


def mutate(state, command, payload, now):
    """Called inside tutor_store.apply: deep-copy, event idempotency and revision there."""
    starts = state.setdefault('assessment_starts', {})
    records = state.setdefault('assessments', {})
    voids = state.setdefault('assessment_voids', {})
    event = payload['event_id']
    if command == 'assessment-start':
        keys(payload, ['event_id', 'material', 'scope', 'prior_exposure', 'protocol'])
        material_check(payload['material']); protocol_check(payload['protocol'])
        scope = payload['scope']
        require(isinstance(scope, list) and scope and len(set(scope)) == len(scope) and set(scope) <= {'written', 'listening_speaking'}, 'Invalid assessment scope')
        require(payload['prior_exposure'] in {'unseen', 'seen', 'unknown'}, 'Explicit exposure check required')
        identity = payload['material']['sha256']
        seen = any(x['material']['sha256'] == identity or x['material']['id'] == payload['material']['id']
                   for x in list(starts.values()) + list(records.values()))
        starts[event] = copy.deepcopy(payload) | {'started_at': now.isoformat(),
            'unseen': payload['prior_exposure'] == 'unseen' and not seen,
            'settings_snapshot': copy.deepcopy(state['settings']), 'material_review': material_limits(payload['material'])}
        return {'assessment_id': event, 'unseen': starts[event]['unseen'], 'material_review': starts[event]['material_review']}
    if command == 'assessment-void':
        keys(payload, ['event_id', 'record_id', 'reason'])
        require(payload['record_id'] in active(state), 'Unknown or inactive assessment')
        text(payload['reason'], 'void reason')
        voids[event] = copy.deepcopy(payload) | {'at': now.isoformat()}
        return {'voided': payload['record_id']}
    if command == 'assessment-correct':
        keys(payload, ['event_id', 'record_id', 'reason', 'scores', 'limitations', 'next_step'])
        require(payload['record_id'] in active(state), 'Unknown or inactive assessment')
        text(payload['reason'], 'correction reason')
        record = copy.deepcopy(records[payload['record_id']])
        record.update({k: copy.deepcopy(payload[k]) for k in ('scores', 'limitations', 'next_step')})
        record.update(event_id=event, record_id=event, supersedes=payload['record_id'], correction_reason=payload['reason'])
    elif command == 'assessment-record':
        keys(payload, ['event_id', 'assessment_id', 'occurred_at', 'conditions', 'scores', 'limitations', 'next_step'])
        require(payload['assessment_id'] in starts, 'Start and freeze the assessment before recording; import older results instead')
        require(not any(x.get('assessment_id') == payload['assessment_id'] for x in records.values()), 'Assessment already recorded; correct or void without erasing exposure')
        start = starts[payload['assessment_id']]
        end = stamp(payload['occurred_at'])
        require(stamp(start['started_at']) <= end <= now + timedelta(minutes=5), 'Invalid assessment completion time')
        record = copy.deepcopy(payload) | {k: copy.deepcopy(start[k]) for k in ('material', 'protocol', 'scope', 'unseen', 'settings_snapshot', 'material_review')}
        record.update(mode='coach_run', started_at=start['started_at'], record_id=event)
        wall = (end - stamp(start['started_at'])).total_seconds()
        validate_conditions(record['conditions'], record['scope'])
        measured = sum(c['elapsed_seconds'] or 0 for c in record['conditions'].values() if c['timing'] == 'verified')
        require(measured <= wall + 5, 'Claimed verified durations exceed actual assessment window')
    elif command == 'assessment-import':
        keys(payload, ['event_id', 'material', 'protocol', 'occurred_at', 'conditions', 'scores', 'limitations', 'next_step', 'provenance'])
        material_check(payload['material'], historical=True); protocol_check(payload['protocol'])
        text(payload['provenance'], 'historical/import provenance')
        require(stamp(payload['occurred_at']) <= now + timedelta(minutes=5), 'Future grade cannot be imported')
        record = copy.deepcopy(payload) | {'record_id': event, 'mode': 'historical_import', 'unseen': False,
            'scope': list(payload['conditions']), 'settings_snapshot': copy.deepcopy(state['settings']),
            'material_review': material_limits(payload['material'])}
        require(set(record['scope']) <= {'written', 'listening_speaking'}, 'Unknown condition scope')
    else:
        raise ValueError('Unknown assessment mutation')
    validate_conditions(record['conditions'], record['scope'])
    for section in record['scores']:
        group = 'written' if section in WRITTEN or section == 'written' else 'listening_speaking'
        if section != 'total':
            require(group in record['scope'], 'Score outside declared assessment scope')
    check_notes(record)
    validate_scores(record['scores'], state['settings']['store_quotes'] and record['settings_snapshot']['store_quotes'], record['conditions'], record['mode'])
    # A catalogue conflict cannot silently become an exact AI score. A teacher's
    # already-awarded grade is retained as a report, without endorsing its rubric.
    review = record['material_review']
    if 'section_gates' in review:
        for section, score in record['scores'].items():
            if review.get('section_gates', {}).get(section) == 'quarantined' and score['basis'] in {'key_checked', 'rubric_estimate'}:
                require(score['range'][0] < score['range'][1], 'Quarantined section needs an unresolved interval or omission, not exact scoring')
                indexed = {i['id']: i for i in score['items']}
                for q in review.get('blocked_items', {}).get(section, []):
                    require(q in indexed and indexed[q]['range'] == [0, indexed[q]['max']],
                            'Quarantined item must use its canonical Q ID and full unresolved [0, max] range')
    record['saved_at'] = now.isoformat()
    if command == 'assessment-correct':
        voids[event] = {'record_id': payload['record_id'], 'reason': payload['reason'], 'at': now.isoformat()}
    records[event] = record
    return {'record_id': event, 'assessment': report(record, state['settings']['target_score'])}


def report(record, target=UNSET):
    target = record['settings_snapshot']['target_score'] if target is UNSET else target
    if target is not None:
        number(target, 150)
    scores = record['scores']
    b = bounds(scores)
    rows = {k: {'max': cap, 'score': known_score(scores, k), 'possible_bounds': b[k],
                'basis': scores[k]['basis'] if k in scores else 'sum' if known_score(scores, k) is not None else 'unknown'} for k, cap in MAXIMA.items()}
    cold = {}
    for group, c in record['conditions'].items():
        time_limit = 6300 if group == 'written' else 2100
        cold[group] = record['mode'] == 'coach_run' and record['unseen'] and c['support'] == 'none' and c['timing'] == 'verified' and c['elapsed_seconds'] <= time_limit and (group == 'written' or c['audio'] == 'observed')
    target_status = 'no_target'
    if target is not None:
        target_status = 'below_even_with_unknowns_full' if b['total'][1] < target else 'unknown_or_interval_crosses_target'
        if rows['total']['score'] is not None and rows['total']['score'][0] >= target:
            target_status = 'observed_score_range_meets_target_not_prediction'
    errors = []
    for section, score in scores.items():
        for item in score['items']:
            if item['range'][0] < item['max']:
                errors.append({'section': section, 'item_id': item['id'], 'lost_range': [round(item['max'] - item['range'][1], 8), round(item['max'] - item['range'][0], 8)],
                               'errors': item['errors'], 'reason': item['reason'], 'next_step': item['next_step']})
    missing = [k for k in CAPS if k not in scores]
    gates = {}
    review = record['material_review']
    for section in CAPS:
        c = cold.get('written' if section in WRITTEN else 'listening_speaking', False)
        material_gate = review.get('section_gates', {}).get(section, 'fresh_review_required')
        score = scores.get(section)
        gates[section] = {'condition_eligible': c, 'material_gate': material_gate,
                          'score_basis': score['basis'] if score else 'unknown',
                          'interpretation': '缺少证据' if not score else '报告成绩' if score['basis'] in {'user_report', 'verified_report'}
                          else '暂估/受限' if score['basis'] == 'rubric_estimate' or not c or material_gate != 'key_crosschecked' or not review.get('identity_matches')
                          else '该分项冷测证据（非官方认证）'}
    return {'record_id': record['record_id'], 'mode': record['mode'], 'material': record['material'],
            'supersedes': record.get('supersedes'), 'correction_reason': record.get('correction_reason'),
            'occurred_at': record['occurred_at'], 'scores': rows, 'target': target,
            'target_status': target_status, 'full_score_available': rows['total']['score'] is not None,
            'unmeasured_leaf_sections': missing, 'cold_conditions': cold,
            'strict_official_equivalence': False, 'material_review': record['material_review'],
            'section_evidence': gates, 'baseline_protocol': record['protocol'],
            'limitations': record['limitations'], 'lost_score_evidence': errors, 'next_step': record['next_step'],
            'warnings': ['区间是评分/缺项边界，不是统计置信区间或考试概率。', '条件符合不等于题卷、rubric 或阅卷已获官方认证。',
                         '导入成绩可以作为历史证据，但不会变成教练现场未见题冷测。']}


def leaves(group):
    require(group in GROUPS, 'Unknown score group')
    return {group} if group in CAPS else set().union(*(leaves(c) for c in GROUPS[group]))


def scenario(record, replacements, target=UNSET):
    require(isinstance(replacements, dict) and replacements, 'Provide same-attempt replacement score ranges')
    base = known_score(record['scores'], 'total')
    require(base is not None, 'No full-score evidence: report bounds instead of inventing a scenario total')
    used, before, after, cap = set(), [0, 0], [0, 0], 0
    for group, value in replacements.items():
        subset = leaves(group)
        require(not used & subset, 'Overlapping replacements would double-count improvement')
        used |= subset
        maximum = sum(CAPS[k] for k in subset)
        interval(value, maximum)
        old = known_score(record['scores'], group)
        require(old is not None, 'Unmeasured component cannot be replaced or inferred from an aggregate')
        before = [before[e] + old[e] for e in (0, 1)]
        after = [after[e] + value[e] for e in (0, 1)]
        cap += maximum
    residual = [max(0, base[0] - before[1]), min(150 - cap, base[1] - before[0])]
    result = [round(residual[e] + after[e], 8) for e in (0, 1)]
    target = record['settings_snapshot']['target_score'] if target is UNSET else target
    if target is not None:
        number(target, 150)
    return {'record_id': record['record_id'], 'scenario_range': result, 'target': target,
            'meets_target_throughout_range': target is not None and result[0] >= target,
            'assumption': '只替换指定分项，其他分项保持这同一次表现；区间为保守算术包络，不是预计增分或达标概率。'}


def compare(first, second):
    changes = {}
    for section in MAXIMA:
        a, b = known_score(first['scores'], section), known_score(second['scores'], section)
        changes[section] = None if a is None or b is None else [round(b[0] - a[1], 8), round(b[1] - a[0], 8)]
    flags = []
    if first['material']['sha256'] == second['material']['sha256']:
        flags.append('同卷/重测可能曝光，不是独立新材料。')
    else:
        flags.append('不同卷未做难度等值，分差不能直接等同能力增量。')
    if first['conditions'] != second['conditions']:
        flags.append('作答条件/时长不同；逐项对齐后解释。')
    flags += ['不同评分者或主观 rubric 可能形成评分差异。', '不拼接各次最好分项；不输出达标概率。']
    return {'from': first['record_id'], 'to': second['record_id'], 'change_ranges': changes, 'comparability_limits': flags}


def history(state, on=None):
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(state['settings']['timezone'])
    records = sorted(active(state).values(), key=lambda r: stamp(r['occurred_at']))
    if on:
        day = date.fromisoformat(on)
        records = [r for r in records if stamp(r['occurred_at']).astimezone(tz).date() <= day]
    return [report(r, state['settings']['target_score']) for r in records]
