import 'package:bossip_mobile/features/memory/models/memory_models.dart';
import 'package:bossip_mobile/features/memory/models/wiki_models.dart';
import 'package:bossip_mobile/features/memory/utils/knowledge_text.dart';
import 'package:bossip_mobile/features/memory/utils/wiki_content.dart';
import 'package:bossip_mobile/shared/router/paths.dart';
import 'package:bossip_mobile/shared/utils/format.dart';
import 'package:flutter_test/flutter_test.dart';

WikiPage _page(
  String body, {
  List<Map<String, dynamic>> citations = const [],
  String? exchangePath,
  Map<String, dynamic> exchangeLinks = const {},
}) => WikiPage.fromJson({
  'id': 'p1',
  'title': 'Guide',
  'slug': 'guide',
  'status': 'published',
  'body_available': true,
  'body': body,
  'project_id': 'project',
  'paragraphs': [
    {'text': 'x', 'citations': citations},
  ],
  'exchange_path': exchangePath,
  'exchange_links': exchangeLinks,
});

const _s1 = {'source_id': 's1', 'revision': 2, 'quote': 'We meet every week.'};

final _decisions = WikiSummary.fromJson({
  'id': 'p2',
  'slug': 'decisions',
  'title': 'Decisions',
  'body_available': true,
  'status': 'published',
  'project_id': 'project',
  'source_ids': ['s1'],
});

String _prepare(WikiPage page, [List<WikiSummary> pages = const []]) =>
    prepareWikiMarkdown(page, citations: citationsOf(page), pages: pages);

