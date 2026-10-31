import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/providers.dart';
import '../../../shared/models/first_video.dart';
import '../../../shared/models/json.dart';

/// Transport for the guided first video (backend `api/first_video.py`).
/// Every call carries the usual bearer + `X-Workspace-Id`; the server scopes
/// the one free job to that workspace.
class FirstVideoApi {
  /// [uploader] carries the bytes to object storage: a bare client, because
  /// the API interceptors would attach our bearer token to the bucket
  /// request and the signature covers exactly the ticket's headers.
  FirstVideoApi(this._dio, {Dio? uploader}) : _uploader = uploader ?? Dio();

  final Dio _dio;
  final Dio _uploader;

  /// Idempotent: a second POST for the same workspace returns the same job.
  /// The server also copies industry / shop name / headline into the
  /// assistant profile, so the app never calls the profile route itself.
  Future<FirstVideoJob> submit({
    required String template,
    String industry = 'other',
    String? shopName,
    String? headline,
    String? assetId,
  }) async {
    final resp = await _dio.post<Map<String, dynamic>>(
      '/api/first-video',
      data: {
        'industry': industry,
        'shop_name': _clean(shopName),
        'headline': _clean(headline),
        'template': template,
        'asset_id': assetId,
      },
    );
    return FirstVideoJob.fromJson(resp.data ?? const {});
  }

  Future<FirstVideoJob> status() async {
    final resp = await _dio.get<Map<String, dynamic>>('/api/first-video');
    return FirstVideoJob.fromJson(resp.data ?? const {});
  }

  Future<FirstVideoCatalog> catalog() async {
    final resp = await _dio.get<Map<String, dynamic>>(
      '/api/first-video/templates',
    );
    return FirstVideoCatalog.fromJson(resp.data ?? const {});
  }

  /// Presigned GET for the finished video — the same route the chat gallery
  /// plays assets through (`GET /api/assets/{id}/url`), fetched here because
  /// features never import each other.
  Future<String?> assetUrl(String assetId) async {
    final resp = await _dio.get<Map<String, dynamic>>(
      '/api/assets/${Uri.encodeComponent(assetId)}/url',
    );
    return asString(resp.data?['url']);
  }

  /// The reference photo, uploaded the way the resource centre does it:
  /// open an upload, PUT the bytes straight to object storage, then have
  /// the backend verify the object landed. Returns the completed asset id.
  Future<String> uploadPhoto({
    required String name,
    required String mime,
    required List<int> bytes,
    void Function(double fraction)? onProgress,
  }) async {
    final ticket = await _dio.post<Map<String, dynamic>>(
      '/api/assets',
      data: {'name': name, 'mime': mime, 'size': bytes.length},
    );
    final data = ticket.data ?? const <String, dynamic>{};
    final putUrl = asString(data['putUrl']) ?? '';
    final id = asString(data['id']) ?? '';
    if (putUrl.isEmpty || id.isEmpty) {
      throw StateError('The server returned no upload ticket');
    }
    final headers = asMap(data['headers']);
    await _uploader.put<dynamic>(
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
    final done = await _dio.post<Map<String, dynamic>>(
      '/api/assets/${Uri.encodeComponent(id)}/complete',
    );
    return asString(done.data?['id']) ?? id;
  }

  static String? _clean(String? value) {
    final trimmed = value?.trim() ?? '';
    return trimmed.isEmpty ? null : trimmed;
  }
}

final firstVideoApiProvider = Provider<FirstVideoApi>(
  (ref) => FirstVideoApi(ref.watch(apiDioProvider)),
);

/// Labels and the expected wait, from the server when it answers and from
/// the bundled copy otherwise (the picker reads `fallback` keys then).
final firstVideoCatalogProvider = FutureProvider<FirstVideoCatalog?>((
  ref,
) async {
  try {
    return await ref.watch(firstVideoApiProvider).catalog();
  } catch (_) {
    return null;
  }
});
