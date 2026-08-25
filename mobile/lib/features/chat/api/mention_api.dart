import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/providers.dart';
import '../../../shared/models/json.dart';

/// Mention-menu data (web `features/chat/api/mention.ts`): skills and
/// commands. File search lives on the shared ContainersApi.
class MentionEntry {
  const MentionEntry({required this.name, this.description});

  final String name;
  final String? description;
}

List<MentionEntry> _parseEntries(List<dynamic>? data) => (data ?? const [])
    .whereType<Map<String, dynamic>>()
    .map((json) =>
        MentionEntry(name: asString(json['name']) ?? '', description: asString(json['description'])))
    .where((e) => e.name.isNotEmpty)
    .toList();

final mentionSkillsProvider = FutureProvider<List<MentionEntry>>((ref) async {
  final resp =
      await ref.watch(apiDioProvider).get<List<dynamic>>('/api/agent/skill');
  return _parseEntries(resp.data);
});

/// Backend currently returns [] — the menu handles that gracefully.
final mentionCommandsProvider = FutureProvider<List<MentionEntry>>((ref) async {
  try {
    final resp = await ref
        .watch(apiDioProvider)
        .get<List<dynamic>>('/api/agent/command');
    return _parseEntries(resp.data);
  } on DioException {
    return const [];
  }
});
