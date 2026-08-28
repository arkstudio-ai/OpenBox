import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/providers.dart';
import '../../../shared/models/video_identity.dart';

/// Real-person authorisation transport (web `api/video-identities.ts`).
class VideoIdentityApi {
  VideoIdentityApi(this._dio);

  final Dio _dio;

  /// Ask the backend to re-read the provider's state for one identity. The
  /// person authorises on their own device, so nothing tells this client when
  /// it lands — the card polls on demand.
  Future<VideoIdentity?> refresh(String identityId) async {
    final resp = await _dio.post<Map<String, dynamic>>(
      '/api/video/identities/${Uri.encodeComponent(identityId)}/refresh',
    );
    return VideoIdentity.tryParse(resp.data);
  }
}

final videoIdentityApiProvider = Provider<VideoIdentityApi>(
  (ref) => VideoIdentityApi(ref.watch(apiDioProvider)),
);
