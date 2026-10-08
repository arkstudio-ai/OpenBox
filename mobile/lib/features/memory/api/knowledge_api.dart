import 'dart:convert';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/providers.dart';
import '../../../shared/api/workspace_scope.dart';
import '../../../shared/models/json.dart';
import '../models/memory_models.dart';
import '../models/wiki_models.dart';

/// Transport for the knowledge page: memories, topics and files. The same
/// endpoints and bodies as the web (`shared/api/memory.ts`,
/// `features/memory/wiki-api.ts`, `features/memory/wiki/documents-api.ts`).
/// Reads never call a model or write anything.
class KnowledgeApi {
  KnowledgeApi(this._dio);

  final Dio _dio;

  static String _memory(String id) =>
      '/api/memories/${Uri.encodeComponent(id)}';
  static String _page(String id) =>
      '/api/memory-wiki/pages/${Uri.encodeComponent(id)}';
  static String _document(String id) =>
      '/api/memory-documents/${Uri.encodeComponent(id)}';

  Map<String, dynamic> _map(Response<dynamic> resp) => asMap(resp.data);

  // ------------------------------------------------------------ memories

  /// Newest first, a page at a time; `query` narrows to memories containing it.
  Future<Paged<MemoryRecord>> listMemories({
    required String projectId,
    String query = '',
    int offset = 0,
  }) async {
    final resp = await _dio.get<dynamic>(
      '/api/memories',
      queryParameters: {
        'limit': '100',
        'status': 'ACTIVE',
        if (projectId.isNotEmpty) 'project_id': projectId,
        if (query.isNotEmpty) 'query': query,
        if (offset > 0) 'offset': '$offset',
      },
    );
    final data = _map(resp);
    return Paged([
      for (final row in asList(data['memories']))
        if (row is Map<String, dynamic>) MemoryRecord.fromJson(row),
    ], nextOffset: asInt(data['next_offset']));
  }

  Future<MemoryProcessing> processing(String projectId) async {
    final resp = await _dio.get<dynamic>(
      '/api/memories/processing',
      queryParameters: {if (projectId.isNotEmpty) 'project_id': projectId},
    );
    return MemoryProcessing.fromJson(_map(resp));
  }

  Future<void> retryTurn(String id) => _dio.post<dynamic>(
    '/api/memories/processing/${Uri.encodeComponent(id)}/retry',
  );

  Future<void> dismissTurn(String id) => _dio.post<dynamic>(
    '/api/memories/processing/${Uri.encodeComponent(id)}/dismiss',
  );

  Future<MemoryRecord> create(
    String summary,
    String projectId,
    String requestId,
  ) async {
    final resp = await _dio.post<dynamic>(
      '/api/memories',
      data: {
        'summary': summary,
        'project_id': projectId.isEmpty ? null : projectId,
        'request_id': requestId,
      },
    );
    return MemoryRecord.fromJson(_map(resp));
  }

  /// Rewords a memory, naming the revision on screen.
  Future<MemoryRecord> correct(
    MemoryRecord memory,
    String summary,
    String requestId,
  ) async {
    final resp = await _dio.patch<dynamic>(
      _memory(memory.id),
      data: {
        'summary': summary,
        'expected_revision': memory.revision,
        'request_id': requestId,
      },
    );
    return MemoryRecord.fromJson(_map(resp));
  }

  /// Forgets one memory, and with [sourceIds] also the wording it was
  /// learned from — never a chat, a session or a project.
  Future<void> forget(
    MemoryRecord memory,
    String requestId, {
    List<String>? sourceIds,
  }) => _dio.post<dynamic>(
    '${_memory(memory.id)}/forget',
    data: {
      'expected_revision': memory.revision,
      'request_id': requestId,
      'mode': sourceIds == null ? 'memory' : 'sources',
      'source_ids': sourceIds ?? const <String>[],
    },
  );

  /// The memory as it is now, re-authorized: text only while its sources
  /// allow it.
  Future<MemoryRecord> memory(String id) async =>
      MemoryRecord.fromJson(_map(await _dio.get<dynamic>(_memory(id))));

