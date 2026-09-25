import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]


class PortableTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cet4-portable-')
        self.base = Path(self.temp.name).resolve()
        self.copy = self.base / 'installed'
        shutil.copytree(SKILL, self.copy, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        self.vault = self.base / 'vault'; self.vault.mkdir()
        self.registry = self.base / 'local' / 'profiles.json'

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, *args, profile='synthetic', ok=True):
        command = [sys.executable, '-I', str(self.copy / 'scripts/cet4_store.py'), '--registry', str(self.registry)]
        if profile:
            command += ['--profile', profile]
        p = subprocess.run(command + list(args), cwd=self.base, capture_output=True, text=True, timeout=10)
        if ok:
            self.assertEqual(p.returncode, 0, p.stderr)
            return json.loads(p.stdout)
        self.assertNotEqual(p.returncode, 0)
        return p

    def test_copied_bundle_setup_configure_and_fresh_process_resume(self):
        out = self.run_cli('init', '--vault', str(self.vault))
        self.assertTrue(out['saved'] and out['views_verified'])
        payload = self.base / 'config.json'
        payload.write_text(json.dumps({'event_id': 'CFG', 'expected_revision': 1, 'patch': {'target_score': 580, 'oral_target': '优秀'}}))
        self.run_cli('configure', '--input', str(payload))
        context = self.run_cli('context')
        self.assertEqual(context['settings']['target_score'], 580)
        self.assertEqual(context['settings']['oral_target'], '优秀')
        self.assertEqual(context['revision'], 2)
        self.assertEqual(context['oral_evidence']['stability'], 'insufficient')
        self.assertTrue(self.run_cli('configure', '--input', str(payload))['replayed'])
        self.assertEqual(self.run_cli('show')['revision'], 2)

    def test_multiple_profiles_require_selection(self):
        self.run_cli('init', '--vault', str(self.vault))
        self.run_cli('init', '--vault', str(self.vault), profile='another')
        self.run_cli('show', profile=None, ok=False)

    def test_synced_vault_can_rebind_but_empty_target_cannot(self):
        self.run_cli('init', '--vault', str(self.vault))
        moved = self.base / 'moved-vault'
        shutil.move(str(self.vault), moved)
        self.run_cli('show', ok=False)
        self.assertTrue(self.run_cli('init', '--vault', str(moved), '--rebind')['saved'])
        self.assertEqual(self.run_cli('show')['profile_id'], 'synthetic')
        self.vault.mkdir()
        self.run_cli('init', '--vault', str(self.vault), '--rebind', ok=False)

    def test_readonly_material_catalog_is_self_contained(self):
        c = self.run_cli('materials', profile=None)
        self.assertEqual(len(c['materials']), 5)
        self.assertFalse(self.registry.exists())

    def test_oral_show_and_withdrawal_work_through_cli(self):
        self.run_cli('init', '--vault', str(self.vault))
        sys.path.insert(0, str(SKILL / 'scripts'))
        from assessment import ORAL_DIMS
        p = {'event_id': 'ORAL-1', 'occurred_at': datetime.now(timezone.utc).isoformat(), 'pack_id': 'P1',
             'topic': 'Synthetic text only', 'prior_exposure': False, 'support': 'none', 'modality': 'text_only',
             'audio_observed': False, 'partner': 'ai:balanced', 'speaker_attribution': 'not_applicable',
             'timekeeper': 'unknown', 'dry_run_user': False, 'dry_run_partner': False, 'stage_reveal': False,
             'technical_failure': False, 'task_times': {},
             'observations': {k: {'status': 'INDETERMINATE', 'evidence': 'No audio in synthetic test.'} for k in ORAL_DIMS},
             'receipt_locator': 'synthetic conversation fixture', 'next_step': 'Collect actual audio only with consent.'}
        payload = self.base / 'oral.json'; payload.write_text(json.dumps(p))
        self.assertEqual(self.run_cli('oral-record', '--input', str(payload))['alignment'], 'indeterminate')
        self.assertTrue(self.run_cli('assessment-show', '--record', 'ORAL-1')['active'])
        payload.write_text(json.dumps({'event_id': 'VOID-1', 'record_id': 'ORAL-1', 'reason': 'Synthetic withdrawal.'}))
        self.run_cli('assessment-void', '--input', str(payload))
        self.assertFalse(self.run_cli('assessment-show', '--record', 'ORAL-1')['active'])

    def test_practice_view_is_readable_and_distinguishes_reported_scores(self):
        self.run_cli('init', '--vault', str(self.vault))
        p = {'event_id': 'REPORT-1', 'kind': 'official_report', 'title': 'Synthetic report',
             'occurred_at': datetime.now(timezone.utc).isoformat(), 'limitations': ['Synthetic fixture only.'], 'next_step': 'New diagnostic.',
             'report': {'total': 560, 'listening': 170, 'reading': 220, 'writing_translation': 170, 'oral_grade': None,
                        'verification': 'self_report', 'locator': 'Synthetic self-report.'}}
        payload = self.base / 'report.json'; payload.write_text(json.dumps(p))
        r = self.run_cli('assessment-import', '--input', str(payload))
        view = Path(r['practice_path']).read_text()
        self.assertIn('| 总分 | 560 | 710 |', view)
        self.assertIn('用户自报，尚未核对原报告', view)
        self.assertNotIn('forecast_710', view)

    def test_existing_goal_required_no_goal_created(self):
        self.run_cli('init', '--vault', str(self.vault))
        payload = self.base / 'goal.json'
        payload.write_text(json.dumps({'event_id': 'BIND', 'expected_revision': 1, 'patch': {'goal_id': 'G-2026-001'}}))
        self.run_cli('configure', '--input', str(payload), ok=False)
        self.assertIsNone(self.run_cli('show')['settings']['goal_id'])
        self.assertFalse((self.vault / 'Goals').exists())

    def test_markdown_resource_links_exist_within_bundle(self):
        for path in SKILL.rglob('*.md'):
            for destination in re.findall(r'\]\(([^)]+)\)', path.read_text()):
                if '://' in destination or destination.startswith('#'):
                    continue
                resolved = (path.parent / destination.split('#')[0]).resolve()
                self.assertTrue(resolved.is_relative_to(SKILL.resolve()), (path, destination))
                self.assertTrue(resolved.exists(), (path, destination))

    def test_no_private_research_dependency_or_extra_entrypoint(self):
        self.assertEqual(len(list(SKILL.rglob('SKILL.md'))), 1)
        for path in SKILL.rglob('*'):
            if path.is_file() and path.suffix in {'.py', '.json', '.md', '.yaml'}:
                text = path.read_text()
                if path.name == 'test_portable.py':
                    continue
                self.assertNotIn('/Users/', text)
                self.assertNotIn('Downloads/sylvia', text)
        self.assertLess(len((SKILL / 'SKILL.md').read_text().splitlines()), 500)


if __name__ == '__main__':
    unittest.main()
