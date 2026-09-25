"""CET-4 evidence accounting, never a raw-to-710 conversion or score predictor."""
import copy
import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from cet4_store import exact_keys, identifier, nonempty, require, timestamp

# Fixed paper blueprint; weights are percentage points, NOT reported CET points.
PARTS = {'news': (7, 7), 'conversations': (8, 8), 'passages': (10, 20),
         'banked_cloze': (10, 5), 'matching': (10, 10), 'careful_reading': (10, 20),
         'writing': (15, 15), 'translation': (15, 15)}
BLOCKS = {'writing': ('writing',), 'listening': ('news', 'conversations', 'passages'),
          'reading': ('banked_cloze', 'matching', 'careful_reading'), 'translation': ('translation',)}
LIMITS = {'writing': 1800, 'listening': 1500, 'reading': 2400, 'translation': 1800}
SCORE_MAX = {'total': 710, 'listening': 249, 'reading': 249, 'writing_translation': 212}
ORAL_DIMS = {'exchange', 'opinion', 'description', 'accuracy_range', 'discourse', 'flexibility',
             'read_accuracy', 'read_fluency', 'read_completeness', 'response', 'negotiation', 'completion'}
STAGES = {'intro': 20, 'read_prep': 45, 'read': 60, 'short_1': 20, 'short_2': 20,
          'statement_prep': 45, 'statement': 60, 'interaction_prep': 60, 'interaction': 180}
STATUSES = {'OBSERVED', 'PARTIALLY_OBSERVED', 'NOT_OBSERVED', 'NOT_ELICITED', 'INDETERMINATE'}


def number(value, low, high):
    require(type(value) in (int, float) and math.isfinite(value) and low <= value <= high, 'Number outside valid range')


def boolean(value):
    require(type(value) is bool, 'Expected boolean, not unknown/string/integer')


def strings(values):
    require(isinstance(values, list) and all(isinstance(x, str) and x.strip() for x in values), 'Expected list of nonempty text')


def occurred(value, now):
    require(timestamp(value) <= now + timedelta(minutes=5), 'Future evidence is not an observation')


def material(data):
    exact_keys(data, ['material_id', 'title', 'kind', 'locator', 'sha256', 'exam_session',
                      'verified_components', 'answer_status', 'audio_status', 'issues'])
    identifier(data['material_id'])
    for k in ('title', 'locator', 'exam_session'):
        nonempty(data[k], k)
    require(data['kind'] in {'official_sample', 'publisher_reconstruction', 'user_mock', 'original'}, 'Invalid material kind')
    require(isinstance(data['sha256'], str) and re.fullmatch('[0-9a-f]{64}', data['sha256']), 'Freeze actual file SHA-256 before assessment')
    strings(data['verified_components'])
    require(len(set(data['verified_components'])) == len(data['verified_components']) and set(data['verified_components']) <= PARTS.keys(), 'Invalid verified components')
    require(data['answer_status'] in {'checked', 'partial', 'unverified'}, 'Invalid answer status')
    require(data['audio_status'] in {'matched', 'unverified', 'missing'}, 'Invalid audio status')
    strings(data['issues'])


def sections(data):
    require(isinstance(data, dict) and data.keys() <= PARTS.keys(), 'Unknown paper component')
    for key, value in data.items():
        if value is None:
            continue
        exact_keys(value, ['value', 'basis', 'evidence'])
        nonempty(value['evidence'], 'scoring evidence/answer locator')
        if key in {'writing', 'translation'}:
            require(value['basis'] == 'rubric_estimate', 'Subjective practice uses a 0–15 raw rubric range')
            require(isinstance(value['value'], list) and len(value['value']) == 2, 'Subjective score requires [low, high]')
            for x in value['value']:
                number(x, 0, 15)
            require(value['value'][0] <= value['value'][1], 'Reversed rubric range')
        else:
            require(value['basis'] in {'key_checked', 'self_report'}, 'Objective score requires key basis')
            require(type(value['value']) is int and 0 <= value['value'] <= PARTS[key][0], 'Correct count outside question count')


