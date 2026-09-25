import copy
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import cet4_store as store
import assessment as assess


class Fixture(unittest.TestCase):
    def setUp(self):
        self.state = store.new_state('synthetic', {'target_score': 580, 'oral_target': '优秀'})
        self.now = datetime(2026, 9, 1, 2, tzinfo=timezone.utc)
        self.serial = 0

    def put(self, command, payload):
        self.serial += 1
        payload.setdefault('event_id', f'E{self.serial}')
        self.state, result = store.apply(self.state, command, payload, self.now)
        return result

    def material(self, **changes):
        return {'material_id': 'fixture-paper', 'title': 'Synthetic paper, not a real student', 'kind': 'original',
                'locator': 'synthetic-fixture', 'sha256': 'a' * 64, 'exam_session': 'synthetic',
                'verified_components': list(assess.PARTS), 'answer_status': 'checked', 'audio_status': 'matched', 'issues': []} | changes

    def raw(self):
        return {k: {'value': [12, 14] if k in {'writing', 'translation'} else count,
                    'basis': 'rubric_estimate' if k in {'writing', 'translation'} else 'key_checked',
                    'evidence': 'Synthetic criterion evidence, not an actual answer key.'}
                for k, (count, _) in assess.PARTS.items()}

    def conditions(self):
        data = {k: {'completed': True, 'elapsed_seconds': limit, 'timer_verified': True,
                    'support': 'none', 'technical_failure': False, 'note': 'Synthetic conditions.'}
                for k, limit in assess.LIMITS.items()}
        data['listening'].update(play_count=1, transcript_seen=False, audio_matched=True)
        return data

    def start(self, **m):
        return self.put('assessment-start', {'material': self.material(**m), 'prior_exposure': False})['start_id']

    def result(self, start, **changes):
        return {'start_id': start, 'occurred_at': self.now.isoformat(), 'sections': self.raw(),
                'conditions': self.conditions(), 'execution': {'single_sitting': True, 'order_verified': True, 'closed_blocks_respected': True},
                'limitations': ['Synthetic test only.'], 'next_step': 'Collect real unseen evidence.'} | changes

    def written(self, **changes):
        start = self.start()
        self.now += timedelta(minutes=125)
        return self.put('assessment-record', self.result(start, **changes))

    def official(self, **changes):
        values = {'total': 560, 'listening': 170, 'reading': 220, 'writing_translation': 170,
                  'oral_grade': '优秀', 'verification': 'verified', 'locator': 'Synthetic report fixture.'} | changes
        return self.put('assessment-import', {'kind': 'official_report', 'title': 'Synthetic official-score fixture',
            'occurred_at': self.now.isoformat(), 'report': values, 'limitations': ['Fixture only.'], 'next_step': 'Do a new diagnostic.'})

    def oral(self, **changes):
        return {'occurred_at': self.now.isoformat(), 'pack_id': f'PACK-{self.serial}', 'topic': 'Community',
                'prior_exposure': False, 'support': 'none', 'modality': 'recording', 'audio_observed': True,
                'partner': 'ai:balanced', 'speaker_attribution': 'not_applicable', 'timekeeper': 'external_timer',
                'dry_run_user': True, 'dry_run_partner': True, 'stage_reveal': True, 'technical_failure': False,
                'task_times': dict(assess.STAGES), 'observations': {k: {'status': 'OBSERVED', 'evidence': 'Synthetic observation at a stated task.'} for k in assess.ORAL_DIMS},
                'receipt_locator': 'synthetic receipt, no real audio', 'next_step': 'Collect real evidence.'} | changes

    def oral_summary(self):
        return assess.oral_history(self.state, self.now.date().isoformat())


