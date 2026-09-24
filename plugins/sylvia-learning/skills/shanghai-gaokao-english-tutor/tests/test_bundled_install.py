"""Run from a copied skill with no repository, research tree or real learner registry."""
import csv
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / 'scripts'))
import curriculum_lookup as corpus


class PortableInstallTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='sh-english-portable-')
        cls.root = Path(cls.temporary.name).resolve()
        cls.installed = cls.root / '另一个安装位置' / SKILL.name
        shutil.copytree(SKILL, cls.installed, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        cls.textbook = cls.installed / 'references/textbook'
        cls.registry = cls.root / 'private-registry.json'
        cls.cwd = cls.root / 'unrelated-directory'
        cls.cwd.mkdir()

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def cli(self, *args, script='curriculum_lookup.py', installed=None):
        return subprocess.run([sys.executable, '-I', str((installed or self.installed) / 'scripts' / script), *args],
                              cwd=self.cwd, capture_output=True, text=True)

    def lookup(self, *args):
        proc = self.cli(*args)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout)

    def test_no_repository_or_research_is_available(self):
        self.assertFalse((self.root / 'research').exists())
        self.assertFalse((self.root / '.git').exists())
        self.assertFalse((self.installed / 'research').exists())
        self.assertEqual(self.lookup()['corpus_selection'], 'bundled')

    def test_default_word_lookup_resolves_copied_skill(self):
        result = self.lookup('--word', 'charge')
        self.assertEqual(result['matches'][0]['printed_page'], 198)
        self.assertIn('*v.', result['matches'][0]['senior'])
        self.assertTrue(Path(result['source_issues_path']).is_relative_to(self.installed))

    def test_every_unit_is_resolvable_without_configuration(self):
        for unit in corpus.UNITS:
            with self.subTest(unit=unit):
                result = self.lookup('--unit', unit)
                path = Path(result['reference_path'])
                self.assertTrue(path.is_file())
                self.assertTrue(path.is_relative_to(self.installed))

    def test_complete_vocabulary_and_source_quirks_present(self):
        data = json.loads((self.textbook / 'vocabulary-reviewed/vocabulary.json').read_text())
        self.assertEqual(len(data['rows']), 3319)
        self.assertEqual({r['printed_page'] for r in data['rows']}, set(range(178, 303)))
        self.assertEqual(data['rows'][0]['headword'], 'a, an')
        self.assertEqual(data['rows'][-1]['headword'], 'zoo')
        self.assertEqual(self.lookup('--word', 'word')['matches'][0]['printed_page'], 300)
        clear = self.lookup('--word', 'clear')['matches'][0]
        self.assertIn('清楚', clear['senior'])
        self.assertTrue(clear['source_notes'])

    def test_page_mapping_covers_complete_scan(self):
        with (self.textbook / 'page-map.tsv').open() as handle:
            rows = list(csv.DictReader(handle, delimiter='\t'))
        canonical = [r for r in rows if r['use'] == 'canonical']
        self.assertEqual(len(rows), 308)
        self.assertEqual(len(canonical), 307)
        self.assertEqual({r['printed_page'] for r in canonical}, {'iii', 'iv'} | {str(n) for n in range(1, 306)})

    def test_all_published_resources_sealed_including_stress_image(self):
        manifest = json.loads((self.textbook / 'corpus-manifest.json').read_text())
        paths = {p.relative_to(self.textbook).as_posix() for p in self.textbook.rglob('*') if p.is_file()}
        self.assertEqual(set(manifest['resources']), paths - {'corpus-manifest.json'})
        self.assertIn('source-snippets/p013-stress-answer.jpg', manifest['resources'])
        self.assertFalse(any(p.startswith('evidence/') for p in manifest['resources']))
        corpus.checked_corpus(self.textbook)

    def test_visual_tasks_have_original_images_and_teacher_boundary(self):
        figures = json.loads((self.textbook / 'figure-manifest.json').read_text())['images']
        self.assertEqual({r['printed_page'] for r in figures}, {59, 66, 67, 69, 98, 115, 116, 117, 140, 141, 142, 163, 164, 165, 168})
        with (self.textbook / 'page-map.tsv').open() as handle:
            mapping = {r['printed_page']: r for r in csv.DictReader(handle, delimiter='\t') if r['use'] == 'canonical'}
        for figure in figures:
            content = (self.textbook / figure['file']).read_bytes()
            self.assertEqual(hashlib.sha256(content).hexdigest(), mapping[str(figure['printed_page'])]['image_sha256'])
            self.assertEqual(figure['audience'], 'teacher_reference')
            self.assertTrue(figure['may_contain_answers'])
            self.assertTrue(content.startswith(b'\xff\xd8'))

    def test_national_baselines_are_complete_and_separate(self):
        national = self.installed / 'references/national-curriculum'
        manifest = json.loads((national / 'manifest.json').read_text())
        for name, expected in manifest['resources'].items():
            self.assertEqual(hashlib.sha256((national / name).read_bytes()).hexdigest(), expected)
        with (national / 'moe-2020-vocabulary-3000.tsv').open() as handle:
            self.assertEqual(len(list(csv.DictReader(handle, delimiter='\t'))), 3000)
        with (national / 'moe-2020-grammar-scope.tsv').open() as handle:
            rows = list(csv.DictReader(handle, delimiter='\t'))
        self.assertEqual(len(rows), 103)
        self.assertEqual(len({r['id'] for r in rows}), 103)

    def test_teaching_references_have_no_developer_paths_or_logs(self):
        forbidden = ('/Users/', '/private/tmp/', '/tmp/', 'ocr-drafts/', 'vocabulary-reviewed/pages/', 'evidence/proof-')
        for path in (self.installed / 'references').rglob('*'):
            if path.is_file() and path.suffix in {'.md', '.json', '.tsv', '.txt'}:
                for token in forbidden:
                    self.assertNotIn(token, path.read_text(), f'{path}: {token}')

    def test_markdown_links_stay_in_copied_skill_and_resolve(self):
        for path in self.installed.rglob('*.md'):
            body = re.sub(r'```.*?```', '', path.read_text(), flags=re.S)
            for target in re.findall(r'\]\(([^)]+)\)', body):
                if target.startswith(('http:', 'https:', 'mailto:', '#')):
                    continue
                target = target.strip('<>').split('#', 1)[0]
                linked = (path.parent / target).resolve()
                self.assertTrue(linked.is_relative_to(self.installed), f'{path}: {target}')
                self.assertTrue(linked.exists(), f'{path}: {target}')

    def test_explicit_missing_override_does_not_silently_fallback(self):
        proc = self.cli('--corpus', str(self.root / 'missing-override'), '--word', 'charge')
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('Corpus unavailable', proc.stderr)

    def test_missing_bundled_chapter_is_reported(self):
        with tempfile.TemporaryDirectory(prefix='sh-english-broken-') as scratch:
            target = Path(scratch) / SKILL.name
            shutil.copytree(self.installed, target)
            (target / 'references/textbook/04-syntax.md').unlink()
            proc = self.cli('--word', 'charge', installed=target)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn('Corpus unavailable', proc.stderr)

    def test_tampered_bundled_chapter_is_reported(self):
        with tempfile.TemporaryDirectory(prefix='sh-english-changed-') as scratch:
            target = Path(scratch) / SKILL.name
            shutil.copytree(self.installed, target)
            (target / 'references/textbook/04-syntax.md').write_text('Synthetic corruption')
            proc = self.cli('--unit', 'syntax', installed=target)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn('Resource changed since review', proc.stderr)

    def test_default_corpus_does_not_create_personal_configuration(self):
        before = self.registry.exists()
        self.lookup('--word', 'clear')
        self.assertEqual(self.registry.exists(), before)
        self.assertFalse((self.root / 'Learning').exists())

    def test_synthetic_profile_needs_no_absolute_corpus_binding(self):
        with tempfile.TemporaryDirectory(prefix='sh-english-profile-') as scratch:
            vault = Path(scratch).resolve()
            registry = vault / 'registry.json'
            args = ['--registry', str(registry), '--profile', 'synthetic']
            proc = self.cli(*args, 'init', '--vault', str(vault), script='tutor_store.py')
            self.assertEqual(proc.returncode, 0, proc.stderr)
            proc = self.cli(*args, 'context', script='tutor_store.py')
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIsNone(json.loads(proc.stdout)['settings']['corpus_path'])
            # The independent, no-registry lookup still resolves this installed package.
            self.assertEqual(self.lookup('--word', 'charge')['corpus_selection'], 'bundled')

    def test_assessment_catalogue_portable_and_metadata_only(self):
        proc = self.cli('materials', script='tutor_store.py')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        self.assertEqual(len(data['materials']), 22)
        self.assertEqual(len({r['id'] for r in data['materials']}), 22)
        self.assertEqual(len({r['district'] for r in data['materials']}), 15)
        self.assertEqual(sum(r['key_comparison_count'] for r in data['materials']), 850)
        self.assertEqual(sum(len(r['preexposed_key_items']) for r in data['materials']), 5)
        for row in data['materials']:
            self.assertRegex(row['question_sha256'], r'^[a-f0-9]{64}$')
            self.assertTrue(row['source_url'].startswith('https://'))
            self.assertEqual(len(row['section_gates']), 10)
            self.assertTrue(row['issues'])
            self.assertNotIn('answers_1_10', row)
            self.assertNotIn('reference_key_11_50', row)
            self.assertNotIn('local_file', row)
            self.assertNotIn('student', row)
        self.assertFalse(self.registry.exists())

    def test_assessment_import_correction_and_old_version_in_fresh_processes(self):
        with tempfile.TemporaryDirectory(prefix='sh-assessment-cli-') as scratch:
            vault = Path(scratch).resolve()
            registry = vault / 'registry.json'
            args = ['--registry', str(registry), '--profile', 'synthetic']
            proc = self.cli(*args, 'init', '--vault', str(vault), script='tutor_store.py')
            self.assertEqual(proc.returncode, 0, proc.stderr)
            source = vault / 'private-input.json'
            score = {'range': [104, 104], 'basis': 'user_report', 'source': 'Synthetic test',
                     'rubric': 'Synthetic denominator115', 'items': []}
            payload = {'event_id': 'IMPORT-SYNTHETIC', 'material': {'id': 'SYNTHETIC-HISTORY',
                'title': 'Synthetic history, not a learner', 'kind': 'unknown', 'locator': 'Synthetic test',
                'sha256': None, 'year': None, 'session': 'unknown', 'normalization': 'Synthetic 115 reported grade'},
                'protocol': {'baseline_status': 'provisional', 'baseline_source': 'Synthetic only',
                             'checked_on': '2020-01-01', 'scoring_plan': 'No original paper: report only'},
                'occurred_at': '2020-01-01T12:00:00+08:00',
                'conditions': {'written': {'support': 'unknown', 'timing': 'unknown',
                    'elapsed_seconds': None, 'audio': 'none', 'evidence': 'Synthetic unknown conditions'}},
                'scores': {'written': score}, 'limitations': ['Synthetic test, not student data'],
                'next_step': 'Collect actual evidence', 'provenance': 'Synthetic fixture'}
            source.write_text(json.dumps(payload))
            proc = self.cli(*args, 'assessment-import', '--input', str(source), script='tutor_store.py')
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(json.loads(proc.stdout)['views_verified'])
            proc = self.cli(*args, 'context', script='tutor_store.py')
            self.assertEqual(json.loads(proc.stdout)['assessments'][0]['scores']['total']['possible_bounds'], [104, 139])
            score['range'] = [108, 108]
            source.write_text(json.dumps({'event_id': 'CORRECT-SYNTHETIC', 'record_id': 'IMPORT-SYNTHETIC',
                'reason': 'Synthetic transcription correction', 'scores': {'written': score},
                'limitations': ['Still a synthetic fixture'], 'next_step': 'Collect actual evidence'}))
            proc = self.cli(*args, 'assessment-correct', '--input', str(source), script='tutor_store.py')
            self.assertEqual(proc.returncode, 0, proc.stderr)
            proc = self.cli(*args, 'assessment-show', '--record', 'IMPORT-SYNTHETIC', script='tutor_store.py')
            self.assertEqual(proc.returncode, 0, proc.stderr)
            old = json.loads(proc.stdout)
            self.assertFalse(old['active'])
            self.assertEqual(old['superseded_by'], ['CORRECT-SYNTHETIC'])
            proc = self.cli(*args, 'assessment-report', '--record', 'CORRECT-SYNTHETIC', script='tutor_store.py')
            new = json.loads(proc.stdout)
            self.assertEqual(new['scores']['total']['possible_bounds'], [108, 143])
            self.assertEqual(new['supersedes'], 'IMPORT-SYNTHETIC')
            proc = self.cli(*args, 'context', script='tutor_store.py')
            data = json.loads(proc.stdout)
            self.assertEqual(len(data['assessments']), 1)
            self.assertEqual(data['active_attempt_count'], 0)
            view = vault / 'Learning/Shanghai-Gaokao-English/synthetic/Practice.md'
            self.assertIn('不是新的学习进步', view.read_text())


if __name__ == '__main__':
    unittest.main()
