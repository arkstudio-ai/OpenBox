import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/providers.dart';
import '../../../shared/models/json.dart';
import '../../../shared/models/light.dart';
import '../../../shared/models/session.dart';

/// Transport for Light 模式 (backend `api/light.py`, docs
/// `LIGHT_MODE_BACKEND.md`). Every call carries the usual bearer and
/// `X-Workspace-Id`; availability and the recent results are computed for
/// that workspace on the server.
class LightApi {
  /// [uploader] carries photo bytes to object storage: a bare client, since
  /// the API interceptors would add our bearer to the bucket request and the
  /// presigned signature covers exactly the ticket's headers.
  LightApi(this._dio, {Dio? uploader}) : _uploader = uploader ?? Dio();

  final Dio _dio;
  final Dio _uploader;

  Future<LightEntriesView> entries() async {
    final resp = await _dio.get<Map<String, dynamic>>('/api/light/entries');
    return LightEntriesView.fromJson(resp.data ?? const {});
  }

  /// A real conversation with the entry's opening card already in it.
  /// 409 `LIGHT_ENTRY_UNAVAILABLE` carries `availability` in its detail.
  Future<LightStartResult> start({
    required String entryId,
    String? projectId,
    String? subtype,
    String? sample,
  }) async {
    final resp = await _dio.post<Map<String, dynamic>>(
      '/api/light/start',
      data: {
        'entry_id': entryId,
        'project_id': ?projectId,
        'subtype': ?subtype,
        'sample': ?sample,
      },
    );
    return LightStartResult.fromJson(resp.data ?? const {});
  }

  Future<List<LightRecentGroup>> recent({int limit = 8}) async {
    final resp = await _dio.get<Map<String, dynamic>>(
      '/api/light/recent',
      queryParameters: {'limit': limit},
    );
    return lightRecentGroupsFromJson(resp.data ?? const {});
  }

  /// Presigned GET for a result's thumbnail (`GET /api/assets/{id}/url`),
  /// fetched here because features never import each other.
  Future<String?> assetUrl(String assetId) async {
    final resp = await _dio.get<Map<String, dynamic>>(
      '/api/assets/${Uri.encodeComponent(assetId)}/url',
    );
    return asString(resp.data?['url']);
  }

  /// The conversations of this workspace, the same read the drawer makes.
  Future<List<Session>> sessions() async {
    final resp = await _dio.get<List<dynamic>>('/api/agent/session');
    return (resp.data ?? const [])
        .whereType<Map<String, dynamic>>()
        .map(Session.fromJson)
        .toList();
  }

  /// What the personal assistant knows about them: which it follows and the
  /// latest result summary (`GET /api/assistant/sessions`).
  Future<List<Map<String, dynamic>>> assistantSessions() async {
    final resp = await _dio.get<Map<String, dynamic>>(
      '/api/assistant/sessions',
      queryParameters: {'limit': 50},
    );
    return asList(resp.data?['items']).whereType<Map<String, dynamic>>().toList();
  }

  /// The shop name the first-video flow recorded, for the home heading
  /// (`extra.first_video.shop_name`; M4 reads `stores` instead).
  Future<String?> shopName() async {
    final resp = await _dio.get<Map<String, dynamic>>(
      '/api/auth/me/preferences',
    );
    final name = asString(
      asMap(asMap(resp.data?['extra'])['first_video'])['shop_name'],
    )?.trim();
    return name == null || name.isEmpty ? null : name;
  }

  /// A photo for the topic chat, uploaded the way the resource centre does
  /// it: open an upload, PUT the bytes to object storage, have the backend
  /// verify the object landed. Returns the asset id to attach.
  Future<String> uploadImage({
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
}

final lightApiProvider = Provider<LightApi>(
  (ref) => LightApi(ref.watch(apiDioProvider)),
);
