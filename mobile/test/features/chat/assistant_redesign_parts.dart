/// Messages, parts and watched tasks the assistant tests build from.
library;

import 'package:bossip_mobile/shared/models/message.dart';
import 'package:bossip_mobile/shared/models/message_part.dart';

ToolPart toolPart(String tool, {String status = 'completed', Object? output}) =>
    MessagePart.fromJson({
          'type': 'tool',
          'id': 'part-$tool-$status',
          'tool': tool,
          'status': status,
          'output': ?output,
        })
        as ToolPart;

ChatMessage userMessage(
  String id, {
  String text = 'Please make the home page dark',
  String? origin,
  Map<String, dynamic>? ref,
  bool synthetic = false,
}) => ChatMessage.fromJson({
  'id': id,
  'session_id': 'main',
  'role': 'user',
  'created_at': DateTime.now().toUtc().toIso8601String(),
  'parts': [
    {
      'id': '$id-text',
      'type': 'text',
      'text': text,
      'synthetic': synthetic,
      'origin': ?origin,
      'origin_ref': ?ref,
    },
  ],
});

ChatMessage replyMessage(
  String id, {
  String? text,
  List<Map<String, dynamic>> parts = const [],
  String? finish = 'stop',
  String? parent,
}) => ChatMessage.fromJson({
  'id': id,
  'session_id': 'main',
  'role': 'assistant',
  'parent_id': ?parent,
  'finish': finish,
  'model': 'test/model',
  'tokens': {'input': 12345, 'output': 678},
  'created_at': DateTime.now().toUtc().toIso8601String(),
  'parts': [
    ...parts,
    if (text != null) {'id': '$id-text', 'type': 'text', 'text': text},
  ],
});

Map<String, dynamic> watchItem(
  String id, {
  String title = 'Task',
  String sessionStatus = 'idle',
  String desired = 'running',
  String observed = 'idle',
  int pending = 0,
  String? outcome,
  String summary = '',
}) => {
  'task_id': id,
  'title': title,
  'project': {'id': 'project', 'name': 'Snake game'},
  'session_id': 'session-$id',
  'session_status': sessionStatus,
  'desired_state': desired,
  'observed_state': observed,
  'revision': 1,
  'updated_at': DateTime.now().toUtc().toIso8601String(),
  'pending_questions': pending,
  if (outcome != null)
    'latest_result': {
      'result_id': 'result-$id',
      'outcome': outcome,
      'delivery_state': 'processed',
      'created_at': DateTime.now().toUtc().toIso8601String(),
      'summary': summary,
    },
};
