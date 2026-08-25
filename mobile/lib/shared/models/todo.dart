import 'json.dart';

/// Mirrors `TodoItem` (frontend-v2 `shared/types/api.ts:210-223`).
enum TodoStatus { pending, inProgress, completed, cancelled }

TodoStatus todoStatusFrom(String? value) => switch (value) {
      'in_progress' => TodoStatus.inProgress,
      'completed' => TodoStatus.completed,
      'cancelled' => TodoStatus.cancelled,
      _ => TodoStatus.pending,
    };

class TodoItem {
  const TodoItem({
    required this.id,
    required this.subject,
    required this.status,
    this.description,
    this.activeForm,
    this.priority,
    this.source,
    this.startedAt,
  });

  factory TodoItem.fromJson(Map<String, dynamic> json) => TodoItem(
        id: asString(json['id']) ?? '',
        subject: asString(json['subject']) ?? '',
        status: todoStatusFrom(asString(json['status'])),
        description: asString(json['description']),
        activeForm: asString(json['active_form']),
        priority: asString(json['priority']),
        source: asString(json['source']),
        startedAt: asDate(json['started_at']),
      );

  final String id;
  final String subject;
  final TodoStatus status;
  final String? description;
  final String? activeForm;
  final String? priority; // high | medium | low
  final String? source; // model | user
  final DateTime? startedAt;
}
