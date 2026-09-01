import 'dart:io';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:path_provider/path_provider.dart';

import '../../../shared/api/auth_store.dart';
import '../../../shared/api/providers.dart';
import '../../../shared/models/json.dart';
import '../../../shared/models/session.dart';
import '../../../shared/models/skill.dart';

/// A catalogue write can commit its configuration while the follow-up MCP
/// connection fails.  The backend reports that as HTTP 200 with an installed
/// entry whose status is `error`, so transport success alone is not success for
/// the person waiting in the install sheet.
class McpCatalogInstallException implements Exception {
  const McpCatalogInstallException(this.message);

  final String message;

  @override
  String toString() => message;
}

/// Skill-centre transport + providers (web `features/skills-center/api/*`).
/// Screens never talk to Dio directly (mirrors the web §7 rule).
class SkillsApi {
  SkillsApi(this._dio);

  final Dio _dio;

  Future<List<InstalledSkill>> listSkills() async {
    final resp = await _dio.get<List<dynamic>>('/api/agent/skill');
    return [
      for (final item in resp.data ?? const [])
        if (item is Map<String, dynamic>) InstalledSkill.fromJson(item),
    ];
  }

  Future<List<McpServer>> listServers() async {
    final resp = await _dio.get<List<dynamic>>('/api/agent/mcp');
    return [
      for (final item in resp.data ?? const [])
        if (item is Map<String, dynamic>) McpServer.fromJson(item),
    ];
  }

  Future<Catalog> catalog() async {
    final resp = await _dio.get<Map<String, dynamic>>('/api/agent/catalog');
    return Catalog.fromJson(resp.data ?? const {});
  }

  /// Projects offered by the chat-creation sheet. Its own fetch, so this
  /// feature does not reach sideways into workspace's private API layer.
  Future<List<(String, String)>> listProjects() async {
    final resp = await _dio.get<List<dynamic>>('/api/agent/project');
    return [
      for (final item in resp.data ?? const [])
        if (item is Map<String, dynamic>)
          (
            asString(item['id']) ?? '',
            (asString(item['name'])?.trim().isNotEmpty ?? false)
                ? asString(item['name'])!.trim()
                : asString(item['id']) ?? '',
          ),
    ];
  }

  Future<void> installFromCatalog({
    required String id,
    required String kind,
    List<String> withMcp = const [],
    Map<String, Map<String, String>> env = const {},
  }) async {
    final response = await _dio.post<Map<String, dynamic>>(
      '/api/agent/catalog/install',
      data: {'id': id, 'kind': kind, 'with_mcp': withMcp, 'env': env},
    );

    final failures = <String>[];
    for (final raw in asList(response.data?['installed'])) {
      final installed = asMap(raw);
      if (installed['kind'] != 'mcp' || installed['status'] != 'error') {
        continue;
      }
      final rawName = (asString(installed['name']) ?? 'unknown').trim();
      final name = rawName.length > 120
          ? '${rawName.substring(0, 120)}…'
          : rawName;
      final rawReason = (asString(installed['error']) ?? '').trim();
      final reason = rawReason.length > 500
          ? '${rawReason.substring(0, 500)}…'
          : rawReason;
      failures.add(
        reason.isEmpty
            ? "MCP server '$name' failed to connect."
            : "MCP server '$name' failed to connect: $reason",
      );
    }
    if (failures.isNotEmpty) {
      throw McpCatalogInstallException(failures.join('\n'));
    }
  }

  /// Configure and connect are separate calls; a server that is configured
  /// but never connected contributes no tools, which reads as a broken
  /// install rather than an unfinished one.
  Future<void> addServer(String name, McpConfig config) async {
    await _dio.post<dynamic>(
      '/api/agent/mcp',
      data: {'name': name, ...config.toJson()},
    );
    await connectServer(name);
  }

  Future<void> removeServer(String name) async {
    await _dio.delete<dynamic>('/api/agent/mcp/${Uri.encodeComponent(name)}');
  }

  Future<void> connectServer(String name) async {
    await _dio.post<dynamic>(
      '/api/agent/mcp/${Uri.encodeComponent(name)}/connect',
    );
  }

  Future<void> disconnectServer(String name) async {
    await _dio.post<dynamic>(
      '/api/agent/mcp/${Uri.encodeComponent(name)}/disconnect',
    );
  }

  Future<void> uninstallSkill(String name) async {
    await _dio.delete<dynamic>('/api/agent/skill/${Uri.encodeComponent(name)}');
  }

  Future<void> publishSkill(String installDir) async {
    await _dio.post<dynamic>(
      '/api/agent/skill/${Uri.encodeComponent(installDir)}/publish',
    );
  }

  /// Install a skill from a pasted SKILL.md or a git URL.
  Future<void> installSkill({String? url, String? name, String? content}) async {
    await _dio.post<dynamic>('/api/agent/skill/install', data: {
      'url': ?url,
      'name': ?name,
      'content': ?content,
    });
  }