  Future<List<MemoryRevision>> history(String id) async {
    final data = _map(await _dio.get<dynamic>('${_memory(id)}/history'));
    return [
      for (final row in asList(data['revisions']))
        if (row is Map<String, dynamic>) MemoryRevision.fromJson(row),
    ];
  }

  Future<List<MemorySource>> sources(String id) async {
    final data = _map(await _dio.get<dynamic>('${_memory(id)}/sources'));
    return [
      for (final row in asList(data['sources']))
        if (row is Map<String, dynamic>) MemorySource.fromJson(row),
    ];
  }

  Future<MemoryCleanup> cleanup(String id) async => MemoryCleanup.fromJson(
    _map(await _dio.get<dynamic>('${_memory(id)}/cleanup')),
  );

  Future<MemorySettings> settings() async => MemorySettings.fromJson(
    _map(await _dio.get<dynamic>('/api/memories/settings')),
  );

  Future<MemorySettings> setAutoSave(bool autoSave) async {
    final resp = await _dio.put<dynamic>(
      '/api/memories/settings',
      data: {'auto_save': autoSave},
    );
    return MemorySettings.fromJson(_map(resp));
  }

  /// Everything remembered, as a Markdown file to keep.
  Future<Uint8List> exportAll(String language) async {
    final resp = await _dio.get<List<int>>(
      '/api/memories/export',
      queryParameters: {'lang': language},
      options: Options(responseType: ResponseType.bytes),
    );
    return Uint8List.fromList(resp.data ?? const []);
  }

  /// Forgets every memory in one project, or everywhere when none is given.
  Future<int> forgetAll(String projectId) async {
    final resp = await _dio.post<dynamic>(
      '/api/memories/forget-all',
      data: {
        'project_id': projectId.isEmpty ? null : projectId,
        'confirm': 'forget-all',
      },
    );
    return asInt(_map(resp)['forgotten']) ?? 0;
  }

  /// The scope options. Its own fetch — features never import each other.
  Future<List<KnowledgeProject>> projects() async {
    final resp = await _dio.get<dynamic>('/api/agent/project');
    return [
      for (final item in asList(resp.data))
        if (item is Map<String, dynamic> && asString(item['id']) != null)
          KnowledgeProject(
            id: asString(item['id'])!,
            name: asString(item['name']) ?? '',
          ),
    ];
  }

  // ------------------------------------------------------------- topics

  /// Whether this account may organize and upload files.
  Future<bool> uploadsEnabled() async {
    final data = _map(await _dio.get<dynamic>('/api/memory-wiki/capabilities'));
    return asBool(data['enabled']) ?? false;
  }

  Future<Paged<WikiSummary>> library({
    required String projectId,
    required String query,
    int offset = 0,
  }) async {
    final resp = await _dio.get<dynamic>(
      '/api/memory-wiki/library',
      queryParameters: {
        'query': query,
        'status': 'all',
        'offset': '$offset',
        if (projectId.isNotEmpty) 'project_id': projectId,
      },
    );
    final data = _map(resp);
    return Paged([
      for (final row in asList(data['pages']))
        if (row is Map<String, dynamic>) WikiSummary.fromJson(row),
    ], nextOffset: asInt(data['next_offset']));
  }

  Future<List<MemoryTopic>> memoryGroups(String projectId) async {
    final resp = await _dio.get<dynamic>(
      '/api/memory-wiki/memory-groups',
      queryParameters: {if (projectId.isNotEmpty) 'project_id': projectId},
    );
    return [
      for (final row in asList(_map(resp)['groups']))
        if (row is Map<String, dynamic>) MemoryTopic.fromJson(row),
    ];
  }

  Future<WikiPage> page(String id) async =>
      WikiPage.fromJson(_map(await _dio.get<dynamic>(_page(id))));

  Future<WikiEditSnapshot> editSnapshot(String id) async =>
      WikiEditSnapshot.fromJson(
        _map(await _dio.get<dynamic>('${_page(id)}/edit')),
      );

