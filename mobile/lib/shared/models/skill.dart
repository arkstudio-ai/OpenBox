/// Skill-centre domain types, mirroring frontend-v2
/// `features/skills-center/types/index.ts`. Field names follow the backend
/// payloads (`backend/api/metadata.py`, `backend/skill/catalog.py`), so a
/// field is searchable across both sides.
library;

import 'json.dart';

/// An MCP server's transport. `stdio` runs a local process; `remote` is HTTP.
class McpConfig {
  const McpConfig({
    required this.type,
    this.command,
    this.args = const [],
    this.env = const {},
    this.url,
    this.headers = const {},
    this.timeout = 60,
  });

  final String type; // stdio | remote
  final String? command;
  final List<String> args;
  final Map<String, String> env;
  final String? url;
  final Map<String, String> headers;
  final int timeout;

  factory McpConfig.fromJson(Map<String, dynamic> json) => McpConfig(
        type: asString(json['type']) ?? 'stdio',
        command: asString(json['command']),
        args: [for (final a in asList(json['args'])) '$a'],
        env: {
          for (final e in asMap(json['env']).entries) e.key: '${e.value}',
        },
        url: asString(json['url']),
        headers: {
          for (final e in asMap(json['headers']).entries) e.key: '${e.value}',
        },
        timeout: asInt(json['timeout']) ?? 60,
      );

  Map<String, dynamic> toJson() => {
        'type': type,
        if (type == 'stdio') ...{
          'command': ?command,
          'args': args,
          'env': env,
        } else ...{
          'url': ?url,
          'headers': headers,
        },
        'timeout': timeout,
      };
}

class McpTool {
  const McpTool({required this.name, this.description});

  factory McpTool.fromJson(Map<String, dynamic> json) => McpTool(
        name: asString(json['name']) ?? '',
        description: asString(json['description']),
      );

  final String name;
  final String? description;
}

/// An MCP server as the container reports it.
class McpServer {
  const McpServer({
    required this.name,
    required this.type,
    required this.status,
    this.tools = const [],
    this.error,
    this.command,
    this.args = const [],
    this.url,
  });

  factory McpServer.fromJson(Map<String, dynamic> json) => McpServer(
        name: asString(json['name']) ?? '',
        type: asString(json['type']) ?? '',
        status: asString(json['status']) ?? 'disconnected',
        tools: asList(json['tools'])
            .whereType<Map<String, dynamic>>()
            .map(McpTool.fromJson)
            .toList(),
        error: asString(json['error']),
        command: asString(json['command']),
        args: [for (final a in asList(json['args'])) '$a'],
        url: asString(json['url']),
      );

  final String name;
  final String type;

  /// connected | disconnected | error
  final String status;
  final List<McpTool> tools;
  final String? error;
  final String? command;
  final List<String> args;
  final String? url;

  bool get isConnected => status == 'connected';

  /// What the row shows under the name: the endpoint, or the argv.
  String get subtitle {
    final remote = url;
    if (remote != null && remote.isNotEmpty) return remote;
    return [?command, ...args].join(' ');
  }
}

/// An installed skill. `icon`/`requires_mcp` come from SKILL.md frontmatter.
class InstalledSkill {
  const InstalledSkill({
    required this.name,
    this.description,
    this.icon,
    this.requiresMcp = const [],
    this.homepage,
    this.source,
    this.installDir,
    this.category,
    this.publicationStatus,
    this.libraryId,
    this.catalogId,
    this.publishedAt,
  });

  factory InstalledSkill.fromJson(Map<String, dynamic> json) => InstalledSkill(
        name: asString(json['name']) ?? '',
        description: asString(json['description']),
        icon: asString(json['icon']),
        requiresMcp: [for (final m in asList(json['requires_mcp'])) '$m'],
        homepage: asString(json['homepage']),
        source: asString(json['source']),
        installDir: asString(json['install_dir']),
        category: asString(json['category']),
        publicationStatus: asString(json['publication_status']),
        libraryId: asString(json['library_id']),
        catalogId: asString(json['catalog_id']),
        publishedAt: asString(json['published_at']),
      );

