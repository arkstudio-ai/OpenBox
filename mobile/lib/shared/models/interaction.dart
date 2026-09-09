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
    this.custom = true,
    this.detail,
  });

  factory QuestionItem.fromJson(Map<String, dynamic> json) => QuestionItem(
    question: asString(json['question']) ?? '',
    header: asString(json['header']),
    options: asList(
      json['options'],
    ).whereType<Map<String, dynamic>>().map(QuestionOption.fromJson).toList(),
    multiple: asBool(json['multiple']) ?? false,
    // Absent means allowed — only an explicit false closes the text box
    // (web `item.custom !== false`; the backend's own default is true).
    custom: asBool(json['custom']) ?? true,
    detail: json['detail'] is Map<String, dynamic>
        ? json['detail'] as Map<String, dynamic>
        : null,
  );

  final String question;
  final String? header;
  final List<QuestionOption> options;
  final bool multiple;
  final bool custom;

  /// Structured context rendered by first-party confirmation cards
  /// (video script/segment approvals).
  final Map<String, dynamic>? detail;
}

class QuestionRequest {
  const QuestionRequest({
    required this.id,
    required this.sessionId,
    required this.questions,
    this.tool,
    this.createdAt,
    this.generation = 0,
    this.status = 'pending',
    this.draft = const [],
    this.draftRevision = 0,
    this.expiresAt,
  });

  factory QuestionRequest.fromJson(Map<String, dynamic> json) =>
      QuestionRequest(
        id: asString(json['id']) ?? '',
        sessionId: asString(json['session_id']) ?? '',
        questions: asList(
          json['questions'],
        ).whereType<Map<String, dynamic>>().map(QuestionItem.fromJson).toList(),
        tool: asString(json['tool']),
        createdAt: asDate(json['created_at']),
        generation: asInt(json['generation']) ?? 0,
        status: asString(json['status']) ?? 'pending',
        draft: asList(json['draft'])
            .whereType<Map<String, dynamic>>()
            .map(QuestionDraftAnswer.fromJson)
            .toList(),
        draftRevision: asInt(json['draft_revision']) ?? 0,
        expiresAt: asDate(json['expires_at']),
      );

  final String id;
  final String sessionId;
  final List<QuestionItem> questions;
  final String? tool;
  final DateTime? createdAt;
  final int generation;
  final String status;
  final List<QuestionDraftAnswer> draft;
  final int draftRevision;
  final DateTime? expiresAt;
}

class QuestionDraftAnswer {
  const QuestionDraftAnswer({
    this.selected = const [],
    this.custom = '',
    this.useCustom = false,
  });

  factory QuestionDraftAnswer.fromJson(Map<String, dynamic> json) =>
      QuestionDraftAnswer(
        selected: asList(json['selected']).whereType<String>().toList(),
        custom: asString(json['custom']) ?? '',
        useCustom: asBool(json['use_custom']) ?? false,
      );

  final List<String> selected;
  final String custom;
  final bool useCustom;

  Map<String, dynamic> toJson() => {
    'selected': selected,
    'custom': custom,
    'use_custom': useCustom,
  };
}
