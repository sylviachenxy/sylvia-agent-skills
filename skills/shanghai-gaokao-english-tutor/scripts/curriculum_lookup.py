#!/usr/bin/env python3
"""Query the complete, bundled textbook; optional explicit override. No network or writes."""
import argparse
import hashlib
import json
import re
from pathlib import Path

SOURCE_ID = 'shanghai-basic-requirements-scan-20260920'
BUNDLED_CORPUS = Path(__file__).resolve().parents[1] / 'references' / 'textbook'
UNITS = {'contents': '00-contents.md', 'pronunciation': '01-pronunciation.md', 'vocabulary': '02-vocabulary.md',
         'morphology': '03-morphology.md', 'syntax': '04-syntax.md',
         'discourse': '05-discourse.md', 'functions': '06-functions.md',
         'themes': '07-themes.md', 'requirements': '03-07-content-requirements.md',
         'word-formation': 'appendix-4-word-formation.md',
         'academic-quality': 'appendix-2-academic-quality.md',
         'planning': 'appendix-1-unit-planning.md', 'vocabulary-rules': 'appendix-3-vocabulary-rules.md',
         'issues': 'source-issues.md', 'figures': 'figures.md'}
BUNDLE_RESOURCES = {'index.md', 'source-manifest.json', 'review-summary.json', 'page-map.tsv',
                    'source-snippets/p013-stress-answer.jpg', 'vocabulary-reviewed/vocabulary.tsv',
                    'figure-manifest.json'} | {
    f'vocabulary-reviewed/vocabulary-{start}-{min(start+19,302)}.md' for start in range(178,303,20)} | {
    f'figures/p{page:03}.jpg' for page in [59, 66, 67, 69, 98, 115, 116, 117, 140, 141, 142, 163, 164, 165, 168]}


def checked_corpus(root=None):
    root = Path(root if root is not None else BUNDLED_CORPUS).expanduser().resolve(strict=True)
    manifest = json.loads((root / 'corpus-manifest.json').read_text())
    if not isinstance(manifest, dict) or manifest.get('schema_version') not in (1, 2) or manifest.get('source_id') != SOURCE_ID:
        raise ValueError('Unsupported corpus identity/schema; do not silently use another edition')
    if manifest.get('full_text_visual_review') is not True:
        raise ValueError('Corpus has not passed full visual proofreading')
    required = set(UNITS.values()) | {'vocabulary-reviewed/vocabulary.json'}
    if manifest['schema_version'] == 1:
        required.discard('figures.md')  # compatibility with the earlier explicit local corpus
    if manifest['schema_version'] == 2:
        required |= BUNDLE_RESOURCES
        if manifest.get('distribution') != 'bundled-teaching-corpus' or manifest.get('unique_printed_pages') != 307 or manifest.get('vocabulary_row_count') != 3319:
            raise ValueError('Incomplete bundled textbook metadata')
    resources = manifest.get('resources', {})
    if not isinstance(resources, dict) or not required <= resources.keys():
        raise ValueError('Missing required reviewed resources')
    for name, expected in resources.items():
        if not isinstance(name, str) or not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected):
            raise ValueError('Invalid resource name or SHA-256')
        path = root / name
        if Path(name).is_absolute() or not path.resolve().is_relative_to(root):
            raise ValueError('Corpus resource escapes its root')
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError(f'Resource changed since review: {name}; reconcile before use')
    return root, manifest


def lookup(root=None, word=None, unit=None):
    selection = 'bundled' if root is None else 'explicit_override'
    root, manifest = checked_corpus(root)
    result = {'source_id': SOURCE_ID, 'corpus_selection': selection, 'reviewed_on': manifest['reviewed_on'],
              'edition_evidence': manifest['edition_evidence'],
              'limitations': ['课程来源不是当年考试大纲；原书疑点不直接用作评分键。',
                              '初中星号与高中星号不等义；高学段涵盖低学段。'],
              'source_issues_path': str(root / 'source-issues.md')}
    if unit:
        if UNITS[unit] not in manifest['resources']:
            raise ValueError('This older explicit corpus does not include the requested resource')
        result['reference_path'] = str(root / UNITS[unit])
    if word is not None:
        word = word.strip()
        if not word:
            raise ValueError('Empty word query')
        data = json.loads((root / 'vocabulary-reviewed/vocabulary.json').read_text())
        rows = data['rows']
        if len(rows) != data['row_count'] or len(rows) != manifest['vocabulary_row_count']:
            raise ValueError('Vocabulary row count mismatch')
        exact = [r for r in rows if r['headword'] == word]
        folded = [r for r in rows if r['headword'].casefold() == word.casefold()]
        aliases = [r for r in rows if re.search(r'(?<![A-Za-z])' + re.escape(word) + r'(?![A-Za-z])', r['headword'], re.I)]
        matches = exact or folded or aliases
        if any(r.get('status') != 'visually_reviewed' for r in matches):
            raise ValueError('Unreviewed word record')
        result.update(query=word, matches=matches, ambiguous=len(matches) > 1,
                      matching='exact' if exact else 'casefold' if folded else 'headword_alias',
                      not_found_means='未在本版词头／变体中匹配；不等于超纲或禁止出现。')
    result['verified_resources'] = len(manifest['resources'])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', help='Optional explicit sealed corpus; omitted uses the installed skill textbook, never cwd or a registry')
    query = parser.add_mutually_exclusive_group()
    query.add_argument('--word')
    query.add_argument('--unit', choices=UNITS)
    args = parser.parse_args()
    try:
        print(json.dumps(lookup(args.corpus, args.word, args.unit), ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f'Corpus unavailable: {exc}\n')


if __name__ == '__main__':
    main()