def conditions(data):
    require(isinstance(data, dict) and data.keys() <= BLOCKS.keys(), 'Unknown block')
    for block, c in data.items():
        exact_keys(c, ['completed', 'elapsed_seconds', 'timer_verified', 'support', 'technical_failure', 'note'],
                   ['play_count', 'transcript_seen', 'audio_matched'])
        for k in ('completed', 'timer_verified', 'technical_failure'):
            boolean(c[k])
        require(c['elapsed_seconds'] is None or type(c['elapsed_seconds']) in (int, float), 'Invalid elapsed time')
        if c['elapsed_seconds'] is not None:
            number(c['elapsed_seconds'], 0.001, 86400)
        require(not c['timer_verified'] or c['elapsed_seconds'] is not None, 'Timer requires measurement')
        require(c['support'] in {'none', 'location', 'cue', 'rule', 'model', 'unknown'}, 'Invalid support')
        require(isinstance(c['note'], str), 'note must be text')
        if block == 'listening':
            require(type(c.get('play_count')) is int and c['play_count'] >= 0, 'Listening needs play count')
            boolean(c.get('transcript_seen'))
            boolean(c.get('audio_matched'))


def official(data):
    exact_keys(data, [*SCORE_MAX, 'oral_grade', 'verification', 'locator'])
    for k, maximum in SCORE_MAX.items():
        require(data[k] is None or type(data[k]) is int and 0 <= data[k] <= maximum, 'Invalid reported score')
    require(any(data[k] is not None for k in SCORE_MAX) or data['oral_grade'] is not None, 'Empty report')
    require(data['verification'] in {'verified', 'self_report'}, 'Invalid verification')
    require(data['oral_grade'] in {None, '优秀', '良好', '合格', '不合格', 'A+', 'A', 'B+', 'B', 'C+', 'C', 'D'}, 'Unknown oral grade')
    nonempty(data['locator'], 'report locator; no candidate ID required')
    parts = [data[k] for k in ('listening', 'reading', 'writing_translation')]
    if data['total'] is not None and all(x is not None for x in parts):
        require(sum(parts) == data['total'], 'Reported total disagrees with reported components')
    if data['total'] is not None:
        require(sum(x or 0 for x in parts) <= data['total'], 'Known components exceed total')
        require(sum(SCORE_MAX[k] if data[k] is None else data[k] for k in ('listening', 'reading', 'writing_translation')) >= data['total'], 'Total impossible given components')


def active(state):
    hidden = {v['record_id'] for v in state.get('assessment_voids', {}).values()}
    hidden |= {a['supersedes'] for a in state.get('assessments', {}).values() if a.get('supersedes')}
    return {k: v for k, v in state.get('assessments', {}).items() if k not in hidden}


def mutate(state, command, p, now):
    for key in ('assessment_starts', 'assessments', 'assessment_voids', 'oral_records'):
        state.setdefault(key, {})
    event = p['event_id']
    if command == 'assessment-start':
        exact_keys(p, ['event_id', 'material', 'prior_exposure'])
        material(p['material'])
        boolean(p['prior_exposure'])
        m = p['material']
        seen = any(x['material']['sha256'] == m['sha256'] or x['material']['material_id'] == m['material_id']
                   for x in state['assessment_starts'].values())
        seen |= any(x.get('material', {}).get('sha256') == m['sha256'] or x.get('material', {}).get('material_id') == m['material_id']
                    for x in state['assessments'].values())
        start = {'start_id': event, 'material': copy.deepcopy(m), 'unseen': not seen and not p['prior_exposure'],
                 'started_at': now.isoformat(), 'settings_snapshot': copy.deepcopy(state['settings'])}
        state['assessment_starts'][event] = start
        return {'start_id': event, 'unseen': start['unseen']}
    if command == 'assessment-void':
        exact_keys(p, ['event_id', 'record_id', 'reason'])
        require(p['record_id'] in state['assessments'] or p['record_id'] in state['oral_records'], 'Unknown record')
        nonempty(p['reason'], 'withdrawal reason')
        state['assessment_voids'][event] = copy.deepcopy(p) | {'at': now.isoformat()}
        return {'record_id': p['record_id'], 'withdrawn': True}
    if command == 'oral-record':
        return record_oral(state, p, now)
    if command == 'assessment-correct':
        exact_keys(p, ['event_id', 'record_id', 'replacement', 'reason'])
        require(p['record_id'] in active(state), 'Only current active records can be corrected')
        nonempty(p['reason'], 'correction reason')
        old = active(state)[p['record_id']]
        record = copy.deepcopy(old)
        if old['kind'] == 'official_report':
            official(p['replacement'])
            record['report'] = copy.deepcopy(p['replacement'])
        else:
            sections(p['replacement'])
            record['sections'] = copy.deepcopy(p['replacement'])
        record.update(record_id=event, supersedes=p['record_id'], correction_reason=p['reason'])
    elif command == 'assessment-record':
        exact_keys(p, ['event_id', 'start_id', 'occurred_at', 'sections', 'conditions', 'execution', 'limitations', 'next_step'])
        require(p['start_id'] in state['assessment_starts'], 'Unknown assessment start')
        require(not any(a.get('start_id') == p['start_id'] for a in state['assessments'].values()), 'Attempt already recorded; use correction, not another observation')
        start = state['assessment_starts'][p['start_id']]
        exact_keys(p['execution'], ['single_sitting', 'order_verified', 'closed_blocks_respected'])
        for value in p['execution'].values():
            boolean(value)
        require(timestamp(p['occurred_at']) >= timestamp(start['started_at']), 'Evidence predates assessment start')
        record = copy.deepcopy(p) | copy.deepcopy(start) | {'kind': 'coach_run', 'record_id': event, 'supersedes': None}
    elif command == 'assessment-import':
        exact_keys(p, ['event_id', 'occurred_at', 'kind', 'title', 'limitations', 'next_step'],
                   ['report', 'material', 'sections', 'conditions'])
        require(p['kind'] in {'official_report', 'practice_import'}, 'Unknown import type')
        record = copy.deepcopy(p) | {'record_id': event, 'supersedes': None,
                                    'settings_snapshot': copy.deepcopy(state['settings'])}
        if p['kind'] == 'official_report':
            require(not any(k in p for k in ('material', 'sections', 'conditions')), 'Keep reported scores separate from raw practice')
            official(p.get('report'))
        else:
            require('report' not in p, 'Practice cannot contain a 710 report')
            material(p.get('material'))
    else:
        raise ValueError('Unsupported assessment command')
    occurred(record['occurred_at'], now)
    strings(record['limitations'])
    nonempty(record['next_step'], 'next_step')
    if record['kind'] != 'official_report':
        sections(record['sections'])
        conditions(record['conditions'])
        record['title'] = record['material']['title']
    nonempty(record['title'], 'assessment title')
    record['recorded_at'] = now.isoformat()
    state['assessments'][event] = record
    return report(record, state['settings']['target_score'])


