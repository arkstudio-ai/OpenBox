import '../../../shared/models/message_part.dart';

/// Tool → kind-label key + one-line detail, mirroring web
/// `features/chat/lib/tool-map.ts` (`describeTool`). Keys resolve under
/// `chat:kind.*`.
String toolKindKey(String tool) {
  final name = tool.toLowerCase();
  if (name.startsWith('mcp')) return 'mcp';
  if (name.contains('todo')) return 'todo';
  // Exact match only: an MCP server or a user tool whose name merely
  // contains "skill" is not a skill load (web tool-map.ts).
  if (name == 'skill') return 'skill';
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

/// Structural layout for a tool's detail column — how its output is composed
/// (web `resolveToolLayout`).
String resolveToolLayout(String tool) {
  final t = tool.toLowerCase();
  if (t == 'web_search' || t == 'websearch') return 'search';
  if (t == 'web_fetch' || t == 'webfetch' || t == 'fetch') return 'fetch';
  if (t == 'bash' || t == 'shell' || t == 'terminal') return 'shell';
  if (const [
    'read',
    'write',
    'edit',
    'multiedit',
    'apply_patch',
    'str_replace',
    'readfile',
    'writefile',
    'view',
    'create',
    'new_file',
  ].contains(t)) {
    return 'file';
  }
  if (const ['glob', 'grep', 'find', 'search', 'ls', 'ripgrep'].contains(t)) {
    return 'find';
  }
  // A skill load injects a whole instruction document; rendering it in the
  // transcript buries the conversation under the manual. The name is the only
  // part a reader needs.
  if (t == 'skill') return 'skill';
  if (t == 'task' || t == 'agent') return 'agent';
  // A question is worth reading back as the exchange it was.
  if (t == 'question') return 'question';
  return 'generic';
}

/// The thing a call was aimed at — the command, the path, the query
/// (web `toolTarget`). One line, for the row's own summary.
String toolTarget(ToolPart part) {
  final t = part.tool.toLowerCase();
  String pick(List<String> keys) {
    final input = part.input;
    if (input is Map<String, dynamic>) {
      for (final key in keys) {
        final value = input[key];
        if (value is String && value.trim().isNotEmpty) return value.trim();
      }
    }
    return '';
  }

  final title = part.title?.trim() ?? '';
  if (t == 'bash' || t == 'shell' || t == 'terminal') {
    final command = pick(const ['command']);
    return command.isEmpty ? part.tool : command;
  }
  if (const ['read', 'edit', 'write', 'multiedit', 'readfile', 'writefile', 'view']
      .contains(t)) {
    final path = pick(const ['file_path', 'path']);
    return path.isNotEmpty ? path : (title.isNotEmpty ? title : part.tool);
  }
  if (const ['glob', 'grep', 'search', 'find'].contains(t)) {
    final pattern = pick(const ['pattern', 'query']);
    return pattern.isNotEmpty ? pattern : (title.isNotEmpty ? title : part.tool);
  }
  if (t == 'web_search' || t == 'websearch') {
    final query = pick(const ['query']);
    return query.isNotEmpty ? query : (title.isNotEmpty ? title : part.tool);
  }
  if (t == 'web_fetch' || t == 'webfetch' || t == 'fetch') {
    final url = pick(const ['url']);
    return url.isNotEmpty ? url : (title.isNotEmpty ? title : part.tool);
  }
  if (title.isNotEmpty) return title;
  final description = pick(const ['description']);
  return description.isNotEmpty ? description : part.tool;
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
