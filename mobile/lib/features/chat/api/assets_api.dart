import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/providers.dart';
import '../../../shared/models/json.dart';

/// Presigned asset URL for previews (web `useAssetUrl`):
/// `GET /api/assets/{id}/url` → `{url, mime, name, size}`. Presigned GETs
/// live an hour; cached entries refresh well inside that (40 min, like web).
class AssetUrl {
  const AssetUrl({required this.url, this.mime, this.name, this.size});

  final String url;
  final String? mime;
  final String? name;
  final int? size;
}

final assetUrlProvider =
    FutureProvider.family<AssetUrl, String>((ref, assetId) async {
  final link = ref.keepAlive();
  final expiry = Timer(const Duration(minutes: 40), link.close);
  ref.onDispose(expiry.cancel);
  final resp = await ref
      .watch(apiDioProvider)
      .get<Map<String, dynamic>>('/api/assets/$assetId/url');
  final data = resp.data ?? const {};
  return AssetUrl(
    url: asString(data['url']) ?? '',
    mime: asString(data['mime']),
    name: asString(data['name']),
    size: asInt(data['size']),
  );
});