def report(record, target=None):
    out = {k: copy.deepcopy(record.get(k)) for k in ('record_id', 'title', 'kind', 'occurred_at', 'supersedes', 'limitations', 'next_step')}
    out.update(target_score=target, forecast_710=None, oral_in_written_total=False)
    if record['kind'] == 'official_report':
        r = copy.deepcopy(record['report'])
        out.update(reported=r, observed_gap=None if target is None or r['total'] is None else target-r['total'],
                   interpretation='observed_report_not_prediction' if r['verification'] == 'verified' else 'unverified_self_report_not_baseline')
        return out
    raw = record['sections']
    weighted = [0.0, 0.0]
    unknown = []
    for key, (maximum, weight) in PARTS.items():
        entry = raw.get(key)
        if entry is None:
            unknown.append(key)
            continue
        v = entry['value']
        v = v if isinstance(v, list) else [v, v]
        for i in (0, 1):
            weighted[i] += v[i] / maximum * weight
    weighted = [round(x, 3) for x in weighted]
    cold = {}
    for block, parts in BLOCKS.items():
        c = record['conditions'].get(block)
        ok = record['kind'] == 'coach_run' and record.get('unseen', False) and c is not None
        if ok:
            ok = c['completed'] and c['timer_verified'] and c['elapsed_seconds'] <= LIMITS[block] and c['support'] == 'none' and not c['technical_failure']
            ok &= all(raw.get(k) is not None and k in record['material']['verified_components'] for k in parts)
            ok &= all(raw[k]['basis'] != 'self_report' for k in parts if raw.get(k))
            ok &= record['material']['answer_status'] == 'checked'
            if block == 'listening':
                ok &= c['play_count'] == 1 and not c['transcript_seen'] and c['audio_matched'] and record['material']['audio_status'] == 'matched'
        cold[block] = bool(ok)
    execution = record.get('execution', {})
    same_sitting = all(execution.get(k, False) for k in ('single_sitting', 'order_verified', 'closed_blocks_respected'))
    same_sitting &= 'started_at' in record and timestamp(record['occurred_at']) - timestamp(record['started_at']) <= timedelta(hours=4)
    if same_sitting:
        wall_seconds = (timestamp(record['occurred_at']) - timestamp(record['started_at'])).total_seconds()
        measured_seconds = sum(c['elapsed_seconds'] or 0 for c in record['conditions'].values())
        same_sitting = wall_seconds + 5 >= measured_seconds
    out.update(raw_sections=copy.deepcopy(raw), known_weighted_subtotal=weighted, unknown_components=unknown,
               weighted_practice_percent=None if unknown else weighted, unit='practice_percentage_points_not_710',
               cold_blocks=cold, complete_cold_written=bool(all(cold.values()) and same_sitting), observed_gap=None,
               interpretation='uncalibrated_practice_not_score_prediction')
    return out


