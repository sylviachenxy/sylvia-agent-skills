"""Synthetic evidence only. No real learner registry, network or external writes."""
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import assessment as assess
import tutor_store as store


def material(digest='1' * 64):
    return {'id': 'SYNTHETIC', 'title': 'Synthetic test — not a student result', 'kind': 'original',
            'locator': 'synthetic unit test', 'sha256': digest, 'year': 2026, 'session': 'mock',
            'normalization': 'Synthetic 115 + 35 working blueprint, not an official paper'}


def protocol():
    return {'baseline_status': 'provisional', 'baseline_source': 'Synthetic format fixture',
            'checked_on': '2026-01-01', 'scoring_plan': 'Synthetic isolated test rubric, not official scoring'}


def condition(group='written', **changes):
    return {'support': 'unknown', 'timing': 'unknown', 'elapsed_seconds': None,
            'audio': 'none' if group == 'written' else 'unknown', 'evidence': 'Synthetic condition observation'} | changes


def grade(low, high=None, basis='user_report'):
    return {'range': [low, low if high is None else high], 'basis': basis,
            'source': 'Synthetic score source', 'rubric': 'Synthetic stated denominator', 'items': []}


def section_grade(section, loss=0, uncertain=False):
    items = []
    for n, weight in enumerate(assess.COUNTS.get(section, [assess.CAPS[section]])):
        deficit = min(loss, weight); loss -= deficit
        low = weight - deficit
        high = min(weight, low + 1) if uncertain and n == 0 else low
        q = f'Q{assess.Q_RANGES[section][n]}' if section in assess.Q_RANGES else f'{section}-{n+1}'
        items.append({'id': q, 'max': weight, 'range': [low, high],
                      'evidence_kind': 'observation', 'response': 'Synthetic student observation',
                      'reason': 'Synthetic rubric comparison', 'errors': ['synthetic-gap'] if deficit else [],
                      'next_step': 'Synthetic independent transfer check'})
    return {'range': [sum(i['range'][e] for i in items) for e in (0, 1)],
            'basis': 'rubric_estimate' if uncertain else 'key_checked',
            'source': 'Synthetic verified key', 'rubric': 'Synthetic item rubric', 'items': items}


