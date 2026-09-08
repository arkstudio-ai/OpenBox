import 'package:bossip_mobile/features/skills/utils/group_skills.dart';
import 'package:bossip_mobile/features/skills/utils/listing.dart';
import 'package:bossip_mobile/features/skills/utils/store_sections.dart';
import 'package:bossip_mobile/features/skills/widgets/entry_row.dart';
import 'package:bossip_mobile/shared/models/skill.dart';
import 'package:flutter_test/flutter_test.dart';

CatalogEntry entry(
  String name, {
  String kind = 'skill',
  String? origin,
  bool featured = false,
  int installs = 0,
  String? title,
  String description = '',
}) =>
    CatalogEntry.fromJson({
      'id': name,
      'name': name,
      'title': title ?? name,
      'icon': '',
      'description': description,
      'installed': false,
      'origin': ?origin,
      'featured': featured,
      'installs_count': installs,
    }, kind);

InstalledSkill personal({
  String? publicationStatus,
  String? listing,
  String? note,
}) =>
    InstalledSkill.fromJson({
      'name': 'mine',
      'install_dir': 'mine',
      'source': 'container',
      'category': 'personal',
      'publication_status': ?publicationStatus,
      'listing': ?listing,
      'listing_note': ?note,
    });

void main() {
  group('listingChipFor', () {
    test('collapses the author and operator axes into one state', () {
      expect(listingChipFor('published', 'pending'), ListingChip.pending);
      expect(listingChipFor('published', 'listed'), ListingChip.listed);
      expect(listingChipFor('published', 'rejected'), ListingChip.rejected);
      expect(listingChipFor('published', 'delisted'), ListingChip.delisted);
      // Withdrawal is the author's own decision and outranks whatever the
      // shelf last said, or a pulled-but-approved release reads as on sale.
      expect(listingChipFor('withdrawn', 'listed'), ListingChip.withdrawn);
    });

    test('a draft that was never submitted gets no chip', () {
      expect(listingChipFor('unpublished', null), isNull);
      expect(listingChipFor(null, null), isNull);
    });

    test('an older backend without shelves means published = listed', () {
      expect(listingChipFor('published', null), ListingChip.listed);
    });
  });

  test('only a refusal or a delisting owes the author a reason', () {
    expect(explainsItself(ListingChip.rejected), isTrue);
    expect(explainsItself(ListingChip.delisted), isTrue);
    expect(explainsItself(ListingChip.pending), isFalse);
    expect(explainsItself(ListingChip.listed), isFalse);
    expect(explainsItself(ListingChip.withdrawn), isFalse);
  });

  test('a refusal shouts louder than a delisting', () {
    expect(listingTones[ListingChip.rejected], BadgeTone.danger);
    expect(listingTones[ListingChip.delisted], BadgeTone.muted);
    expect(listingTones[ListingChip.listed], BadgeTone.ok);
  });

  test('re-submission covers every state an operator already ruled on', () {
    expect(isResubmission(ListingChip.rejected), isTrue);
    expect(isResubmission(ListingChip.delisted), isTrue);
    expect(isResubmission(ListingChip.withdrawn), isTrue);
    expect(isResubmission(ListingChip.listed), isFalse);
    expect(isResubmission(ListingChip.pending), isFalse);
    expect(isResubmission(null), isFalse);
  });

  test('withdrawal is offered while a release still stands', () {
    expect(canWithdraw(ListingChip.listed), isTrue);
    expect(canWithdraw(ListingChip.pending), isTrue);
    expect(canWithdraw(ListingChip.delisted), isTrue);
    // Nothing left to pull.
    expect(canWithdraw(ListingChip.withdrawn), isFalse);
    expect(canWithdraw(null), isFalse);
  });

  test('groupSkills carries the listing axis onto the row', () {
    final groups = groupSkills([
      personal(publicationStatus: 'published', listing: 'rejected', note: 'no'),
    ]);
    expect(groups.single.listing, 'rejected');
    expect(groups.single.listingNote, 'no');
    expect(
      listingChipFor(groups.single.publicationStatus, groups.single.listing),
      ListingChip.rejected,
    );
  });

  group('buildStoreShelves', () {
    final catalog = Catalog(
      skills: [
        entry('ours', origin: 'official'),
        entry('theirs', origin: 'third_party'),
        entry('someone', origin: 'community'),
      ],
      mcp: [entry('server', kind: 'mcp', origin: 'third_party')],
    );

    test('shelves run ours, then users, then everyone else', () {
      final shelves = buildStoreShelves(catalog, kind: 'all', query: '');
      expect(
        shelves.sections.map((s) => s.origin),
        ['official', 'community', 'third_party'],
      );
      expect(shelves.total, 4);
    });

    test('an entry with no origin is somebody upload, never ours', () {
      final shelves = buildStoreShelves(
        Catalog(skills: [entry('legacy')]),
        kind: 'all',
        query: '',
      );
      expect(shelves.sections.single.origin, 'community');
    });

    test('kind filters across every shelf rather than splitting them', () {
      final shelves = buildStoreShelves(catalog, kind: 'mcp', query: '');
      expect(shelves.sections.single.origin, 'third_party');
      expect(shelves.sections.single.entries.single.name, 'server');
      // total ignores the filter, so the caller can still say "the store has
      // things, your filter does not".
      expect(shelves.total, 4);
    });

    test('empty sections are dropped, and total survives a fruitless search', () {
      final shelves = buildStoreShelves(catalog, kind: 'all', query: 'zzz');
      expect(shelves.sections, isEmpty);
      expect(shelves.total, 4);
    });

    test('featured first, then installs, then name', () {
      final shelves = buildStoreShelves(
        Catalog(skills: [
          entry('b-popular', origin: 'official', installs: 9),
          entry('a-quiet', origin: 'official'),
          entry('c-pinned', origin: 'official', featured: true),
        ]),
        kind: 'all',
        query: '',
      );
      expect(
        shelves.sections.single.entries.map((e) => e.name),
        ['c-pinned', 'b-popular', 'a-quiet'],
      );
    });

    test('search reaches the fields a person actually types', () {
      final shelves = buildStoreShelves(
        Catalog(skills: [
          entry('a', title: 'Browser QA', description: 'drive a real browser'),
          entry('b', title: 'PDF', description: 'documents'),
        ]),
        kind: 'all',
        query: 'BROWSER',
      );
      expect(shelves.sections.single.entries.single.title, 'Browser QA');
    });
  });
}
