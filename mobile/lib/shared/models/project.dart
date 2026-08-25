import 'json.dart';

/// Mirrors `Project` (frontend-v2 `shared/types/api.ts:33-42`).
class Project {
  const Project({
    required this.id,
    required this.name,
    this.slug,
    this.description,
    this.sessionCount,
  });

  factory Project.fromJson(Map<String, dynamic> json) => Project(
        id: asString(json['id']) ?? '',
        name: asString(json['name']) ?? '',
        slug: asString(json['slug']),
        description: asString(json['description']),
        sessionCount: asInt(json['session_count']),
      );

  final String id;
  final String name;
  final String? slug;
  final String? description;
  final int? sessionCount;
}
