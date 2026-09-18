import 'package:bossip_mobile/shared/models/message.dart';
import 'package:bossip_mobile/shared/models/message_part.dart';

ChatMessage compactRequest({
  String id = 'm02',
  bool auto = true,
  String? replacement,
}) => ChatMessage.fromJson({
  'id': id,
  'session_id': 's1',
  'role': 'user',
  'agent': 'compaction',
  'parts': [
    {
      'id': '$id-part',
      'type': 'compaction',
      'auto': auto,
      'replacement_id': replacement,
    },
  ],
});

ChatMessage compactSummary({
  String id = 'm03',
  String parent = 'm02',
  String? finish,
  bool legacy = false,
}) => ChatMessage.fromJson({
  'id': id,
  'session_id': 's1',
  'role': 'assistant',
  'agent': legacy ? 'build' : 'compaction',
  'parent_id': parent,
  'summary': legacy || finish == 'stop',
  'finish': finish,
  'parts': [
    {
      'id': '$id-text',
      'type': 'text',
      'text': '## Goal\nKeep the password 青竹-0917',
    },
  ],
});

ChatMessage answer({
  String id = 'm01',
  String text = 'Answer',
  String? finish = 'stop',
}) => ChatMessage(
  id: id,
  sessionId: 's1',
  role: 'assistant',
  finish: finish,
  reaction: 'up',
  parts: [TextPart(id: '$id-text', text: text)],
);