void main() {
  test('a card preview drops Markdown and markers but keeps real words', () {
    expect(
      plainExcerpt(
        '# Guide\n\n## Working together\n\n- Use **weekly** check-ins. [source:s1@2]',
        'Guide',
      ),
      'Working together Use weekly check-ins.',
    );
    expect(
      plainExcerpt(
        'See [[decisions|the decision log]] and [docs](https://example.org).',
      ),
      'See the decision log and docs.',
    );
    expect(
      plainExcerpt('| Type | Count |\n| --- | --- |\n| Group | 12 |'),
      'Type Count Group 12',
    );
    // A sentence that merely starts with the title keeps it.
    expect(plainExcerpt('云杉项目的负责人是小李。', '云杉项目'), '云杉项目的负责人是小李。');
    expect(plainExcerpt('# 云杉项目\n负责人是小李。', '云杉项目'), '负责人是小李。');
  });

  test('search matches and marks the term regardless of case', () {
    expect(matchesQuery('Use Shanghai timezone', 'shanghai'), isTrue);
    expect(matchesQuery('Use Shanghai timezone', ''), isTrue);
    expect(matchesQuery(null, 'x'), isFalse);
    expect(splitMatches('Ab ab', 'ab'), [
      (text: 'Ab', hit: true),
      (text: ' ', hit: false),
      (text: 'ab', hit: true),
    ]);
    expect(splitMatches('no hit', ''), [(text: 'no hit', hit: false)]);
  });

  test('unknown views open the overview', () {
    expect(readView('files'), 'files');
    expect(readView('reviews'), 'overview');
    expect(readView(null), 'overview');
  });

  test('a forgotten or stopped memory never counts as readable', () {
    final memory = MemoryRecord.fromJson({
      'id': 'm',
      'summary': 'x',
      'status': 'ACTIVE',
      'revision': 1,
    });
    expect(memoryIsStopped(memory), isFalse);
    expect(memoryIsStopped(memory.forgotten()), isTrue);
    expect(
      memoryIsStopped(memory, const MemoryCleanup(status: 'cleaned')),
      isTrue,
    );
    expect(
      memoryIsStopped(memory, const MemoryCleanup(status: 'x', stopped: true)),
      isTrue,
    );
  });

  test('request ids are fresh UUIDs', () {
    final id = newRequestId();
    expect(
      RegExp(
        r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$',
      ).hasMatch(id),
      isTrue,
    );
    expect(newRequestId(), isNot(id));
  });

  test('a citation on its own line ends the text it supports', () {
    final page = _page(
      '# Guide\n\nWe meet weekly.\n\n[source:s1@2]\n\n- Notes are shared.\n\n[source:s1@2]',
      citations: [_s1],
    );
    expect(
      _prepare(page),
      'We meet weekly. [1](#wiki-citation-0)\n\n'
      '- Notes are shared. [1](#wiki-citation-0)',
    );
  });

  test('repeated numbers collapse; a marker citing nothing disappears', () {
    final page = _page(
      'A.\n[source:s1@16] [source:s1@16] [source:ghost@1]',
      citations: [
        {'source_id': 's1', 'revision': 16, 'quote': 'First excerpt'},
        {'source_id': 's1', 'revision': 16, 'quote': 'Second excerpt'},
      ],
    );
    final prepared = _prepare(page);
    expect(prepared, startsWith('A. [1](#wiki-citation-0)'));
    expect(prepared, isNot(contains('ghost')));
    expect(prepared, isNot(contains('[source:')));
    expect(citationsOf(page).single.quotes, [
      'First excerpt',
      'Second excerpt',
    ]);
  });

  test('topic links resolve only to one readable page in the same scope', () {
    final page = _page(
      '[[decisions|Decision log]] and [[decisions]] and [[unknown]]\n\n'
      '`[[decisions]]`\n\n```\n[[decisions]]\n```',
    );
    final prepared = _prepare(page, [_decisions]);
    expect(prepared, contains('[Decision log](#wiki-page-p2)'));
    expect(prepared, contains('[Decisions](#wiki-page-p2)'));
    expect(prepared, contains(' and unknown\n'));
    // Code stays exactly as written.
    expect(prepared, contains('`[[decisions]]`'));
    expect(prepared, contains('```\n[[decisions]]\n```'));

    final elsewhere = WikiSummary.fromJson({
      'id': 'p9',
      'slug': 'decisions',
      'title': 'Decisions',
      'body_available': true,
      'status': 'published',
      'project_id': 'other-project',
    });
    expect(_prepare(_page('[[decisions]]'), [elsewhere]), 'decisions');
    // Two candidates: ambiguous, so no link.
    expect(
      _prepare(_page('[[decisions]]'), [_decisions, _decisions]),
      'decisions',
    );
  });

  test('raw HTML never renders and remote images show only their text', () {
    final prepared = _prepare(
      _page(
        'Before\n\n<script>alert(1)</script>\n\n<b>bold</b> '
        '![hidden](https://untrusted.test/track) <!-- note -->',
      ),
    );
    expect(prepared, isNot(contains('script')));
    expect(prepared, isNot(contains('alert')));
    expect(prepared, isNot(contains('untrusted.test')));
    expect(prepared, isNot(contains('note')));
    expect(prepared, contains('bold hidden'));
  });

  test('imported pages link only to reviewed local pages and sources', () {
    final page = _page(
      '',
      exchangePath: 'concepts/guide.md',
      exchangeLinks: {
        'concepts/decisions.md': {'page_id': 'p2'},
        'references/notes.md': {'source_id': 's1'},
      },
    );
    expect(exchangeHref('decisions.md', page), '#wiki-page-p2');
    expect(
      exchangeHref('/references/notes.md', page),
      '#wiki-import-source-s1',
    );
    expect(exchangeHref('pending.md', page), isNull);
    expect(exchangeHref('//evil.test/x.md', page), isNull);
    expect(exchangeHref('https://example.org', page), 'https://example.org');
    expect(
      exchangeHref('https://example.org', _page('')),
      'https://example.org',
    );
  });

  test('an export carries the text and every quoted excerpt', () {
    final page = _page('# Guide\n\nUse weekly check-ins.', citations: [_s1]);
    expect(
      wikiExport(page),
      '# Guide\n\nUse weekly check-ins.\n\n---\n\n'
      '[source:s1@2]\n\n> We meet every week.\n',
    );
    final stale = WikiPage.fromJson({
      'id': 'p1',
      'status': 'stale',
      'body_available': false,
      'body': 'old',
    });
    expect(wikiExport(stale), '');
    expect(relatedPages(stale, [_decisions]), isEmpty);
    expect(relatedPages(page, [_decisions]).single.id, 'p2');
  });

  test('knowledge paths mirror the web', () {
    expect(Paths.wiki(), '/app/wiki');
    expect(
      Paths.wiki(projectId: 'p1', view: 'memories'),
      '/app/wiki?project=p1&view=memories',
    );
    expect(Paths.wikiPage('page-1'), '/app/wiki/page-1');
    expect(
      Paths.wikiPage('page 1', projectId: 'p1'),
      '/app/wiki/page%201?project=p1',
    );
    expect(Paths.memory, '/app/memory');
  });

  test('a short time ago is relative, an older one a date', () {
    final now = DateTime.utc(2026, 10, 7, 12);
    expect(
      formatSince(DateTime.utc(2026, 10, 7, 10), 'en-US', now: now),
      '2 hours ago',
    );
    expect(formatSince(DateTime.utc(2026, 9, 1), 'en-US', now: now), 'Sep 1');
    expect(
      formatSince(DateTime.utc(2025, 9, 1), 'en-US', now: now),
      'Sep 1, 2025',
    );
  });
}