class WrittenTests(Fixture):
    def test_blueprint_weights_and_raw_ranges(self):
        result = self.written()
        self.assertEqual(result['weighted_practice_percent'], [94, 98])
        self.assertTrue(result['complete_cold_written'])
        self.assertIsNone(result['forecast_710'])
        self.assertIsNone(result['observed_gap'])

    def test_missing_is_not_zero_or_full_percentage(self):
        raw = {'news': {'value': 0, 'basis': 'key_checked', 'evidence': 'Synthetic zero.'}}
        r = self.written(sections=raw)
        self.assertEqual(r['known_weighted_subtotal'], [0, 0])
        self.assertIsNone(r['weighted_practice_percent'])
        self.assertEqual(len(r['unknown_components']), 7)
        self.assertFalse(r['complete_cold_written'])

    def test_different_listening_types_have_different_weights(self):
        raw = {k: {'value': 1, 'basis': 'key_checked', 'evidence': 'Fixture.'} for k in ('news', 'conversations', 'passages')}
        r = self.written(sections=raw)
        self.assertEqual(r['known_weighted_subtotal'], [4, 4])

    def test_listening_replay_blocks_cold_only_that_block(self):
        c = self.conditions(); c['listening']['play_count'] = 2
        r = self.written(conditions=c)
        self.assertFalse(r['cold_blocks']['listening'])
        self.assertTrue(r['cold_blocks']['reading'])

    def test_transcript_or_unmatched_audio_blocks_cold(self):
        for key, value in [('transcript_seen', True), ('audio_matched', False)]:
            with self.subTest(key=key):
                self.setUp()
                c = self.conditions(); c['listening'][key] = value
                self.assertFalse(self.written(conditions=c)['cold_blocks']['listening'])

    def test_unaccepted_material_does_not_become_cold(self):
        start = self.start(answer_status='unverified', audio_status='missing')
        self.assertFalse(any(self.put('assessment-record', self.result(start))['cold_blocks'].values()))

    def test_only_verified_components_qualify(self):
        start = self.start(verified_components=['writing'])
        r = self.put('assessment-record', self.result(start))
        self.assertEqual(r['cold_blocks'], {'writing': True, 'listening': False, 'reading': False, 'translation': False})

    def test_overlong_or_assisted_block_is_not_cold(self):
        for changes in [{'elapsed_seconds': 2500}, {'support': 'cue'}, {'technical_failure': True}, {'timer_verified': False}, {'completed': False}]:
            with self.subTest(changes=changes):
                self.setUp()
                c = self.conditions(); c['reading'].update(changes)
                self.assertFalse(self.written(conditions=c)['cold_blocks']['reading'])

    def test_same_file_new_id_still_seen(self):
        self.written()
        r = self.put('assessment-start', {'material': self.material(material_id='different-id'), 'prior_exposure': False})
        self.assertFalse(r['unseen'])

    def test_same_identity_reformatted_file_still_seen(self):
        self.written()
        r = self.put('assessment-start', {'material': self.material(sha256='b' * 64), 'prior_exposure': False})
        self.assertFalse(r['unseen'])

    def test_abandoned_start_still_exposed(self):
        self.start()
        self.assertFalse(self.put('assessment-start', {'material': self.material(), 'prior_exposure': False})['unseen'])

    def test_start_cannot_be_used_twice_even_after_void(self):
        start = self.start()
        r = self.put('assessment-record', self.result(start))
        self.put('assessment-void', {'record_id': r['record_id'], 'reason': 'Synthetic correction.'})
        with self.assertRaises(ValueError):
            self.put('assessment-record', self.result(start))

    def test_cross_day_assembly_is_not_complete_baseline(self):
        start = self.start()
        self.now += timedelta(days=1)
        self.assertFalse(self.put('assessment-record', self.result(start))['complete_cold_written'])

    def test_clock_span_cannot_be_shorter_than_claimed_task_times(self):
        start = self.start()
        self.now += timedelta(seconds=20)
        self.assertFalse(self.put('assessment-record', self.result(start))['complete_cold_written'])

    def test_out_of_order_and_return_to_closed_blocks_not_complete(self):
        for k in ('single_sitting', 'order_verified', 'closed_blocks_respected'):
            self.setUp()
            execution = {x: True for x in ('single_sitting', 'order_verified', 'closed_blocks_respected')}
            execution[k] = False
            self.assertFalse(self.written(execution=execution)['complete_cold_written'])

    def test_historical_practice_does_not_claim_coach_cold(self):
        r = self.put('assessment-import', {'kind': 'practice_import', 'title': 'Imported fixture', 'occurred_at': self.now.isoformat(),
            'material': self.material(), 'sections': self.raw(), 'conditions': self.conditions(), 'limitations': [], 'next_step': 'Check anew.'})
        self.assertFalse(any(r['cold_blocks'].values()))

    def test_raw_to_710_field_is_rejected(self):
        with self.assertRaises(ValueError):
            self.written(predicted_score=680)

    def test_invalid_counts_ranges_and_nonfinite_rejected(self):
        for key, value in [('news', True), ('news', 8), ('news', 6.5), ('writing', [14, 10]), ('translation', [0, float('nan')]), ('writing', [0, 16])]:
            with self.subTest(key=key, value=value):
                self.setUp()
                raw = self.raw(); raw[key]['value'] = value
                with self.assertRaises(ValueError):
                    self.written(sections=raw)

    def test_correction_keeps_occasion_and_is_not_progress(self):
        r = self.written()
        old = copy.deepcopy(assess.active(self.state)[r['record_id']])
        raw = self.raw(); raw['writing']['value'] = [10, 12]
        corrected = self.put('assessment-correct', {'record_id': r['record_id'], 'replacement': raw, 'reason': 'Fixture misgrading.'})
        self.assertEqual(corrected['occurred_at'], old['occurred_at'])
        self.assertEqual(len(assess.active(self.state)), 1)
        self.assertIn(r['record_id'], self.state['assessments'])
        with self.assertRaises(ValueError):
            assess.compare(old, assess.active(self.state)[corrected['record_id']])

    def test_failed_mutation_is_atomic(self):
        before = copy.deepcopy(self.state)
        with self.assertRaises(ValueError):
            self.official(total=900)
        self.assertEqual(before, self.state)

    def test_material_catalog_does_not_claim_unaccepted_papers_ready(self):
        c = assess.catalogue()
        self.assertEqual(c['downloaded_recent_question_papers'], 4)
        self.assertEqual(c['complete_cold_written_accepted_count'], sum(m['cold_written_ready'] for m in c['materials']))