  /// Install a skill from an archive picked off the device (zip/tar/tgz).
  Future<void> uploadArchive({
    required String filename,
    required List<int> bytes,
    String? name,
  }) async {
    final form = FormData.fromMap({
      'file': MultipartFile.fromBytes(bytes, filename: filename),
      'name': name ?? '',
    });
    // Dio sets its own multipart boundary from the FormData; the JSON content
    // type the client defaults to would leave the server unable to parse it.
    await _dio.post<dynamic>(
      '/api/agent/skill/upload',
      data: form,
      options: Options(contentType: Headers.multipartFormDataContentType),
    );
  }

  /// Save a personal skill's whole install directory next to the app's other
  /// documents, and return where it landed. A phone has no download tray, so
  /// the path is the only useful receipt.
  Future<String> downloadArchive(String installDir) async {
    final resp = await _dio.get<List<int>>(
      '/api/agent/skill/${Uri.encodeComponent(installDir)}/download',
      options: Options(responseType: ResponseType.bytes),
    );
    final name = _dispositionFilename(resp.headers.value('content-disposition')) ??
        '$installDir.zip';
    final dir = await getApplicationDocumentsDirectory();
    final file = File('${dir.path}/$name');
    await file.writeAsBytes(Uint8List.fromList(resp.data ?? const []));
    return file.path;
  }

  /// Create a real chat, seed it with the person's natural-language request,
  /// then hand the session back for navigation.
  Future<Session> createSkillChat({
    required String projectId,
    required String brief,
    required String prompt,
  }) async {
    final title = brief.length > 32 ? '${brief.substring(0, 32)}…' : brief;
    final created = await _dio.post<Map<String, dynamic>>(
      '/api/agent/session',
      data: {'project_id': projectId, 'agent': 'build', 'title': title},
    );
    final session = Session.fromJson(created.data ?? const {});
    await _dio.post<dynamic>(
      '/api/agent/session/${session.id}/prompt_async',
      data: {
        'text': prompt,
        'agent': 'build',
        'client_message_id': 'skill-create-${session.id}',
      },
    );
    return session;
  }
}

String? _dispositionFilename(String? value) {
  if (value == null) return null;
  final encoded =
      RegExp(r"filename\*=UTF-8''([^;]+)", caseSensitive: false).firstMatch(value);
  if (encoded != null) {
    final raw = encoded.group(1)!;
    try {
      return Uri.decodeComponent(raw);
    } catch (_) {
      return raw;
    }
  }
  final quoted =
      RegExp(r'filename="([^"]+)"', caseSensitive: false).firstMatch(value);
  if (quoted != null) return quoted.group(1);
  final bare = RegExp(r'filename=([^;]+)', caseSensitive: false).firstMatch(value);
  return bare?.group(1)?.trim();
}

final skillsApiProvider =
    Provider<SkillsApi>((ref) => SkillsApi(ref.watch(apiDioProvider)));

/// The account whose Skill Centre state the current widget tree may consume.
/// Keeping this as a provider makes the auth dependency explicit and gives
/// tests one production selector to exercise during A -> signed-out -> B.
final skillsAccountProvider = Provider<String?>((ref) {
  return ref.watch(authProvider.select((state) => state.user?.id));
});

/// Bumped after every write — an install moves more than one list, so all three
/// refetch together. Revisions are account-scoped so a write by B never revives
/// or mutates a retained A cache entry.
final skillsRevisionProvider = StateProvider.autoDispose.family<int, String?>(
  (ref, userId) => 0,
);

void bumpSkills(WidgetRef ref) {
  final userId = ref.read(skillsAccountProvider);
  if (userId == null) return;
  ref.read(skillsRevisionProvider(userId).notifier).state++;
}

/// Every server-backed cache is a family keyed by the authenticated account.
/// On A -> B, the screen watches B's fresh AsyncLoading rather than an async
/// refresh of A's provider (which Riverpod may otherwise retain as previous
/// data). autoDispose also gives route remounts a real refetch.
final installedSkillsProvider = FutureProvider.autoDispose
    .family<List<InstalledSkill>, String?>((ref, userId) async {
      ref.watch(skillsRevisionProvider(userId));
      if (userId == null) return const <InstalledSkill>[];
      return ref.watch(skillsApiProvider).listSkills();
    });

final mcpServersProvider = FutureProvider.autoDispose
    .family<List<McpServer>, String?>((ref, userId) async {
      ref.watch(skillsRevisionProvider(userId));
      if (userId == null) return const <McpServer>[];
      return ref.watch(skillsApiProvider).listServers();
    });

final skillCatalogProvider = FutureProvider.autoDispose
    .family<Catalog, String?>((ref, userId) async {
      ref.watch(skillsRevisionProvider(userId));
      if (userId == null) return const Catalog();
      return ref.watch(skillsApiProvider).catalog();
    });

final skillProjectsProvider = FutureProvider.autoDispose
    .family<List<(String, String)>, String?>((ref, userId) async {
      if (userId == null) return const <(String, String)>[];
      return ref.watch(skillsApiProvider).listProjects();
    });

/// One write at a time, and the last failure — shared by the screen and by
/// whichever sheet is open, because a modal route builds once and would
/// otherwise never see the screen's own state change.
final skillsBusyProvider = StateProvider.autoDispose.family<bool, String?>(
  (ref, userId) => false,
);

final skillsErrorProvider = StateProvider.autoDispose.family<String?, String?>(
  (ref, userId) => null,
);
