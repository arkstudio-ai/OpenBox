import '../../../shared/models/message_part.dart';

/// Plain-words name of a tool call for Light 模式's one-line trace — the
/// "AI 做了什么" vocabulary of the trajectory monitor's 简明模式 rather than
/// the tool's own name. Keys resolve under `light:tool.*`.
String lightToolKey(MessagePart? part) {
  if (part is SubtaskPart) return 'task';
  if (part is! ToolPart) return 'working';
  final name = part.tool.toLowerCase();
  if (name.contains('compose')) return 'compose';
  if (name.contains('video')) return 'video';
  if (name.contains('image')) return 'image';
  if (name.contains('transcribe') || name.contains('speech')) {
    return 'transcribe';
  }
  if (name.contains('publish') || name.contains('douyin')) return 'publish';
  if (name.contains('desktop') ||
      name.contains('computer') ||
      name.contains('browser')) {
    return 'desktop';
  }
  if (name.contains('cron') || name.contains('schedule')) return 'schedule';
  if (name.contains('question') || name.contains('ask')) return 'question';
  if (name == 'skill') return 'skill';
  if (name.contains('web_search') || name.contains('websearch')) {
    return 'search';
  }
  if (name.contains('web_fetch') || name.contains('webfetch') || name == 'fetch') {
    return 'fetch';
  }
  if (name.contains('todo')) return 'plan';
  if (name.contains('task') || name.contains('agent')) return 'task';
  if (name.contains('bash') ||
      name.contains('shell') ||
      name.contains('read') ||
      name.contains('write') ||
      name.contains('edit') ||
      name.contains('glob') ||
      name.contains('grep')) {
    return 'files';
  }
  return 'working';
}
