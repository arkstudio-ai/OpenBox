/// Grouping installed skills back into the things that were actually
/// installed — a 1:1 port of frontend-v2
/// `features/skills-center/lib/group-skills.ts`.
///
/// A skill pack is one install that unpacks into many skills: cloning
/// anthropic/skills lands 19 SKILL.md files under one directory, and the scan
/// reports 19 entries that all carry the same install_dir. Listing them flat
/// makes 我的 read as 23 installs when the person performed four, and — worse
/// — the uninstall on any one row is addressed by install_dir, so removing
/// docx would take the other 18 with it without saying so.
library;

import '../../../shared/models/skill.dart';

class SkillGroup {
  const SkillGroup({
    required this.id,
    required this.name,
    required this.members,
    required this.isPack,
    required this.removable,
    required this.origin,
    required this.category,
    required this.publicationStatus,
    this.libraryId,
    this.catalogId,
    this.publishedAt,
    this.icon,
    this.description,
  });

  /// The directory the install produced — what uninstall actually removes.
  final String id;

  /// Shown as the row title.
  final String name;
  final List<InstalledSkill> members;

  /// A pack is anything one install left behind as more than one skill.
  final bool isPack;

  /// Only container installs can be removed from here.
  final bool removable;

  /// Where it came from — decides which badge the row wears.
  final String origin; // container | builtin | host

  /// Product grouping supplied by the backend's durable install registry.
  final String category; // personal | store | installed | builtin | host
  final String? publicationStatus; // unpublished | published
  final String? libraryId;
  final String? catalogId;
  final String? publishedAt;
  final String? icon;
  final String? description;

  bool get isPersonal => category == 'personal';

  bool get isPublished => publicationStatus == 'published';
}

String _legacyCategory(InstalledSkill skill) {
  if (skill.source == 'builtin') return 'builtin';
  if (skill.source == 'container') return 'installed';
  return 'host';
}

List<SkillGroup> groupSkills(List<InstalledSkill> skills) {
  final byDir = <String, List<InstalledSkill>>{};
  for (final skill in skills) {
    // Host skills have no install_dir; they are their own group and are not
    // removable anyway.
    final key = (skill.installDir?.isNotEmpty ?? false)
        ? skill.installDir!
        : skill.name;
    byDir.putIfAbsent(key, () => []).add(skill);
  }

  final groups = <SkillGroup>[];
  for (final entry in byDir.entries) {
    final members = entry.value;
    final isPack = members.length > 1;
    final first = members.first;
    final category = first.category ?? _legacyCategory(first);
    final hasContainer = members.any((m) => m.source == 'container');
    groups.add(SkillGroup(
      id: entry.key,
      // A pack is named by its directory, since no single member's name
      // describes the whole thing.
      name: isPack ? entry.key : first.name,
      members: members,
      isPack: isPack,
      removable: hasContainer,
      origin: hasContainer
          ? 'container'
          : members.any((m) => m.source == 'builtin')
              ? 'builtin'
              : 'host',
      category: category,
      publicationStatus:
          category == 'personal' ? (first.publicationStatus ?? 'unpublished') : null,
      libraryId: first.libraryId,
      catalogId: first.catalogId,
      publishedAt: first.publishedAt,
      icon: isPack ? null : first.icon,
      description: isPack ? null : first.description,
    ));
  }

  // Packs first, then alphabetical: the biggest thing installed is the one
  // someone is most likely looking for.
  groups.sort((a, b) {
    if (a.isPack != b.isPack) return a.isPack ? -1 : 1;
    return a.name.compareTo(b.name);
  });
  return groups;
}

/// One dependency and what has to happen to it (web `Dependency`).
class SkillDependency {
  const SkillDependency({
    required this.name,
    required this.configured,
    this.catalog,
  });

  final String name;

  /// Already configured on the sandbox, just not connected.
  final bool configured;

  /// Present when the store knows how to install it.
  final CatalogEntry? catalog;

  bool get actionable => configured || catalog != null;
}

/// Dependencies of [requiresMcp] that are not usable yet.
///
/// "Configured but disconnected" and "not there at all" both leave the skill
/// broken, but they need different fixes, so they are distinguished rather
/// than lumped into one "missing".
List<SkillDependency> unmetDependencies(
  List<String> requiresMcp,
  List<McpServer> servers,
  List<CatalogEntry> catalogMcp,
) {
  final byName = {for (final s in servers) s.name: s};
  final catalogByName = {for (final c in catalogMcp) c.name: c};
  final out = <SkillDependency>[];
  for (final name in requiresMcp) {
    final server = byName[name];
    if (server != null && server.isConnected) continue;
    out.add(SkillDependency(
      name: name,
      configured: server != null,
      catalog: catalogByName[name],
    ));
  }
  return out;
}
