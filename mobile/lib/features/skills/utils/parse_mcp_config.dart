/// Parsing pasted MCP configuration — a 1:1 port of frontend-v2
/// `features/skills-center/lib/parse-mcp-config.ts`.
///
/// People arrive holding a snippet from a README, and those snippets are
/// written in whichever shape the tool that documented them uses. Rejecting
/// all but one would mean asking someone to retype a config they already
/// have, so every shape below maps onto the single {name, config} the backend
/// takes.
library;

import 'dart:convert';

import '../../../shared/models/skill.dart';

class ParsedMcpEntry {
  const ParsedMcpEntry({required this.name, required this.config});

  final String name;
  final McpConfig config;
}

class McpParseResult {
  const McpParseResult({this.entries = const [], this.error});

  final List<ParsedMcpEntry> entries;

  /// One of empty | invalidJson | invalidShape | noServers | needName, so the
  /// caller can look up `skills:upload.jsonError.<error>`.
  final String? error;
}

/// `url` alone is enough to know a server is remote, whatever it calls itself.
String _readTransport(Map<String, dynamic> raw) {
  final declared = '${raw['type'] ?? raw['transport'] ?? ''}'.toLowerCase();
  if (declared == 'stdio') return 'stdio';
  if (const ['remote', 'http', 'sse', 'streamable-http', 'streamablehttp']
      .contains(declared)) {
    return 'remote';
  }
  return raw['url'] != null ? 'remote' : 'stdio';
}

Map<String, String> _readStringMap(Object? value) {
  if (value is! Map) return const {};
  return {
    for (final entry in value.entries)
      if (entry.value != null) '${entry.key}': '${entry.value}',
  };
}

List<String> _readArgs(Object? value) =>
    value is List ? [for (final v in value) '$v'] : const [];

int _readTimeout(Object? value) {
  if (value is num && value > 0) return value.toInt();
  if (value is String) {
    final parsed = int.tryParse(value);
    if (parsed != null && parsed > 0) return parsed;
  }
  return 60;
}

ParsedMcpEntry? _readOne(String name, Object? raw) {
  if (raw is! Map<String, dynamic>) return null;
  final type = _readTransport(raw);

  if (type == 'remote') {
    final url = raw['url'] is String ? (raw['url'] as String).trim() : '';
    if (url.isEmpty) return null;
    return ParsedMcpEntry(
      name: name,
      config: McpConfig(
        type: 'remote',
        url: url,
        headers: _readStringMap(raw['headers']),
        timeout: _readTimeout(raw['timeout']),
      ),
    );
  }

  final command =
      raw['command'] is String ? (raw['command'] as String).trim() : '';
  if (command.isEmpty) return null;
  return ParsedMcpEntry(
    name: name,
    config: McpConfig(
      type: 'stdio',
      command: command,
      args: _readArgs(raw['args']),
      env: _readStringMap(raw['env']),
      timeout: _readTimeout(raw['timeout']),
    ),
  );
}

/// Read one or more servers out of pasted JSON.
///
/// Accepted shapes:
///   `{"mcpServers": {"name": {...}}}`  Claude Desktop / Cursor / Windsurf
///   `{"servers": {"name": {...}}}`     VS Code
///   `{"name": {...}}`                  a bare map of servers
///   `{"command": "npx", ...}`          a single server, named by [fallbackName]
McpParseResult parseMcpConfig(String text, {String fallbackName = ''}) {
  final trimmed = text.trim();
  if (trimmed.isEmpty) return const McpParseResult(error: 'empty');

  Object? data;
  try {
    data = jsonDecode(trimmed);
  } catch (_) {
    // Only after a straight parse fails: a snippet copied off a web page —
    // or typed on iOS, whose keyboard substitutes as you go — carries curly
    // quotes and en-dashes that no JSON parser accepts. Valid JSON is never
    // rewritten, so a string that legitimately contains “ ” survives.
    try {
      data = jsonDecode(_straightenPunctuation(trimmed));
    } catch (_) {
      return const McpParseResult(error: 'invalidJson');
    }
  }
  if (data is! Map<String, dynamic>) {
    return const McpParseResult(error: 'invalidShape');
  }

  final container = data['mcpServers'] ?? data['servers'] ?? data['mcp_servers'];
  if (container is Map<String, dynamic>) {
    final entries = <ParsedMcpEntry>[];
    for (final entry in container.entries) {
      final parsed = _readOne(entry.key, entry.value);
      if (parsed != null) entries.add(parsed);
    }
    return entries.isEmpty
        ? const McpParseResult(error: 'noServers')
        : McpParseResult(entries: entries);
  }

  // A single server object, recognised by carrying a transport field itself.
  if (data['command'] != null || data['url'] != null) {
    final declared = data['name'];
    final name = declared is String && declared.trim().isNotEmpty
        ? declared.trim()
        : fallbackName.trim();
    if (name.isEmpty) return const McpParseResult(error: 'needName');
    final parsed = _readOne(name, data);
    return parsed == null
        ? const McpParseResult(error: 'invalidShape')
        : McpParseResult(entries: [parsed]);
  }

  // A bare map whose values are server objects.
  final entries = <ParsedMcpEntry>[];
  for (final entry in data.entries) {
    final parsed = _readOne(entry.key, entry.value);
    if (parsed != null) entries.add(parsed);
  }
  return entries.isEmpty
      ? const McpParseResult(error: 'noServers')
      : McpParseResult(entries: entries);
}

const _typographic = {
  '\u201C': '"',
  '\u201D': '"',
  '\u2018': "'",
  '\u2019': "'",
  '\u2013': '-',
  '\u2014': '-',
};

String _straightenPunctuation(String text) {
  var out = text;
  _typographic.forEach((from, to) => out = out.replaceAll(from, to));
  return out;
}

/// "KEY=value" or "Key: value" per line — the shape people already have.
Map<String, String> parsePairs(String text) {
  final out = <String, String>{};
  for (final line in text.split('\n')) {
    final trimmed = line.trim();
    if (trimmed.isEmpty || trimmed.startsWith('#')) continue;
    final eq = trimmed.indexOf('=');
    final colon = trimmed.indexOf(':');
    final at = eq == -1
        ? colon
        : colon == -1
            ? eq
            : (eq < colon ? eq : colon);
    if (at <= 0) continue;
    out[trimmed.substring(0, at).trim()] = trimmed.substring(at + 1).trim();
  }
  return out;
}
