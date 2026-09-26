import '../../../shared/models/json.dart';

/// Store categories the product knows about (backend `db.models.store`).
/// Which of them can actually be picked comes from `openCategories` on the
/// list response, so a newly opened industry needs no app release.
const storeCategories = ['food', 'beauty', 'retail'];
const storePlatforms = ['douyin_laike', 'meituan_merchant'];

/// One merchant store per workspace (docs/OPS_CASE_PLAN.md §3.1).
class Store {
  const Store({
    required this.id,
    required this.workspaceId,
    required this.name,
    required this.category,
    this.categoryOpen = true,
    this.mainPlatforms = const [],
    this.address,
    this.city,
    this.platformBindings = const {},
    this.personaStatus = 'none',
    this.personaSessionId,
  });

  factory Store.fromJson(Map<String, dynamic> json) => Store(
    id: asString(json['id']) ?? '',
    workspaceId: asString(json['workspaceId']) ?? '',
    name: asString(json['name']) ?? '',
    category: asString(json['category']) ?? 'other',
    categoryOpen: asBool(json['categoryOpen']) ?? true,
    mainPlatforms: asList(json['mainPlatforms']).whereType<String>().toList(),
    address: asString(json['address']),
    city: asString(json['city']),
    platformBindings: asMap(json['platformBindings']),
    personaStatus: asString(json['personaStatus']) ?? 'none',
    personaSessionId: asString(json['personaSessionId']),
  );

  final String id;
  final String workspaceId;
  final String name;

  /// `food` | `beauty` | `retail` | `other`.
  final String category;
  final bool categoryOpen;
  final List<String> mainPlatforms;
  final String? address;
  final String? city;
  final Map<String, dynamic> platformBindings;

  /// `none` | `proposed` | `active` (§4.3).
  final String personaStatus;
  final String? personaSessionId;

  /// The industry whose starter cards apply: `other` has no card set of its
  /// own and reads the food ones.
  String get starterIndustry => category == 'other' ? 'food' : category;
}

/// `GET /api/stores`: the workspace's store (at most one) plus the catalogue
/// the setup form is built from.
class StoreSnapshot {
  const StoreSnapshot({
    this.store,
    this.categories = storeCategories,
    this.openCategories = const ['food'],
    this.platforms = storePlatforms,
  });

  factory StoreSnapshot.fromJson(Map<String, dynamic> json) {
    final items = asList(json['items']).whereType<Map<String, dynamic>>();
    final categories = asList(json['categories']).whereType<String>().toList();
    final open = asList(json['openCategories']).whereType<String>().toList();
    final platforms = asList(json['platforms']).whereType<String>().toList();
    return StoreSnapshot(
      store: items.isEmpty ? null : Store.fromJson(items.first),
      categories: categories.isEmpty ? storeCategories : categories,
      openCategories: open.isEmpty ? const ['food'] : open,
      platforms: platforms.isEmpty ? storePlatforms : platforms,
    );
  }

  final Store? store;
  final List<String> categories;
  final List<String> openCategories;
  final List<String> platforms;

  bool get hasStore => store != null;

  StoreSnapshot withStore(Store? store) => StoreSnapshot(
    store: store,
    categories: categories,
    openCategories: openCategories,
    platforms: platforms,
  );
}

/// A store-aware suggestion on the empty chat (§2.4).
class StarterCard {
  const StarterCard({required this.title, required this.hint});

  factory StarterCard.fromJson(Map<String, dynamic> json) => StarterCard(
    title: asString(json['title']) ?? '',
    hint: asString(json['hint']) ?? '',
  );

  final String title;
  final String hint;
}