  /// Saves the title and text against the revision first shown.
  Future<void> edit(
    WikiEditSnapshot base,
    String title,
    List<WikiEditEntry> entries,
    String requestId,
  ) => _dio.post<dynamic>(
    '${_page(base.id)}/edit',
    data: {
      'expected_revision': base.revision,
      'content_hash': base.contentHash,
      'title': title,
      'entries': [
        for (final entry in entries)
          {'id': entry.id, 'revision': entry.revision, 'text': entry.text},
      ],
      'request_id': requestId,
    },
  );

  // -------------------------------------------------------------- files

  Future<Paged<KnowledgeDocument>> documents({
    required String projectId,
    int offset = 0,
  }) async {
    final resp = await _dio.get<dynamic>(
      '/api/memory-documents',
      queryParameters: {
        'offset': '$offset',
        if (projectId.isNotEmpty) 'project_id': projectId,
      },
    );
    final data = _map(resp);
    return Paged(
      [
        for (final row in asList(data['documents']))
          if (row is Map<String, dynamic>) KnowledgeDocument.fromJson(row),
      ],
      nextOffset: asInt(data['next_offset']),
      cleanupPending: asInt(data['cleanup_pending']) ?? 0,
    );
  }

  /// Sends one file to the scope in view. [userId] and [workspaceId] pin the
  /// request to the identity that started the upload, so a file queued
  /// behind a workspace switch is refused instead of landing in the new one.
  Future<KnowledgeDocument> upload({
    required String filename,
    required List<int> bytes,
    required String projectId,
    String? userId,
    String? workspaceId,
  }) async {
    final resp = await _dio.post<dynamic>(
      '/api/memory-documents',
      data: FormData.fromMap({
        'file': MultipartFile.fromBytes(bytes, filename: filename),
        if (projectId.isNotEmpty) 'project_id': projectId,
      }),
      options: Options(
        contentType: Headers.multipartFormDataContentType,
        headers: {'X-Workspace-Id': ?workspaceId},
        extra: {
          if (userId != null) ...{
            requestScopeUserKey: userId,
            requestScopeWorkspaceKey: workspaceId,
          },
        },
      ),
    );
    return KnowledgeDocument.fromJson(_map(resp));
  }

  Future<void> retryDocument(String id) =>
      _dio.post<dynamic>('${_document(id)}/retry');

  /// Removes the file and everything built from it; chats are untouched.
  /// True while the original is still being removed from storage.
  Future<bool> removeDocument(String id) async {
    final resp = await _dio.delete<dynamic>(_document(id));
    return asString(_map(resp)['original_cleanup']) == 'pending';
  }

  /// The original file, and the name the server gives it.
  Future<({Uint8List bytes, String? filename})> original(String id) async {
    final resp = await _dio.get<List<int>>(
      '${_document(id)}/original',
      options: Options(responseType: ResponseType.bytes),
    );
    return (
      bytes: Uint8List.fromList(resp.data ?? const []),
      filename: dispositionFilename(resp.headers.value('content-disposition')),
    );
  }
}

/// The filename a `Content-Disposition` header names, if any.
String? dispositionFilename(String? value) {
  if (value == null) return null;
  final encoded = RegExp(
    r"filename\*=UTF-8''([^;]+)",
    caseSensitive: false,
  ).firstMatch(value);
  if (encoded != null) {
    try {
      return Uri.decodeComponent(encoded.group(1)!);
    } on ArgumentError {
      return encoded.group(1);
    }
  }
  return RegExp(
    r'filename="?([^";]+)"?',
    caseSensitive: false,
  ).firstMatch(value)?.group(1)?.trim();
}

/// Bytes of a UTF-8 text export.
Uint8List utf8Bytes(String text) => Uint8List.fromList(utf8.encode(text));

final knowledgeApiProvider = Provider<KnowledgeApi>(
  (ref) => KnowledgeApi(ref.watch(apiDioProvider)),
);
