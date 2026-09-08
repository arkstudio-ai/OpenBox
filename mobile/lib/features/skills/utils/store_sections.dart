/// Laying the store out by shelf instead of by type — a 1:1 port of
/// frontend-v2 `features/skills-center/lib/store-sections.ts`.
///
/// Splitting the catalogue into "Skills" and "MCP servers" answered a question
/// nobody asks first. What decides whether someone installs a thing is who
/// stands behind it: us, another user, or a third party we merely host. So the
/// shelves are the sections, and the kind is a filter across all of them —
/// which is also what keeps a delisted third-party server from reading as ours.
library;

import '../../../shared/models/skill.dart';

class StoreSection {
  const StoreSection({required this.origin, required this.entries});

  final String origin; // official | community | third_party
  final List<CatalogEntry> entries;
}

class StoreShelves {
  const StoreShelves({required this.sections, required this.total});

  final List<StoreSection> sections;

  /// How many entries the store offers at all, before filter and search. The
  /// caller needs it to tell "the store is empty" from "your search matched
  /// nothing" — two different things to say, and only one is the person's own
  /// doing.
  final int total;
}

/// Ours first, then the people who use it, then everyone else.
const List<String> shelves = ['official', 'community', 'third_party'];

/// An entry from a backend that predates shelves is somebody's upload, not
/// ours: guessing `official` would put a stranger's work under our name.
String _shelfOf(CatalogEntry entry) => entry.origin ?? 'community';

bool _matches(String query, CatalogEntry entry) {
  final q = query.trim().toLowerCase();
  if (q.isEmpty) return true;
  return [
    entry.title,
    entry.description,
    entry.name,
    entry.tags.join(' '),
  ].any((field) => field.toLowerCase().contains(q));
}

/// Featured first, then what people actually install, then alphabetically.
///
/// The backend already orders each kind this way, but a shelf interleaves two
/// kinds, and concatenating two sorted lists is not a sorted list.
int _byShelfOrder(CatalogEntry a, CatalogEntry b) {
  if (a.featured != b.featured) return a.featured ? -1 : 1;
  final installs = b.installsCount - a.installsCount;
  if (installs != 0) return installs;
  final left = a.title.isNotEmpty ? a.title : a.name;
  final right = b.title.isNotEmpty ? b.title : b.name;
  return left.compareTo(right);
}

/// Group the catalogue into the sections the store renders.
///
/// [kind] is `all` | `skill` | `mcp`, matching the toolbar's filter.
StoreShelves buildStoreShelves(
  Catalog? catalog, {
  required String kind,
  required String query,
}) {
  final skills = catalog?.skills ?? const <CatalogEntry>[];
  final mcp = catalog?.mcp ?? const <CatalogEntry>[];
  final pool = <CatalogEntry>[
    if (kind != 'mcp') ...skills,
    if (kind != 'skill') ...mcp,
  ];
  final visible = pool.where((entry) => _matches(query, entry)).toList();

  final sections = <StoreSection>[];
  for (final origin in shelves) {
    final entries = visible.where((e) => _shelfOf(e) == origin).toList()
      ..sort(_byShelfOrder);
    if (entries.isNotEmpty) {
      sections.add(StoreSection(origin: origin, entries: entries));
    }
  }
  return StoreShelves(sections: sections, total: skills.length + mcp.length);
}