  final String name;
  final String? description;
  final String? icon;
  final List<String> requiresMcp;
  final String? homepage;

  /// "container" for user installs, "builtin" for image-baked,
  /// "global"/"project" for host.
  final String? source;
  final String? installDir;

  /// Product-facing origin. Unlike [source], this distinguishes a user's own
  /// work from something they installed from the public store.
  final String? category; // personal | store | installed | builtin | host

  /// Only personal skills can be published; built-in/store installs are null.
  final String? publicationStatus; // unpublished | published
  final String? libraryId;
  final String? catalogId;
  final String? publishedAt;
}

class CatalogEnvField {
  const CatalogEnvField({
    required this.key,
    required this.label,
    this.secret = false,
  });

  factory CatalogEnvField.fromJson(Map<String, dynamic> json) =>
      CatalogEnvField(
        key: asString(json['key']) ?? '',
        label: asString(json['label']) ?? asString(json['key']) ?? '',
        secret: asBool(json['secret']) ?? false,
      );

  final String key;
  final String label;
  final bool secret;
}

/// A store entry — either half of the catalogue reads as the same row.
class CatalogEntry {
  const CatalogEntry({
    required this.kind,
    required this.id,
    required this.name,
    required this.title,
    required this.icon,
    required this.description,
    required this.installed,
    this.publisher,
    this.homepage,
    this.tags = const [],
    this.community = false,
    this.requiresMcp = const [],
    this.missingMcp = const [],
    this.config,
    this.requiredEnv = const [],
  });

  factory CatalogEntry.fromJson(Map<String, dynamic> json, String kind) =>
      CatalogEntry(
        kind: kind,
        id: asString(json['id']) ?? '',
        name: asString(json['name']) ?? '',
        title: asString(json['title']) ?? asString(json['name']) ?? '',
        icon: asString(json['icon']) ?? '',
        description: asString(json['description']) ?? '',
        installed: asBool(json['installed']) ?? false,
        publisher: asString(json['publisher']),
        homepage: asString(json['homepage']),
        tags: [for (final t in asList(json['tags'])) '$t'],
        community: asBool(json['community']) ?? false,
        requiresMcp: [for (final m in asList(json['requires_mcp'])) '$m'],
        missingMcp: [for (final m in asList(json['missing_mcp'])) '$m'],
        config: json['config'] is Map<String, dynamic>
            ? McpConfig.fromJson(json['config'] as Map<String, dynamic>)
            : null,
        requiredEnv: asList(json['required_env'])
            .whereType<Map<String, dynamic>>()
            .map(CatalogEnvField.fromJson)
            .toList(),
      );

  final String kind; // skill | mcp
  final String id;
  final String name;
  final String title;
  final String icon;
  final String description;
  final bool installed;
  final String? publisher;
  final String? homepage;
  final List<String> tags;

  /// Published by a user rather than maintained in the operator catalogue.
  final bool community;
  final List<String> requiresMcp;

  /// Dependencies not yet installed — resolved server-side so both tabs agree.
  final List<String> missingMcp;
  final McpConfig? config;
  final List<CatalogEnvField> requiredEnv;
}

class Catalog {
  const Catalog({this.skills = const [], this.mcp = const []});

  factory Catalog.fromJson(Map<String, dynamic> json) => Catalog(
        skills: asList(json['skills'])
            .whereType<Map<String, dynamic>>()
            .map((e) => CatalogEntry.fromJson(e, 'skill'))
            .toList(),
        mcp: asList(json['mcp'])
            .whereType<Map<String, dynamic>>()
            .map((e) => CatalogEntry.fromJson(e, 'mcp'))
            .toList(),
      );

  final List<CatalogEntry> skills;
  final List<CatalogEntry> mcp;
}
