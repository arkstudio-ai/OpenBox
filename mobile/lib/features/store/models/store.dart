import '../../../shared/models/json.dart';

/// One merchant store per workspace (docs/OPS_CASE_PLAN.md §3.1). The Light
/// home reads its name; the backend reads the rest.
class Store {
  const Store({required this.id, required this.name, this.headline});

  factory Store.fromJson(Map<String, dynamic> json) => Store(
    id: asString(json['id']) ?? '',
    name: asString(json['name']) ?? '',
    headline: asString(json['headline']),
  );

  final String id;
  final String name;

  /// What the shop leads with (主打).
  final String? headline;
}

/// One line of 「今天可以做」: `GET /api/stores/{id}/starter-cards`.
class StarterCard {
  const StarterCard({
    required this.entryId,
    required this.title,
    this.hint = '',
  });

  factory StarterCard.fromJson(Map<String, dynamic> json) => StarterCard(
    entryId: asString(json['entry']) ?? '',
    title: asString(json['title']) ?? '',
    hint: asString(json['hint']) ?? '',
  );

  /// The Light entry the card opens.
  final String entryId;
  final String title;
  final String hint;
}