class ReportTests(Fixture):
    def test_reported_gap_and_oral_separate(self):
        r = self.official()
        self.assertEqual(r['observed_gap'], 20)
        self.assertFalse(r['oral_in_written_total'])
        self.assertIsNone(r['forecast_710'])

    def test_reported_maximum_matches_official_249_249_212(self):
        self.assertEqual(self.official(total=710, listening=249, reading=249, writing_translation=212)['reported']['total'], 710)

    def test_impossible_report_totals_rejected(self):
        for changes in [{'total': 580}, {'listening': 250}, {'reading': True}, {'total': 650, 'listening': 1, 'reading': None, 'writing_translation': None}]:
            self.setUp()
            with self.assertRaises(ValueError):
                self.official(**changes)

    def test_unknown_not_zero_and_self_report_not_verified(self):
        r = self.official(total=None, reading=None, verification='self_report')
        self.assertIsNone(r['observed_gap'])
        self.assertEqual(r['interpretation'], 'unverified_self_report_not_baseline')

    def test_hypothetical_arithmetic_not_forecast(self):
        r = self.official()
        out = assess.scenario(assess.active(self.state)[r['record_id']], {'assumed_reported_components': {'reading': 240}}, 580)
        self.assertEqual(out['hypothetical_reported_total'], 580)
        self.assertFalse(out['prediction'])

    def test_scenario_rejects_unknown_other_component(self):
        r = self.official(total=None, listening=None)
        with self.assertRaises(ValueError):
            assess.scenario(assess.active(self.state)[r['record_id']], {'assumed_reported_components': {'reading': 249}}, 580)

    def test_scenario_rejects_raw_practice_and_unverified_reports(self):
        r = self.written()
        with self.assertRaises(ValueError):
            assess.scenario(assess.active(self.state)[r['record_id']], {'assumed_reported_components': {'reading': 249}}, 580)
        r = self.official(verification='self_report')
        with self.assertRaises(ValueError):
            assess.scenario(assess.active(self.state)[r['record_id']], {'assumed_reported_components': {'reading': 249}}, 580)

    def test_mixed_units_cannot_be_compared(self):
        a = self.written()
        self.now += timedelta(days=1)
        b = self.official()
        with self.assertRaises(ValueError):
            assess.compare(assess.active(self.state)[a['record_id']], assess.active(self.state)[b['record_id']])

    def test_report_comparison_is_observed_not_causal(self):
        a = self.official()
        self.now += timedelta(days=1)
        b = self.official(total=570, listening=180)
        r = assess.compare(assess.active(self.state)[a['record_id']], assess.active(self.state)[b['record_id']])
        self.assertEqual(r['deltas']['total'], 10)
        self.assertFalse(r['causal_improvement_claim'])

    def test_future_report_cannot_be_imported(self):
        with self.assertRaises(ValueError):
            self.put('assessment-import', {'kind': 'official_report', 'title': 'Future', 'occurred_at': (self.now+timedelta(days=1)).isoformat(),
                'report': {'total': 580, 'listening': None, 'reading': None, 'writing_translation': None, 'oral_grade': None, 'verification': 'self_report', 'locator': 'fixture'},
                'limitations': [], 'next_step': 'Wait.'})


