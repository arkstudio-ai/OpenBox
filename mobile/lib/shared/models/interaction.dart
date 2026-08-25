import 'json.dart';

/// Mirrors `PermissionRequest` (frontend-v2 `shared/types/api.ts:250-258`).
class PermissionRequest {
  const PermissionRequest({
    required this.id,
    required this.sessionId,
    required this.tool,
    this.action,
    this.input,
    this.title,
    this.createdAt,
  });

  factory PermissionRequest.fromJson(Map<String, dynamic> json) =>
      PermissionRequest(
        id: asString(json['id']) ?? '',
        sessionId: asString(json['session_id']) ?? '',
        tool: asString(json['tool']) ?? '',
        action: asString(json['action']),
        input: json['input'],
        title: asString(json['title']),
        createdAt: asDate(json['created_at']),
      );

  final String id;
  final String sessionId;
  final String tool;
  final String? action;
  final dynamic input;
  final String? title;
  final DateTime? createdAt;
}

/// Mirrors `QuestionItem` / `QuestionRequest` (`shared/types/api.ts:278-286`).
class QuestionOption {
  const QuestionOption({required this.label, this.description});

  factory QuestionOption.fromJson(Map<String, dynamic> json) => QuestionOption(
        label: asString(json['label']) ?? '',
        description: asString(json['description']),
      );

  final String label;
  final String? description;
}

class QuestionItem {
  const QuestionItem({
    required this.question,
    this.header,
    this.options = const [],
    this.multiple = false,
    this.custom = false,
  });

  factory QuestionItem.fromJson(Map<String, dynamic> json) => QuestionItem(
        question: asString(json['question']) ?? '',
        header: asString(json['header']),
        options: asList(json['options'])
            .whereType<Map<String, dynamic>>()
            .map(QuestionOption.fromJson)
            .toList(),
        multiple: asBool(json['multiple']) ?? false,
        custom: asBool(json['custom']) ?? false,
      );

  final String question;
  final String? header;
  final List<QuestionOption> options;
  final bool multiple;
  final bool custom;
}

class QuestionRequest {
  const QuestionRequest({
    required this.id,
    required this.sessionId,
    required this.questions,
    this.tool,
    this.createdAt,
  });

  factory QuestionRequest.fromJson(Map<String, dynamic> json) =>
      QuestionRequest(
        id: asString(json['id']) ?? '',
        sessionId: asString(json['session_id']) ?? '',
        questions: asList(json['questions'])
            .whereType<Map<String, dynamic>>()
            .map(QuestionItem.fromJson)
            .toList(),
        tool: asString(json['tool']),
        createdAt: asDate(json['created_at']),
      );

  final String id;
  final String sessionId;
  final List<QuestionItem> questions;
  final String? tool;
  final DateTime? createdAt;
}
