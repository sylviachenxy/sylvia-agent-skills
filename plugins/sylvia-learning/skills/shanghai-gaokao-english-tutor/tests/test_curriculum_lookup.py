import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import curriculum_lookup as corpus


class CorpusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sh-en-corpus-test-')
        self.root = Path(self.temp.name).resolve()
        for name in corpus.UNITS.values():
            (self.root / name).write_text('Synthetic reviewed reference')
        rows = [{'id': 'p178-r01', 'headword': 'China', 'primary': 'country', 'junior': '', 'senior': '', 'status': 'visually_reviewed'},
                {'id': 'p178-r02', 'headword': 'china', 'primary': '', 'junior': 'porcelain', 'senior': '', 'status': 'visually_reviewed'}]
        p = self.root / 'vocabulary-reviewed/vocabulary.json'; p.parent.mkdir()
        p.write_text(json.dumps({'row_count': 2, 'rows': rows}))
        self.manifest = {'schema_version': 1, 'source_id': corpus.SOURCE_ID, 'full_text_visual_review': True,
                         'reviewed_on': '2026-09-20', 'edition_evidence': 'synthetic-test', 'vocabulary_row_count': 2,
                         'resources': {str(p.relative_to(self.root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.rglob('*') if p.is_file()}}
        self.save_manifest()

    def save_manifest(self):
        (self.root / 'corpus-manifest.json').write_text(json.dumps(self.manifest))

    def tearDown(self):
        self.temp.cleanup()

    def test_exact_case_preserved(self):
        self.assertEqual(corpus.lookup(self.root, 'China')['matches'][0]['primary'], 'country')
        self.assertEqual(corpus.lookup(self.root, 'china')['matches'][0]['junior'], 'porcelain')

    def test_ambiguous_casefold_not_silently_selected(self):
        self.assertTrue(corpus.lookup(self.root, 'CHINA')['ambiguous'])

    def test_missing_does_not_invent_entry(self):
        self.assertEqual(corpus.lookup(self.root, 'nonexistent')['matches'], [])

    def test_unreviewed_corpus_rejected(self):
        self.manifest['full_text_visual_review'] = False; self.save_manifest()
        with self.assertRaises(ValueError): corpus.lookup(self.root)

    def test_changed_resource_rejected(self):
        (self.root / '04-syntax.md').write_text('Changed after proof')
        with self.assertRaises(ValueError): corpus.lookup(self.root, 'China')

    def test_path_escape_rejected(self):
        self.manifest['resources']['../outside'] = '0' * 64; self.save_manifest()
        with self.assertRaises(ValueError): corpus.lookup(self.root)

    def test_required_resource_missing_rejected(self):
        del self.manifest['resources']['source-issues.md']; self.save_manifest()
        with self.assertRaises(ValueError): corpus.lookup(self.root)

    def test_unit_returns_verified_path(self):
        self.assertEqual(corpus.lookup(self.root, unit='syntax')['reference_path'], str(self.root / '04-syntax.md'))


if __name__ == '__main__':
    unittest.main()