def history(state, on):
    tz = ZoneInfo(state['settings']['timezone'])
    records = sorted(active(state).values(), key=lambda r: timestamp(r['occurred_at']))
    return [report(r, state['settings']['target_score']) for r in records
            if timestamp(r['occurred_at']).astimezone(tz).date().isoformat() <= on]


def compare(first, second):
    require(first['record_id'] != second['record_id'], 'Choose two observations')
    require(timestamp(second['occurred_at']) > timestamp(first['occurred_at']), 'A correction or same-time record is not learning progress')
    out = {'first': first['record_id'], 'second': second['record_id'], 'forecast_710': None,
           'causal_improvement_claim': False, 'limitations': ['Difficulty, support and occasion may differ; scores do not establish learning causality.']}
    if first['kind'] == second['kind'] == 'official_report':
        out['unit'] = 'reported_points'
        out['verification'] = [first['report']['verification'], second['report']['verification']]
        out['deltas'] = {k: None if first['report'][k] is None or second['report'][k] is None else second['report'][k]-first['report'][k] for k in SCORE_MAX}
    elif first['kind'] != 'official_report' and second['kind'] != 'official_report':
        out['unit'] = 'raw_counts_or_15_point_rubric_ranges'
        out['components'] = {k: [first['sections'].get(k), second['sections'].get(k)] for k in PARTS}
        out['cold_blocks'] = [report(first)['cold_blocks'], report(second)['cold_blocks']]
        out['limitations'].append('Raw differences across forms are not equated reported-score gains.')
    else:
        raise ValueError('Cannot compare raw practice units with 710 reported points')
    return out


def scenario(record, payload, target):
    require(record['kind'] == 'official_report' and record['report']['verification'] == 'verified', 'Scenario needs verified reported components, never raw-to-710 conversion')
    exact_keys(payload, ['assumed_reported_components'])
    assumptions = payload['assumed_reported_components']
    require(isinstance(assumptions, dict) and assumptions and assumptions.keys() <= {'listening', 'reading', 'writing_translation'}, 'Use reported component assumptions')
    values = {k: record['report'][k] for k in ('listening', 'reading', 'writing_translation')}
    for k, v in assumptions.items():
        require(type(v) is int and 0 <= v <= SCORE_MAX[k], 'Invalid assumed reported score')
        values[k] = v
    require(all(v is not None for v in values.values()), 'Unknown components cannot be filled as zero')
    total = sum(values.values())
    return {'hypothetical_reported_total': total, 'components': values, 'gap': None if target is None else target-total,
            'assumptions': assumptions, 'prediction': False, 'warning': 'Arithmetic only; all-correct practice does not establish a reported component score.'}


