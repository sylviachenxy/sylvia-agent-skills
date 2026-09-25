import copy
import json
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import cet4_store as store


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.state = store.new_state('synthetic')
        self.now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        self.serial = 0

    def mutate(self, command, payload):
        self.serial += 1
        payload.setdefault('event_id', f'E-{self.serial}')
        self.state, result = store.apply(self.state, command, payload, self.now)
        return result

    def prepare(self, number=1, domain='grammar', stem=None, demand='produce'):
        item = {'item_id': f'ITEM-{number}', 'domain': domain, 'focus': 'tense:time-reference',
                'demand': demand, 'stem': stem or f'Synthetic question {number}', 'key': 'Synthetic key',
                'rubric': ['Synthetic correctness and explanation'], 'time_limit_seconds': 90,
                'source': {'kind': 'original', 'locator': 'synthetic-test', 'answer_status': 'tutor_checked'}}
        return self.mutate('prepare', {'item': item})['item_id']

    def present(self, item, seen=False):
        return self.mutate('present', {'item_id': item, 'prior_exposure': seen})['presentation_id']

    def payload(self, presentation, **patch):
        return {'presentation_id': presentation, 'session_id': 'SESSION-1',
                'occurred_at': self.now.isoformat(), 'support': 'none', 'result': 'pass',
                'modality': 'text', 'audio_observed': False, 'evidence_kind': 'observation',
                'evidence': 'Synthetic learner response.', 'reason': 'Synthetic rubric evidence.',
                'errors': [], 'next_step': 'Check another context.', 'new_context': False,
                'transfer_note': '', 'timer_verified': False, 'elapsed_seconds': None} | patch

    def answer(self, p, **patch):
        return self.mutate('record', self.payload(p, **patch))

    def status(self):
        return store.context(self.state, '2026-03-01')['skills'][0]

    def test_unseen_first_attempt_is_independent(self):
        p = self.present(self.prepare())
        self.assertTrue(self.answer(p)['qualifies_independent'])
        self.assertEqual(self.status()['status'], 'independent')

    def test_prepared_answer_not_in_student_presentation(self):
        item = self.prepare()
        result = self.mutate('present', {'item_id': item, 'prior_exposure': False})
        self.assertNotIn('key', result)
        self.assertNotIn('rubric', result)

    def test_seen_elsewhere_not_independent(self):
        self.answer(self.present(self.prepare(), True))
        self.assertEqual(self.status()['status'], 'guided')

    def test_hint_blocks_independence(self):
        self.assertFalse(self.answer(self.present(self.prepare()), support='cue')['qualifies_independent'])

    def test_same_presentation_retry_not_independent(self):
        p = self.present(self.prepare())
        self.answer(p, result='fail', errors=['time-reference'])
        self.assertFalse(self.answer(p)['qualifies_independent'])
        self.assertEqual(self.status()['open_errors'], ['time-reference'])

    def test_rename_and_whitespace_do_not_make_new_item(self):
        self.answer(self.present(self.prepare(stem='A   question')))
        second = self.prepare(2, stem='a question ')
        self.assertFalse(self.answer(self.present(second))['qualifies_independent'])

    def test_partial_result_is_not_mastery(self):
        result = self.answer(self.present(self.prepare()), result='partial', errors=['missing-reason'])
        self.assertTrue(result['qualifies_independent'])
        self.assertFalse(result['independent_pass'])
        self.assertEqual(result['result'], 'partial')
        self.assertEqual(self.status()['status'], 'needs_work')

    def test_independent_pass_is_explicit(self):
        result = self.answer(self.present(self.prepare()))
        self.assertTrue(result['independent_pass'])
        self.assertEqual(result['result'], 'pass')

    def test_non_object_mutation_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Expected JSON object'):
            store.apply(self.state, 'configure', [])

    def test_boolean_revision_rejected(self):
        with self.assertRaises(ValueError):
            self.mutate('configure', {'expected_revision': True, 'patch': {'daily_minutes': 20}})

    def test_unassessed_does_not_promote(self):
        self.answer(self.present(self.prepare()), result='unassessed')
        self.assertEqual(self.status()['status'], 'exposed')

    def test_real_transfer_requires_prior_evidence(self):
        self.answer(self.present(self.prepare()), new_context=True, transfer_note='Different context')
        self.assertEqual(self.status()['status'], 'independent')
        self.now += timedelta(days=1)
        self.answer(self.present(self.prepare(2)), new_context=True, transfer_note='Different narrative and time anchor')
        self.assertEqual(self.status()['status'], 'transferable')

    def test_delayed_transfer_and_subsequent_failure(self):
        self.answer(self.present(self.prepare()))
        self.now += timedelta(days=7)
        self.answer(self.present(self.prepare(2)), new_context=True, transfer_note='Different situation')
        self.assertEqual(self.status()['status'], 'durable')
        self.answer(self.present(self.prepare(3)))
        self.assertEqual(self.status()['status'], 'durable')
        self.answer(self.present(self.prepare(4)), result='fail', errors=['time-reference'])
        self.assertEqual(self.status()['status'], 'needs_work')

    def test_assisted_repetition_does_not_postpone_due(self):
        p = self.present(self.prepare())
        self.answer(p)
        due = self.status()['due_date']
        self.now += timedelta(days=10)
        self.answer(p, support='model')
        self.assertEqual(self.status()['due_date'], due)

    def test_transcript_cannot_prove_speaking(self):
        self.answer(self.present(self.prepare(domain='speaking')), modality='transcript')
        self.assertEqual(self.status()['status'], 'guided')

    def test_text_cannot_prove_listening(self):
        self.assertFalse(self.answer(self.present(self.prepare(domain='listening')), play_count=0, transcript_seen=True, audio_matched=False)['qualifies_independent'])

    def test_actual_audio_can_supply_independent_evidence(self):
        result = self.answer(self.present(self.prepare(domain='speaking')), modality='audio', audio_observed=True)
        self.assertTrue(result['qualifies_independent'])

    def test_audio_flag_cannot_be_attached_to_text(self):
        with self.assertRaises(ValueError):
            self.answer(self.present(self.prepare()), audio_observed=True)

    def test_timing_requires_evidence(self):
        p = self.present(self.prepare())
        with self.assertRaises(ValueError):
            self.answer(p, timer_verified=True)
        result = self.answer(p, timer_verified=True, elapsed_seconds=100)
        self.assertFalse(result['within_verified_limit'])

    def test_timed_evidence_separate_from_mastery(self):
        self.answer(self.present(self.prepare()), timer_verified=True, elapsed_seconds=70)
        self.assertEqual(len(self.status()['timed_evidence']), 1)
        self.assertEqual(self.status()['status'], 'independent')

    def test_privacy_setting_applies_to_open_presentations(self):
        p = self.present(self.prepare())
        self.mutate('configure', {'expected_revision': self.state['revision'], 'patch': {'store_quotes': False}})
        with self.assertRaises(ValueError):
            self.answer(p, evidence_kind='quote')
        self.answer(p, evidence_kind='observation')

    def test_config_cas_and_history(self):
        self.mutate('configure', {'expected_revision': 1, 'patch': {'daily_minutes': 20}})
        self.assertEqual(self.state['settings_history'][0]['settings']['daily_minutes'], 30)
        with self.assertRaises(ValueError):
            self.mutate('configure', {'expected_revision': 1, 'patch': {'daily_minutes': 10}})

    def test_idempotency_and_collision(self):
        payload = {'event_id': 'CONFIG-1', 'expected_revision': 1, 'patch': {'daily_minutes': 20}}
        self.mutate('configure', payload)
        revision = self.state['revision']
        self.assertTrue(self.mutate('configure', payload)['replayed'])
        self.assertEqual(self.state['revision'], revision)
        with self.assertRaises(ValueError):
            self.mutate('configure', payload | {'patch': {'daily_minutes': 15}})

    def test_future_and_backdated_answers_rejected(self):
        p = self.present(self.prepare())
        for delta in [-1, 400]:
            with self.assertRaises(ValueError):
                self.answer(p, occurred_at=(self.now + timedelta(seconds=delta)).isoformat())

    def test_timezone_required(self):
        with self.assertRaises(ValueError):
            self.answer(self.present(self.prepare()), occurred_at='2026-01-01T12:00:00')

    def test_derived_fields_cannot_be_supplied(self):
        with self.assertRaises(ValueError):
            self.answer(self.present(self.prepare()), qualifies_independent=True)

    def test_invalidate_recomputes_without_erasing_exposure(self):
        item = self.prepare()
        a = self.answer(self.present(item))['attempt_id']
        self.mutate('invalidate', {'attempt_id': a, 'reason': 'Synthetic wrong assessment'})
        self.assertEqual(store.context(self.state)['active_attempt_count'], 0)
        self.assertFalse(self.answer(self.present(item))['qualifies_independent'])

    def test_demand_dimensions_are_separate(self):
        self.answer(self.present(self.prepare()))
        self.answer(self.present(self.prepare(2, demand='understand')))
        self.assertEqual(len(store.context(self.state)['skills']), 2)

    def test_grouped_session_not_duplicate_achievement(self):
        self.answer(self.present(self.prepare()))
        self.answer(self.present(self.prepare(2)))
        self.assertEqual(store.context(self.state)['session_count'], 1)

    def test_failed_mutation_keeps_original_state(self):
        before = copy.deepcopy(self.state)
        with self.assertRaises(ValueError):
            self.mutate('configure', {'expected_revision': 1, 'patch': {'timezone': 'Invalid/Zone'}})
        self.assertEqual(before, self.state)


class FileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cet4-store-test-')
        self.root = Path(self.temp.name).resolve()
        self.state = store.new_state('synthetic')

    def tearDown(self):
        self.temp.cleanup()

    def test_state_and_view_readback(self):
        self.assertTrue(store.persist(self.root, self.state)['views_verified'])
        self.assertEqual(store.load_state(self.root), self.state)

    def test_user_notes_preserved(self):
        store.persist(self.root, self.state)
        p = self.root / 'Practice.md'
        p.write_text(p.read_text() + '\nMY PRIVATE NOTE\n')
        store.persist(self.root, self.state)
        self.assertIn('MY PRIVATE NOTE', p.read_text())

    def test_view_io_failure_preserves_state_and_can_rebuild(self):
        original_atomic = store.atomic

        def fail_view(path, body):
            if path.name == 'Practice.md':
                raise OSError('synthetic view write failure')
            return original_atomic(path, body)

        with patch.object(store, 'atomic', side_effect=fail_view):
            outcome = store.persist(self.root, self.state)
        self.assertTrue(outcome['saved'])
        self.assertFalse(outcome['views_verified'])
        self.assertEqual(store.load_state(self.root), self.state)
        self.assertTrue(store.persist(self.root, store.load_state(self.root))['views_verified'])

    def test_user_text_cannot_inject_managed_markers(self):
        case = EvidenceTests()
        case.setUp()
        case.answer(case.present(case.prepare()), evidence='Text <!-- CET4:END --> preserved as text')
        store.persist(self.root, case.state)
        self.assertTrue(store.persist(self.root, case.state)['views_verified'])
        body = (self.root / 'Practice.md').read_text()
        self.assertEqual(body.count(store.END), 1)
        self.assertIn('&lt;!-- CET4:END --&gt;', body)

    def test_managed_edit_blocks_without_replacing_state(self):
        store.persist(self.root, self.state)
        before = (self.root / 'state.json').read_bytes()
        p = self.root / 'Practice.md'
        p.write_text(p.read_text().replace('revision 1', 'revision 999'))
        with self.assertRaises(ValueError):
            store.persist(self.root, self.state | {'revision': 2})
        self.assertEqual((self.root / 'state.json').read_bytes(), before)

    def test_missing_marker_blocks(self):
        (self.root / 'Practice.md').write_text('User-owned document')
        with self.assertRaises(ValueError):
            store.persist(self.root, self.state)
        self.assertFalse((self.root / 'state.json').exists())

    def test_state_corruption_is_detected(self):
        store.persist(self.root, self.state)
        p = self.root / 'state.json'
        data = json.loads(p.read_text()); data['revision'] = 50
        p.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            store.load_state(self.root)

    def test_sync_conflict_is_detected(self):
        store.persist(self.root, self.state)
        (self.root / 'state (Conflicted copy).json').write_text('{}')
        with self.assertRaises(ValueError):
            store.load_state(self.root)

    def test_symlink_target_not_overwritten(self):
        p = self.root / 'outside.txt'; p.write_text('preserve')
        (self.root / 'state.json').symlink_to(p)
        with self.assertRaises(ValueError):
            store.persist(self.root, self.state)
        self.assertEqual(p.read_text(), 'preserve')

    def test_profiles_is_nonmutating_when_absent(self):
        reg = self.root / 'profiles.json'
        proc = subprocess.run([sys.executable, str(SCRIPTS / 'cet4_store.py'), '--registry', str(reg), 'profiles'], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout), {})
        self.assertFalse(reg.exists())

    def test_cannot_init_inside_git_checkout(self):
        (self.root / '.git').mkdir()
        proc = subprocess.run([sys.executable, str(SCRIPTS / 'cet4_store.py'), '--registry', str(self.root / 'profiles.json'), '--profile', 'fake', 'init', '--vault', str(self.root)], capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse((self.root / 'Learning').exists())

    def test_explicit_registry_directory_alias_is_supported(self):
        real = self.root / 'actual'; real.mkdir()
        alias = self.root / 'alias'; alias.symlink_to(real, target_is_directory=True)
        proc = subprocess.run([sys.executable, str(SCRIPTS / 'cet4_store.py'), '--registry', str(alias / 'profiles.json'), 'profiles'], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse((real / 'profiles.json').exists())

    def test_registry_file_symlink_still_rejected(self):
        real = self.root / 'actual.json'; real.write_text('{}')
        alias = self.root / 'alias.json'; alias.symlink_to(real)
        proc = subprocess.run([sys.executable, str(SCRIPTS / 'cet4_store.py'), '--registry', str(alias), 'profiles'], capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0)

    def test_starter_items_all_freeze(self):
        items = json.loads((SCRIPTS.parent / 'assets/starter-items.json').read_text())['items']
        for n, item in enumerate(items):
            self.state, _ = store.apply(self.state, 'prepare', {'event_id': f'SEED-{n}', 'item': item})
        self.assertEqual(len(self.state['items']), 6)



if __name__ == '__main__':
    unittest.main()
