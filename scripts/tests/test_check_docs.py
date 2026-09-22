"""Regression tests for portable doc links and Git-based inventory boundaries."""
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location('check_docs', Path(__file__).parents[1] / 'check_docs.py')
check_docs = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = check_docs
SPEC.loader.exec_module(check_docs)


class MarkdownTests(unittest.TestCase):
    def test_code_examples_are_not_links(self):
        text = '''# Guide
`[example](missing.md)`
````md
[example](missing.md)
```
[still code](also-missing.md)
````
[Actual `API` guide](real.md)
'''
        links = check_docs.markdown_links(text)
        self.assertEqual([link.target for link in links], ['real.md'])
        self.assertEqual(text[:links[0].offset].count('\n') + 1, 8)

    def test_destination_forms_and_images(self):
        text = '''[A](path(with-parentheses).md "Title")
[B](<path with spaces.md#heading>)
![Image](image.png)
[ref]: elsewhere.md#part
<a href="README.md">Readme</a>
<img src="screen.png" />
'''
        self.assertEqual([link.target for link in check_docs.markdown_links(text)], [
            'path(with-parentheses).md', 'path with spaces.md#heading', 'image.png',
            'elsewhere.md#part', 'README.md', 'screen.png',
        ])

    def test_heading_fragments_are_unicode_and_duplicate_aware(self):
        text = '''# 启动 API（开发）
## API `Tool`
## API `Tool`
<a id="explicit-target"></a>
~~~md
# Ignore this example
~~~
'''
        self.assertEqual(check_docs.heading_anchors(text), {
            '启动-api开发', 'api-tool', 'api-tool-1', 'explicit-target',
        })


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def write(self, name, content=''):
        file = self.root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content)
        return Path(name)

    def test_git_inventory_excludes_private_ignored_docs(self):
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        self.write('.gitignore', 'private.md\ncache/\n')
        self.write('README.md', '# Public\n')
        self.write('private.md', '# PRIVATE MUST NOT BE INDEXED\n')
        self.write('cache/generated.md', '# Generated\n')
        files = check_docs.public_files(self.root)
        self.assertEqual(files, {Path('.gitignore'), Path('README.md')})
        rendered = check_docs.catalog_text(self.root, [Path('README.md')])
        self.assertNotIn('PRIVATE', rendered)
        self.assertIn('Public', rendered)

    def test_relative_file_image_and_fragment_links(self):
        docs = [self.write('docs/README.md', '[guide](guide.md#中文-api)\n![screen](screen.png)\n')]
        docs.append(self.write('docs/guide.md', '# 中文 API\n'))
        files = set(docs) | {self.write('docs/screen.png')}
        self.assertEqual(check_docs.check_links(self.root, docs, files), ([], 2))

    def test_missing_unversioned_absolute_and_escape_links_fail(self):
        doc = self.write('README.md', '\n'.join([
            '[missing](missing.md)', '[private](private.md)', '[heading](ok.md#missing)',
            '[escape](../outside.md)', '[absolute](/Users/example/README.md)',
            '[remote](https://example.invalid/docs)',
        ]))
        self.write('private.md', '# Secret\n')
        public = self.write('ok.md', '# Present\n')
        errors, count = check_docs.check_links(self.root, [doc, public], {doc, public})
        self.assertEqual(count, 5)
        self.assertEqual(len(errors), 5)
        self.assertTrue(any('unversioned target: private.md' in error for error in errors))
        self.assertTrue(any('missing heading' in error for error in errors))
        self.assertTrue(any('leaves repository' in error for error in errors))
        self.assertTrue(any('absolute link' in error for error in errors))

    def test_percent_encoded_link_and_html_anchor(self):
        doc = self.write('README.md', '[guide](docs/with%20space.md#%E4%B8%AD%E6%96%87)\n')
        page = self.write('docs/with space.md', '<a id="中文"></a>\n')
        self.assertEqual(check_docs.check_links(self.root, [doc, page], {doc, page}), ([], 1))

    def test_catalog_does_not_list_itself(self):
        self.write('README.md', '# Project\n')
        self.write('docs/CATALOG.md', '# Old index\n')
        rendered = check_docs.catalog_text(self.root, [Path('README.md'), Path('docs/CATALOG.md')])
        self.assertIn('[Project](../README.md)', rendered)
        self.assertNotIn('Old index', rendered)


if __name__ == '__main__':
    unittest.main()
