import 'dart:async';

import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/providers.dart';
import '../../../shared/models/json.dart';
import '../../../shared/models/resource.dart';

/// Resource-centre transport + providers (web `features/resources/api/*`).
/// Everything here talks to `/api/assets` — the centre is a view over object
/// storage, not over a sandbox directory, so its files outlive any container.
class ResourcesApi {
  ResourcesApi(this._dio);

  final Dio _dio;

  Future<ResourcePage> list(ResourceQuery query) async {
    final resp = await _dio.get<Map<String, dynamic>>(
      '/api/assets',
      queryParameters: query.toQueryParameters(),
    );
    return ResourcePage.fromJson(resp.data ?? const {});
  }

  Future<void> rename(String id, String name) async {
    await _dio.patch<dynamic>('/api/assets/$id', data: {'name': name});
  }

  Future<void> delete(String id) async {
    await _dio.delete<dynamic>('/api/assets/$id');
  }

  /// Text body for the preview pane. The bucket refuses a cross-origin read,
  /// so this one payload comes back through the API (capped server-side).
  Future<String> text(String id) async {
    final resp = await _dio.get<Map<String, dynamic>>('/api/assets/$id/text');
    return asString(resp.data?['text']) ?? '';
  }

  /// Freshly signed URL carrying `content-disposition: attachment`. The
  /// disposition is part of the signature, so it cannot be appended to a URL
  /// already signed for inline viewing.
  Future<String?> downloadUrl(String id) async {
    final resp = await _dio.get<Map<String, dynamic>>(
      '/api/assets/$id/url',
      queryParameters: {'download': true},
    );
    return asString(resp.data?['url']);
  }

  /// Open an upload, PUT the bytes straight to OSS, then have the backend
  /// verify the object landed. The bytes never pass through the API.
  Future<Resource> upload({
    required String name,
    required String mime,
    required List<int> bytes,
    String? projectId,
    void Function(double fraction)? onProgress,
  }) async {
    final ticket = await _dio.post<Map<String, dynamic>>(
      '/api/assets',
      data: {
        'name': name,
        'mime': mime,
        'size': bytes.length,
        'project_id': ?projectId,
      },
    );
    final data = ticket.data ?? const <String, dynamic>{};
    final putUrl = asString(data['putUrl']) ?? '';
    final id = asString(data['id']) ?? '';
    final headers = asMap(data['headers']);
    // A bare Dio instance: the API interceptors would attach our bearer token
    // to an OSS request, and the signature covers exactly these headers.
    await Dio().put<dynamic>(
      putUrl,
      data: Stream.fromIterable([bytes]),
      options: Options(
        headers: {
          for (final entry in headers.entries) entry.key: entry.value,
          Headers.contentLengthHeader: bytes.length,
        },
      ),
      onSendProgress: (sent, total) {
        if (total > 0) onProgress?.call(sent / total);
      },
    );
    final done =
        await _dio.post<Map<String, dynamic>>('/api/assets/$id/complete');
    return Resource.fromJson(done.data ?? const {});
  }

  /// Project options. Its own fetch — features never import each other.
  Future<List<(String, String)>> listProjects() async {
    final resp = await _dio.get<List<dynamic>>('/api/agent/project');
    return [
      for (final item in (resp.data ?? const []))
        if (item is Map<String, dynamic>)
          (
            asString(item['id']) ?? '',
            (asString(item['name'])?.trim().isNotEmpty ?? false)
                ? asString(item['name'])!.trim()
                : asString(item['id']) ?? '',
          ),
    ];
  }
}

final resourcesApiProvider =
    Provider<ResourcesApi>((ref) => ResourcesApi(ref.watch(apiDioProvider)));

/// Bumped after every write so every open listing refetches.
final resourcesRevisionProvider = StateProvider<int>((ref) => 0);

void bumpResources(WidgetRef ref) =>
    ref.read(resourcesRevisionProvider.notifier).state++;

final resourceListProvider =
    FutureProvider.family<ResourcePage, ResourceQuery>((ref, query) {
  ref.watch(resourcesRevisionProvider);
  return ref.watch(resourcesApiProvider).list(query);
});

final resourceProjectsProvider = FutureProvider<List<(String, String)>>(
  (ref) => ref.watch(resourcesApiProvider).listProjects(),
);

final resourceTextProvider =
    FutureProvider.family<String, String>((ref, id) async {
  ref.keepAlive();
  return ref.watch(resourcesApiProvider).text(id);
});
