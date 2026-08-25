import 'json.dart';
import 'message_part.dart';
import 'token_usage.dart';

/// Mirrors `MessageWithParts` (frontend-v2 `shared/types/api.ts:170-187`).
/// IDs are prefix+ULID and lexicographically sortable ascending.
class ChatMessage {
  const ChatMessage({
    required this.id,
    required this.sessionId,
    required this.role,
    required this.parts,
    this.createdAt,
    this.clientMessageId,
    this.agent,
    this.model,
    this.parentId,
    this.finish,
    this.summary,
    this.tokens,
    this.error,
    this.reaction,
  });

  factory ChatMessage.fromJson(Map<String, dynamic> json) => ChatMessage(
        id: asString(json['id']) ?? '',
        sessionId: asString(json['session_id']) ?? '',
        role: asString(json['role']) ?? 'assistant',
        parts: asList(json['parts'])
            .whereType<Map<String, dynamic>>()
            .map(MessagePart.fromJson)
            .toList(),
        createdAt: asDate(json['created_at']),
        clientMessageId: asString(json['client_message_id']),
        agent: asString(json['agent']),
        model: asString(json['model']),
        parentId: asString(json['parent_id']),
        finish: asString(json['finish']),
        summary: asBool(json['summary']),
        tokens: json['tokens'] is Map<String, dynamic>
            ? TokenUsage.fromJson(json['tokens'] as Map<String, dynamic>)
            : null,
        error: json['error'] is Map<String, dynamic>
            ? json['error'] as Map<String, dynamic>
            : null,
        reaction: asString(json['reaction']),
      );

  final String id;
  final String sessionId;
  final String role; // user | assistant | system
  final List<MessagePart> parts;
  final DateTime? createdAt;
  final String? clientMessageId;
  final String? agent;
  final String? model;
  final String? parentId;
  final String? finish;
  final bool? summary;
  final TokenUsage? tokens;
  final Map<String, dynamic>? error;
  final String? reaction; // up | down | null

  bool get isUser => role == 'user';
  bool get isAssistant => role == 'assistant';

  String? get errorMessage =>
      error == null ? null : asString(error!['message']) ?? error.toString();

  ChatMessage copyWith({
    List<MessagePart>? parts,
    TokenUsage? tokens,
    String? finish,
    Map<String, dynamic>? error,
    String? model,
    String? reaction,
  }) =>
      ChatMessage(
        id: id,
        sessionId: sessionId,
        role: role,
        parts: parts ?? this.parts,
        createdAt: createdAt,
        clientMessageId: clientMessageId,
        agent: agent,
        model: model ?? this.model,
        parentId: parentId,
        finish: finish ?? this.finish,
        summary: summary,
        tokens: tokens ?? this.tokens,
        error: error ?? this.error,
        reaction: reaction ?? this.reaction,
      );

  /// Shallow-merge a partial `message.updated` payload (always `{id, role}`
  /// plus any of tokens/finish/error/model) — web stream.ts:158-166.
  ChatMessage mergePartial(Map<String, dynamic> json) => copyWith(
        tokens: json['tokens'] is Map<String, dynamic>
            ? TokenUsage.fromJson(json['tokens'] as Map<String, dynamic>)
            : null,
        finish: asString(json['finish']),
        error: json['error'] is Map<String, dynamic>
            ? json['error'] as Map<String, dynamic>
            : null,
        model: asString(json['model']),
      );
}
