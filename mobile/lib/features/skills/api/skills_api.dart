import 'dart:io';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:path_provider/path_provider.dart';

import '../../../shared/api/providers.dart';
import '../../../shared/models/json.dart';
import '../../../shared/models/session.dart';
import '../../../shared/models/skill.dart';

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
    await _dio.post<dynamic>('/api/agent/catalog/install', data: {
      'id': id,
      'kind': kind,
      'with_mcp': withMcp,
      'env': env,
    });
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

/// Bumped after every write — an install moves more than one list, so all
/// three refetch together (web `useRefreshAll`).
final skillsRevisionProvider = StateProvider<int>((ref) => 0);

void bumpSkills(WidgetRef ref) =>
    ref.read(skillsRevisionProvider.notifier).state++;

/// A skill can be created by an agent while this route is unmounted, so the
/// list always refetches rather than serving a stale cache.
final installedSkillsProvider = FutureProvider<List<InstalledSkill>>((ref) {
  ref.watch(skillsRevisionProvider);
  return ref.watch(skillsApiProvider).listSkills();
});

final mcpServersProvider = FutureProvider<List<McpServer>>((ref) {
  ref.watch(skillsRevisionProvider);
  return ref.watch(skillsApiProvider).listServers();
});

final skillCatalogProvider = FutureProvider<Catalog>((ref) {
  ref.watch(skillsRevisionProvider);
  return ref.watch(skillsApiProvider).catalog();
});

final skillProjectsProvider = FutureProvider<List<(String, String)>>(
  (ref) => ref.watch(skillsApiProvider).listProjects(),
);

/// One write at a time, and the last failure — shared by the screen and by
/// whichever sheet is open, because a modal route builds once and would
/// otherwise never see the screen's own state change.
final skillsBusyProvider = StateProvider<bool>((ref) => false);

final skillsErrorProvider = StateProvider<String?>((ref) => null);
