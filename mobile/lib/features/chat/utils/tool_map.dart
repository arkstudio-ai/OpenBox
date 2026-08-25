import '../../../shared/models/message_part.dart';

/// Tool → kind-label key + one-line detail, mirroring web
/// `features/chat/lib/tool-map.ts` (`describeTool`). Keys resolve under
/// `chat:kind.*`.
String toolKindKey(String tool) {
  final name = tool.toLowerCase();
  if (name.startsWith('mcp')) return 'mcp';
  if (name.contains('todo')) return 'todo';
  if (name.contains('skill')) return 'skill';
  if (name.contains('glob')) return 'glob';
  if (name.contains('grep')) return 'grep';
  if (name.contains('web_search') || name.contains('websearch')) {
    return 'webSearch';
  }
  if (name.contains('web_fetch') || name.contains('webfetch')) {
    return 'webFetch';
  }
  if (name.contains('multiedit') || name.contains('edit')) return 'edit';
  if (name.contains('write')) return 'write';
  if (name.contains('read')) return 'read';
  if (name.contains('bash') || name.contains('shell')) return 'bash';
  if (name.contains('task') || name.contains('agent')) return 'task';
  if (name.contains('question') || name.contains('ask')) return 'question';
  return 'tool';
}

/// Compact one-line detail for a tool row (path/command/query…).
String toolDetail(ToolPart part) {
  if (part.title != null && part.title!.isNotEmpty) return part.title!;
  final input = part.input;
  if (input is Map<String, dynamic>) {
    for (final key in const [
      'path',
      'file_path',
      'command',
      'query',
      'pattern',
      'url',
      'description',
      'name',
    ]) {
      final value = input[key];
      if (value is String && value.isNotEmpty) return value;
    }
  }
  return part.tool;
}

/// How long a call took, in seconds, or null if it never said. Two homes
/// because the live event and the stored part disagree (web `toolDuration`).
double? toolDuration(ToolPart part) {
  if (part.duration != null) return part.duration;
  final stored = part.metadata['duration'];
  return stored is num ? stored.toDouble() : null;
}

/// Pretty-print tool input/output for the expandable detail body.
String toolPayloadText(dynamic payload) {
  if (payload == null) return '';
  if (payload is String) return payload;
  if (payload is Map<String, dynamic>) {
    return payload.entries
        .map((e) => '${e.key}: ${e.value}')
        .join('\n');
  }
  return payload.toString();
}