class OralTests(Fixture):
    def test_text_does_not_prove_oral_constructs(self):
        p = self.oral(modality='text_only', audio_observed=False)
        with self.assertRaises(ValueError):
            self.put('oral-record', p)
        for v in p['observations'].values():
            v['status'] = 'INDETERMINATE'
        p.pop('event_id', None)
        r = self.put('oral-record', p)
        self.assertEqual(r['alignment'], 'indeterminate')

    def test_full_strict_uses_actual_conditions(self):
        r = self.put('oral-record', self.oral())
        self.assertTrue(r['strict_full'])
        self.assertEqual(r['alignment'], 'demonstrated_once')
        self.assertFalse(self.oral_summary()['excellent_evidence_gate'])

    def test_approximate_timer_or_missing_dry_run_degrades(self):
        for changes in [{'timekeeper': 'approximate'}, {'dry_run_user': False}, {'dry_run_partner': False}, {'stage_reveal': False}, {'support': 'light'}, {'technical_failure': True}]:
            self.setUp()
            r = self.put('oral-record', self.oral(**changes))
            self.assertFalse(r['strict_full'])
            self.assertNotEqual(r['alignment'], 'demonstrated_once')

    def test_excess_time_or_incomplete_tasks_not_full(self):
        p = self.oral(); p['task_times']['read'] = 70
        self.assertFalse(self.put('oral-record', p)['strict_full'])
        self.setUp(); p = self.oral(); p['task_times'].pop('read')
        self.assertFalse(self.put('oral-record', p)['strict_full'])

    def test_same_pack_cannot_raise_stability_even_after_void(self):
        p = self.oral(pack_id='same')
        r = self.put('oral-record', p)
        self.put('assessment-void', {'record_id': r['record_id'], 'reason': 'Fixture.'})
        self.now += timedelta(days=1)
        self.assertFalse(self.put('oral-record', self.oral(pack_id='same'))['strict_full'])

    def test_same_date_topic_or_profile_not_stable(self):
        for changed in ({'topic': 'Travel', 'partner': 'ai:terse'}, {'partner': 'ai:terse'}, {'topic': 'Travel'}):
            self.setUp(); self.put('oral-record', self.oral())
            if len(changed) == 1:
                self.now += timedelta(days=1)
            self.put('oral-record', self.oral(**changed))
            self.assertFalse(self.oral_summary()['excellent_evidence_gate'])
            self.assertNotEqual(self.oral_summary()['stability'], 'repeated_ai_consistent')

    def stable_ai(self):
        self.put('oral-record', self.oral())
        self.now += timedelta(days=1)
        self.put('oral-record', self.oral(topic='Travel', partner='ai:terse'))

    def test_two_different_ai_mocks_need_human_check_for_gate(self):
        self.stable_ai()
        self.assertEqual(self.oral_summary()['stability'], 'repeated_ai_consistent')
        self.assertFalse(self.oral_summary()['excellent_evidence_gate'])
        self.now += timedelta(days=1)
        p = self.oral(partner='human', speaker_attribution='reliable', topic='Volunteering')
        p['task_times'] = {'interaction_prep': 60, 'interaction': 180}
        self.put('oral-record', p)
        self.assertTrue(self.oral_summary()['excellent_evidence_gate'])
        self.assertIsNone(self.oral_summary()['official_grade'])

    def test_unattributed_human_audio_is_not_individual_gate(self):
        self.stable_ai()
        self.put('oral-record', self.oral(partner='human', speaker_attribution='unknown', topic='Volunteering'))
        self.assertFalse(self.oral_summary()['excellent_evidence_gate'])

    def test_strict_counterevidence_resets_stability(self):
        self.stable_ai()
        self.now += timedelta(days=1)
        p = self.oral(topic='Technology', partner='ai:dissenting')
        p['observations']['response']['status'] = 'NOT_OBSERVED'
        self.put('oral-record', p)
        self.assertEqual(self.oral_summary()['stability'], 'insufficient')
        self.assertTrue(self.oral_summary()['counterevidence_reset'])

    def test_old_evidence_needs_refresh_without_deletion(self):
        self.stable_ai(); self.now += timedelta(days=31)
        r = self.oral_summary()
        self.assertEqual(r['stability'], 'insufficient')
        self.assertEqual(len(r['records']), 2)

    def test_unknown_dimension_or_predicted_grade_rejected(self):
        p = self.oral(); p['predicted_grade'] = '优秀'
        with self.assertRaises(ValueError):
            self.put('oral-record', p)


if __name__ == '__main__':
    unittest.main()