def record_oral(state, p, now):
    exact_keys(p, ['event_id', 'occurred_at', 'pack_id', 'topic', 'prior_exposure', 'support', 'modality',
                   'audio_observed', 'partner', 'speaker_attribution', 'timekeeper', 'dry_run_user', 'dry_run_partner',
                   'stage_reveal', 'technical_failure', 'task_times', 'observations', 'receipt_locator', 'next_step'])
    occurred(p['occurred_at'], now)
    identifier(p['pack_id'])
    for k in ('topic', 'receipt_locator', 'next_step'):
        nonempty(p[k], k)
    for k in ('prior_exposure', 'audio_observed', 'dry_run_user', 'dry_run_partner', 'stage_reveal', 'technical_failure'):
        boolean(p[k])
    require(p['support'] in {'none', 'light', 'structured', 'modelled'}, 'Invalid oral support')
    require(p['modality'] in {'live_voice', 'recording', 'text_only'}, 'Invalid oral modality')
    require(not p['audio_observed'] or p['modality'] != 'text_only', 'Text is not observed audio')
    require(p['partner'] in {'none', 'human', 'ai:balanced', 'ai:terse', 'ai:talkative', 'ai:dissenting', 'ai:repairable'}, 'Unknown partner')
    require(p['speaker_attribution'] in {'reliable', 'unreliable', 'unknown', 'not_applicable'}, 'Invalid speaker attribution')
    require(p['timekeeper'] in {'external_timer', 'human_timekeeper', 'approximate', 'unknown'}, 'Invalid timekeeper')
    require(isinstance(p['task_times'], dict) and p['task_times'].keys() <= STAGES.keys(), 'Unknown oral stage')
    for v in p['task_times'].values():
        number(v, 0.001, 86400)
    require(isinstance(p['observations'], dict) and p['observations'].keys() == ORAL_DIMS, 'All oral dimensions required; use NOT_ELICITED/INDETERMINATE for gaps')
    for v in p['observations'].values():
        exact_keys(v, ['status', 'evidence'])
        require(v['status'] in STATUSES, 'Unknown observation status')
        nonempty(v['evidence'], 'observation basis or limitation')
    heard = p['audio_observed'] and p['modality'] != 'text_only'
    if not heard:
        require(all(v['status'] == 'INDETERMINATE' for v in p['observations'].values()), 'No observed audio: oral constructs must remain indeterminate')
    seen = p['prior_exposure'] or any(r['pack_id'] == p['pack_id'] for r in state['oral_records'].values())
    independent = heard and not seen and p['support'] == 'none' and not p['technical_failure']
    timer = p['timekeeper'] in {'external_timer', 'human_timekeeper'} and p['dry_run_user'] and p['dry_run_partner']
    attributable = p['partner'] != 'human' or p['speaker_attribution'] == 'reliable'
    strict = independent and attributable and timer and p['stage_reveal'] and p['partner'] != 'none'
    full = p['task_times'].keys() == STAGES.keys() and all(v <= STAGES[k] for k, v in p['task_times'].items())
    aligns = strict and full and all(v['status'] == 'OBSERVED' for v in p['observations'].values())
    human_check = strict and p['partner'] == 'human' and 0 < p['task_times'].get('interaction', 0) <= 180
    human_check &= all(p['observations'][k]['status'] == 'OBSERVED' for k in ('exchange', 'accuracy_range', 'discourse', 'flexibility', 'response', 'negotiation', 'completion'))
    states = {v['status'] for v in p['observations'].values()}
    alignment = 'indeterminate'
    if independent and attributable:
        if aligns:
            alignment = 'demonstrated_once'
        elif states & {'OBSERVED', 'PARTIALLY_OBSERVED'}:
            alignment = 'partial'
        elif 'NOT_OBSERVED' in states:
            alignment = 'not_demonstrated'
    saved = copy.deepcopy(p) | {'record_id': p['event_id'], 'strict_full': bool(strict and full),
                                'alignment': alignment,
                                'human_check': bool(human_check), 'official_grade': None}
    state['oral_records'][p['event_id']] = saved
    return {k: saved[k] for k in ('record_id', 'strict_full', 'alignment', 'human_check', 'official_grade')}


def oral_history(state, on):
    tz = ZoneInfo(state['settings']['timezone'])
    voids = {v['record_id'] for v in state.get('assessment_voids', {}).values()}
    rows = [r for k, r in state.get('oral_records', {}).items() if k not in voids and timestamp(r['occurred_at']).astimezone(tz).date().isoformat() <= on]
    # Training policy: recent contradictory strict evidence resets the stability claim.
    # Thirty days is a review window, not an official CET validity period.
    today = datetime.fromisoformat(on).date()
    recent = [r for r in rows if (today - timestamp(r['occurred_at']).astimezone(tz).date()).days <= 30]
    counter = [timestamp(r['occurred_at']) for r in recent if r['strict_full'] and r['alignment'] != 'demonstrated_once']
    cutoff = max(counter) if counter else datetime.min.replace(tzinfo=timezone.utc)
    good = [r for r in recent if timestamp(r['occurred_at']) > cutoff and r['alignment'] == 'demonstrated_once' and r['partner'].startswith('ai:')]
    repeated = any(a['topic'].casefold() != b['topic'].casefold() and a['partner'] != b['partner'] and
                   timestamp(a['occurred_at']).astimezone(tz).date() != timestamp(b['occurred_at']).astimezone(tz).date()
                   for a in good for b in good)
    human = any(r['human_check'] and timestamp(r['occurred_at']) > cutoff for r in recent)
    return {'records': [{k: r[k] for k in ('record_id', 'occurred_at', 'pack_id', 'topic', 'partner', 'alignment', 'strict_full', 'receipt_locator', 'next_step')} for r in rows],
            'stability': 'human_partner_observed' if repeated and human else 'repeated_ai_consistent' if repeated else 'single_observation' if good else 'insufficient',
            'excellent_evidence_gate': bool(repeated and human), 'official_grade': None,
            'review_window_days': 30, 'counterevidence_reset': bool(counter),
            'warning': 'Training policy only; recent strict counterevidence resets the claim. Never a grade prediction.'}


def catalogue():
    return json.loads((Path(__file__).resolve().parents[1] / 'assets/material-catalog.json').read_text())
