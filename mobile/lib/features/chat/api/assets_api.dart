import 'dart:typed_data';

import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:video_thumbnail/video_thumbnail.dart';

import '../../../shared/api/providers.dart';
import '../../../shared/models/json.dart';

/// Presigned asset URL for previews (web `useAssetUrl`):
/// `GET /api/assets/{id}/url` → `{url, mime, name, size}`. Presigned GETs
/// live an hour; playback refreshes cached entries after 40 minutes.
class AssetUrl {
  const AssetUrl({
    required this.url,
    this.mime,
    this.name,
    this.size,
    this.refreshAfter,
  });

  final String url;
  final String? mime;
  final String? name;
  final int? size;
  final DateTime? refreshAfter;

  bool get needsRefresh =>
      refreshAfter != null && !DateTime.now().isBefore(refreshAfter!);
}

/// First frame of a video asset for its gallery tile (the web analog is the
/// browser painting `<video preload="metadata">`). Extracted natively
/// (AVAssetImageGenerator on iOS) straight from the presigned URL — only the
/// moov atom and one frame are fetched, not the whole file. Cached for the
/// session; null means extraction failed and the tile keeps its dark fallback.
final videoThumbnailProvider = FutureProvider.family<Uint8List?, String>((
  ref,
  assetId,
) async {
  ref.keepAlive();
  final asset = await ref.watch(assetUrlProvider(assetId).future);
  if (asset.url.isEmpty) return null;
  try {
    return await VideoThumbnail.thumbnailData(
      video: asset.url,
      imageFormat: ImageFormat.JPEG,
      maxWidth: 640,
      quality: 75,
    );
  } catch (_) {
    return null;
  }
});

final assetUrlProvider = FutureProvider.family<AssetUrl, String>((
  ref,
  assetId,
) async {
  final refreshAfter = DateTime.now().add(const Duration(minutes: 40));
  final resp = await ref
      .watch(apiDioProvider)
      .get<Map<String, dynamic>>('/api/assets/$assetId/url');
  final data = resp.data ?? const {};
  return AssetUrl(
    url: asString(data['url']) ?? '',
    mime: asString(data['mime']),
    name: asString(data['name']),
    size: asInt(data['size']),
    refreshAfter: refreshAfter,
  );
});