class AssessmentTests(unittest.TestCase):
    def setUp(self):
        fixture = patch.object(assess, 'catalogue', return_value={'schema_version': 1, 'materials': []})
        fixture.start()
        self.addCleanup(fixture.stop)
        self.state = store.new_state('synthetic', {'target_score': 140})
        self.now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        self.serial = 0

    def mutate(self, command, payload):
        self.serial += 1
        payload = {'event_id': f'E-{self.serial}'} | payload
        self.state, result = store.apply(self.state, command, payload, self.now)
        return result

    def import_result(self, scores, **changes):
        payload = {'material': material(), 'protocol': protocol(), 'occurred_at': self.now.isoformat(),
                   'conditions': {'written': condition(), 'listening_speaking': condition('listening_speaking')},
                   'scores': scores, 'limitations': ['Synthetic evidence only'], 'next_step': 'Collect missing evidence',
                   'provenance': 'Synthetic historical grade fixture'} | changes
        return self.mutate('assessment-import', payload)

    def start(self, **changes):
        return self.mutate('assessment-start', {'material': material(), 'protocol': protocol(),
                           'scope': ['written'], 'prior_exposure': 'unseen'} | changes)['assessment_id']

    def record(self, start, scores, **changes):
        return self.mutate('assessment-record', {'assessment_id': start, 'occurred_at': self.now.isoformat(),
                           'conditions': {'written': condition(support='none', timing='verified', elapsed_seconds=600)},
                           'scores': scores, 'limitations': ['Synthetic estimates only'], 'next_step': 'Independent check'} | changes)

    def test_115_plus_25_plus_10_and_eight_written_sections(self):
        self.assertEqual(sum(assess.CAPS.values()), 150)
        self.assertEqual(sum(assess.CAPS[k] for k in assess.WRITTEN), 115)
        self.assertEqual(len(assess.WRITTEN), 8)

    def test_partial_written_104_bounds_104_to_139(self):
        r = self.import_result({'written': grade(104)})['assessment']
        self.assertIsNone(r['scores']['total']['score'])
        self.assertEqual(r['scores']['total']['possible_bounds'], [104, 139])
        self.assertEqual(r['target_status'], 'below_even_with_unknowns_full')

    def test_written_108_unknown_speaking_not_default_full(self):
        r = self.import_result({'written': grade(108)})['assessment']
        self.assertEqual(r['scores']['total']['possible_bounds'], [108, 143])
        self.assertFalse(r['full_score_available'])
        self.assertIsNone(r['scores']['oral']['score'])

    def test_aggregate_32_does_not_invent_24_and_8(self):
        r = self.import_result({'written': grade(108), 'listening_speaking': grade(32)})['assessment']
        self.assertEqual(r['scores']['total']['score'], [140, 140])
        self.assertIsNone(r['scores']['listening']['score'])
        self.assertIsNone(r['scores']['oral']['score'])
        self.assertEqual(r['scores']['oral']['possible_bounds'], [7, 10])

    def test_reported_total_alone_remains_no_section_diagnosis(self):
        r = self.import_result({'total': grade(140)}, conditions={})['assessment']
        self.assertTrue(r['full_score_available'])
        self.assertEqual(len(r['unmeasured_leaf_sections']), 10)
        self.assertFalse(r['strict_official_equivalence'])

    def test_verbal_historical_report_needs_no_fake_file_or_year(self):
        m = material(); m.update(sha256=None, year=None, kind='unknown')
        r = self.import_result({'total': grade(130)}, material=m, conditions={})['assessment']
        self.assertEqual(r['scores']['total']['score'], [130, 130])
        self.assertFalse(r['cold_conditions'])
        with self.assertRaises(ValueError):
            self.start(material=m)

    def test_output_losses_thirteen_cap_total_137(self):
        r = self.import_result({'summary': grade(7), 'translation': grade(11), 'writing': grade(19)})['assessment']
        self.assertEqual(r['scores']['total']['possible_bounds'][1], 137)

    def test_inconsistent_parent_and_children_fail(self):
        for scores in ({'written': grade(115), 'writing': grade(20)},
                       {'total': grade(140), 'written': grade(100)},
                       {'listening_speaking': grade(32), 'listening': grade(20)}):
            with self.subTest(scores=scores), self.assertRaises(ValueError):
                self.import_result(scores)

    def test_invalid_numbers_and_extra_fields_fail(self):
        for value in (True, float('nan'), float('inf'), -1, 151):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.import_result({'total': grade(value)})
        with self.assertRaises(ValueError):
            self.import_result({'total': grade(140) | {'probability': .95}})

    def test_reversed_interval_and_unknown_sections_fail(self):
        with self.assertRaises(ValueError):
            self.import_result({'written': grade(108, 104)})
        with self.assertRaises(ValueError):
            self.import_result({'ielts': grade(8)})

    def test_rounding_cannot_turn_below_target_into_meeting(self):
        with self.assertRaisesRegex(ValueError, 'two decimal'):
            self.import_result({'written': grade(114.999999999, 115), 'listening_speaking': grade(25)})
        r = self.import_result({'written': grade(114.99), 'listening_speaking': grade(25)})['assessment']
        self.assertEqual(r['scores']['total']['score'], [139.99, 139.99])
        self.assertEqual(r['target_status'], 'below_even_with_unknowns_full')

    def test_132_reading_24_to_full_only_138(self):
        rid = self.import_result({'total': grade(132), 'reading_abc': grade(18), 'reading_gap': grade(6)})['record_id']
        r = assess.scenario(self.state['assessments'][rid], {'reading': [30, 30]})
        self.assertEqual(r['scenario_range'], [138, 138])
        self.assertFalse(r['meets_target_throughout_range'])

    def test_136_reading_26_to_full_conditionally_140(self):
        rid = self.import_result({'total': grade(136), 'reading_abc': grade(20), 'reading_gap': grade(6)})['record_id']
        r = assess.scenario(self.state['assessments'][rid], {'reading': [30, 30]})
        self.assertEqual(r['scenario_range'], [140, 140])

    def test_scenario_missing_total_or_subscore_rejected(self):
        for scores, changes in (({'written': grade(104)}, {'written': [115, 115]}),
                                ({'total': grade(140)}, {'oral': [10, 10]})):
            rid = self.import_result(scores)['record_id']
            with self.assertRaises(ValueError):
                assess.scenario(self.state['assessments'][rid], changes)

    def test_overlapping_scenario_refused(self):
        rid = self.import_result({'total': grade(136), 'reading_abc': grade(20), 'reading_gap': grade(6)})['record_id']
        with self.assertRaises(ValueError):
            assess.scenario(self.state['assessments'][rid], {'reading': [30, 30], 'reading_gap': [8, 8]})

    def test_scenario_retains_subjective_range(self):
        rid = self.import_result({'total': grade(132, 136), 'writing': grade(19, 22)})['record_id']
        r = assess.scenario(self.state['assessments'][rid], {'writing': [24, 25]})
        self.assertEqual(r['scenario_range'], [134, 142])
        self.assertFalse(r['meets_target_throughout_range'])

    def test_import_is_never_live_cold_measurement(self):
        r = self.import_result({'written': grade(110)}, conditions={'written': condition(support='none', timing='verified', elapsed_seconds=6000)})['assessment']
        self.assertFalse(r['cold_conditions']['written'])

    def test_begin_before_scoring_and_no_answer_exposed(self):
        start = self.start()
        self.assertNotIn('key', self.state['assessment_starts'][start])
        self.now += timedelta(seconds=600)
        r = self.record(start, {'grammar': section_grade('grammar')})['assessment']
        self.assertTrue(r['cold_conditions']['written'])
        self.assertFalse(r['full_score_available'])

    def test_complete_synthetic_run_preserves_all_ten_sections_and_uncertainty(self):
        start = self.start(scope=['written', 'listening_speaking'])
        self.now += timedelta(seconds=8400)
        scores = {k: section_grade(k, loss=n) for k, n in
                  {'grammar': 1, 'vocabulary': 1, 'cloze': 1, 'reading_abc': 2, 'reading_gap': 0, 'listening': 1}.items()}
        scores.update({k: section_grade(k, loss=n, uncertain=True) for k, n in
                       {'summary': 1, 'translation': 2, 'writing': 4, 'oral': 2}.items()})
        r = self.record(start, scores, conditions={
            'written': condition(support='none', timing='verified', elapsed_seconds=6300),
            'listening_speaking': condition('listening_speaking', support='none', timing='verified',
                                            elapsed_seconds=2100, audio='observed')})
        report = r['assessment']
        self.assertEqual(report['scores']['written']['score'], [103, 106])
        self.assertEqual(report['scores']['total']['score'], [135, 139])
        self.assertEqual(report['unmeasured_leaf_sections'], [])
        self.assertTrue(report['cold_conditions']['listening_speaking'])
        self.assertFalse(report['strict_official_equivalence'])
        self.assertEqual(len(self.state['attempts']), 0)
        record = self.state['assessments'][r['record_id']]
        self.assertEqual(assess.scenario(record, {'reading': [30, 30]})['scenario_range'], [137, 141])

    def test_hints_unverified_timer_and_seen_block_cold_conditions(self):
        for changes in ({'support': 'hints'}, {'timing': 'reported'}, {'timing': 'untimed', 'elapsed_seconds': None}):
            start = self.start(material=material(str(self.serial % 10) * 64))
            self.now += timedelta(seconds=600)
            r = self.record(start, {'grammar': section_grade('grammar')},
                            conditions={'written': condition(support='none', timing='verified', elapsed_seconds=600) | changes})['assessment']
            self.assertFalse(r['cold_conditions']['written'])

    def test_repeat_paper_fingerprint_remains_seen_after_void(self):
        start = self.start()
        self.now += timedelta(seconds=600)
        rid = self.record(start, {'grammar': section_grade('grammar')})['record_id']
        self.mutate('assessment-void', {'record_id': rid, 'reason': 'Synthetic withdrawal'})
        next_start = self.start()
        self.assertFalse(self.state['assessment_starts'][next_start]['unseen'])

    def test_imported_same_paper_is_also_exposed(self):
        self.import_result({'total': grade(132)})
        start = self.start()
        self.assertFalse(self.state['assessment_starts'][start]['unseen'])

    def test_unknown_exposure_not_cold(self):
        start = self.start(prior_exposure='unknown')
        self.assertFalse(self.state['assessment_starts'][start]['unseen'])

    def test_clock_window_blocks_fake_measured_duration(self):
        start = self.start()
        with self.assertRaisesRegex(ValueError, 'durations exceed'):
            self.record(start, {'grammar': section_grade('grammar')})

    def test_section_totals_and_item_weights_checked(self):
        for damage in ('range', 'max', 'duplicate', 'missing'):
            score = section_grade('grammar')
            if damage == 'range': score['range'] = [9, 9]
            if damage == 'max': score['items'][0]['max'] = 2
            if damage == 'duplicate': score['items'][1]['id'] = score['items'][0]['id']
            if damage == 'missing': score['items'].pop()
            with self.subTest(damage=damage), self.assertRaises(ValueError):
                self.import_result({'grammar': score})

    def test_wrong_section_question_numbers_rejected(self):
        score = section_grade('grammar')
        for n, item in enumerate(score['items']): item['id'] = f'Q{n+11}'
        with self.assertRaisesRegex(ValueError, 'Canonical Q'):
            self.import_result({'grammar': score})

    def test_objective_estimate_cannot_smuggle_fractional_points(self):
        score = section_grade('grammar', loss=1, uncertain=True)
        score['items'][0]['range'] = [.5, 1]
        score['range'] = [9.5, 10]
        with self.assertRaisesRegex(ValueError, 'fractional credit'):
            self.import_result({'grammar': score})

    def test_quarantined_item_not_an_unrelated_question_must_be_unresolved(self):
        row = {'id': 'SYNTHETIC', 'question_sha256': '1' * 64, 'status': 'partial_diagnostic_only',
               'section_gates': {'grammar': 'quarantined'}, 'blocked_items': {'grammar': ['Q2']},
               'issues': ['Synthetic dispute'], 'use_limits': ['Synthetic']}
        with patch.object(assess, 'catalogue', return_value={'schema_version': 1, 'materials': [row]}):
            score = section_grade('grammar', loss=1, uncertain=True)  # Q1 uncertainty is not Q2
            for digest in ('1' * 64, '2' * 64):
                with self.subTest(digest=digest), self.assertRaisesRegex(ValueError, 'Quarantined item'):
                    self.import_result({'grammar': score}, material=material(digest))
            score = section_grade('grammar')
            score['items'][1]['range'] = [0, 1]; score['range'] = [9, 10]
            self.import_result({'grammar': score})

    def test_known_paper_sha_cannot_be_relabelled_to_evade_its_gates(self):
        row = {'id': 'OTHER-PAPER', 'question_sha256': '1' * 64, 'status': 'partial_diagnostic_only',
               'section_gates': {'grammar': 'quarantined'}, 'blocked_items': {'grammar': ['Q2']},
               'issues': ['Synthetic dispute'], 'use_limits': ['Synthetic']}
        with patch.object(assess, 'catalogue', return_value={'schema_version': 1, 'materials': [row]}):
            with self.assertRaisesRegex(ValueError, 'different catalogue ID'):
                self.import_result({'grammar': section_grade('grammar')})

    def test_removing_current_target_does_not_reactivate_old_snapshot(self):
        self.import_result({'total': grade(140)})
        self.mutate('configure', {'expected_revision': self.state['revision'], 'patch': {'target_score': None}})
        r = store.context(self.state)['assessments'][0]
        self.assertIsNone(r['target'])
        self.assertEqual(r['target_status'], 'no_target')

    def test_subjective_string_match_and_fake_precise_estimate_refused(self):
        with self.assertRaises(ValueError):
            self.import_result({'writing': section_grade('writing')})
        score = section_grade('writing'); score['basis'] = 'rubric_estimate'
        with self.assertRaises(ValueError):
            self.import_result({'writing': score})
        self.import_result({'writing': section_grade('writing', loss=4, uncertain=True)})

    def test_transcript_cannot_be_scored_as_hearing(self):
        with self.assertRaisesRegex(ValueError, 'actual audio'):
            self.import_result({'listening': section_grade('listening')},
                               conditions={'listening_speaking': condition('listening_speaking', audio='transcript')})

    def test_verified_external_listening_grade_does_not_require_audio_replay(self):
        r = self.import_result({'listening_speaking': grade(32, basis='verified_report')})['assessment']
        self.assertEqual(r['scores']['listening_speaking']['score'], [32, 32])
        self.assertFalse(r['cold_conditions']['listening_speaking'])

    def test_no_raw_response_when_privacy_disabled(self):
        self.mutate('configure', {'expected_revision': self.state['revision'], 'patch': {'store_quotes': False}})
        score = section_grade('grammar'); score['items'][0]['evidence_kind'] = 'quote'
        with self.assertRaisesRegex(ValueError, 'Raw quotes disabled'):
            self.import_result({'grammar': score})

    def test_correction_keeps_old_evidence_and_attempt_identity(self):
        rid = self.import_result({'total': grade(130)})['record_id']
        r = self.mutate('assessment-correct', {'record_id': rid, 'reason': 'Verified transcription mistake',
                        'scores': {'total': grade(132)}, 'limitations': ['Synthetic'], 'next_step': 'Check uncertainty'})
        new = self.state['assessments'][r['record_id']]
        self.assertEqual(len(assess.active(self.state)), 1)
        self.assertIn(rid, self.state['assessments'])
        self.assertEqual(new['occurred_at'], self.state['assessments'][rid]['occurred_at'])
        self.assertEqual(new['supersedes'], rid)
        self.assertEqual(new['event_id'], new['record_id'])
        self.assertFalse(new['unseen'])
        report = assess.report(new)
        self.assertEqual(report['supersedes'], rid)
        self.assertEqual(report['correction_reason'], 'Verified transcription mistake')
        self.assertIn('不是新的学习进步', store.view_content(self.state))

    def test_replay_collision_and_failed_mutation_are_safe(self):
        start = self.start()
        self.now += timedelta(seconds=600)
        self.record(start, {'grammar': section_grade('grammar')})
        before = copy.deepcopy(self.state)
        with self.assertRaises(ValueError):
            self.record(start, {'grammar': section_grade('grammar')})
        self.assertEqual(before, self.state)
        event = next(reversed(self.state['requests']))
        request = self.state['requests'][event]['request']
        replay = self.mutate(request['command'], request['payload'])
        self.assertTrue(replay['replayed'])
        self.assertEqual(before['revision'], self.state['revision'])

    def test_history_as_of_filters_without_turning_training_into_scores(self):
        self.import_result({'total': grade(130)})
        self.now += timedelta(days=1)
        self.import_result({'total': grade(135)}, material=material('2' * 64))
        data = store.context(self.state, '2026-01-01')
        self.assertEqual(len(data['assessments']), 1)
        self.assertEqual(data['active_attempt_count'], 0)

    def test_comparison_warns_without_equating_or_patching_best_scores(self):
        a = self.import_result({'total': grade(130)})['record_id']
        b = self.import_result({'total': grade(134, 138)}, material=material('2' * 64))['record_id']
        r = assess.compare(self.state['assessments'][a], self.state['assessments'][b])
        self.assertEqual(r['change_ranges']['total'], [4, 8])
        self.assertIsNone(r['change_ranges']['oral'])
        self.assertIn('未做难度等值', r['comparability_limits'][0])

    def test_disk_recovery_preserves_user_notes_and_structured_scores(self):
        rid = self.import_result({'written': grade(104)})['record_id']
        with tempfile.TemporaryDirectory(prefix='assessment-private-test-') as tmp:
            root = Path(tmp).resolve()
            store.persist(root, self.state)
            view = root / 'Practice.md'; view.write_text(view.read_text() + '\nMY NOTE\n')
            state = store.load_state(root)
            self.assertEqual(state['assessments'][rid]['scores']['written']['range'], [104, 104])
            store.persist(root, state)
            self.assertIn('MY NOTE', view.read_text())
            self.assertIn('未知', view.read_text())
            self.assertIn('[104, 139]', view.read_text())
            self.assertIn('文件未匹配', view.read_text())


if __name__ == '__main__':
    unittest.main()
