import 'dart:developer' as developer;

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/widgets/media_viewer.dart';
import '../api/assets_api.dart';

/// The player's URL is a short-lived signature for an owned video asset.
class AssetVideoPreview extends ConsumerStatefulWidget {
  const AssetVideoPreview({
    super.key,
    required this.assetId,
    required this.failedText,
    this.decorate,
    this.subtitles,
  });

  final String assetId;
  final String failedText;
  final Widget Function(Widget video)? decorate;
  final String? subtitles;

  @override
  ConsumerState<AssetVideoPreview> createState() => _AssetVideoPreviewState();
}

class _AssetVideoPreviewState extends ConsumerState<AssetVideoPreview> {
  late Future<AssetUrl> _source = _loadUrl();
  bool _retried = false;

  Future<AssetUrl> _loadUrl({bool refresh = false}) async {
    final assetId = widget.assetId;
    // The thumbnail may already be listening to this provider while the
    // fullscreen route builds. Start any invalidation after that frame's build.
    await Future<void>.value();
    if (!mounted) return const AssetUrl(url: '');
    final provider = assetUrlProvider(assetId);
    try {
      if (refresh) return await ref.refresh(provider.future);
      final cached = ref.read(provider);
      if (!cached.isLoading &&
          (cached.hasError || cached.valueOrNull?.needsRefresh == true)) {
        return await ref.refresh(provider.future);
      }
      return await ref.read(provider.future);
    } catch (error) {
      final code = error is DioException
          ? error.response?.statusCode ?? error.type.name
          : error.runtimeType;
      developer.log(
        'asset=$assetId stage=sign code=$code',
        name: 'video.preview',
      );
      rethrow;
    }
  }

  void _onPlaybackError(String code) {
    developer.log(
      'asset=${widget.assetId} stage=playback code=$code',
      name: 'video.preview',
    );
    if (_retried) return;
    setState(() {
      _retried = true;
      _source = _loadUrl(refresh: true);
    });
  }

  @override
  void didUpdateWidget(AssetVideoPreview oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.assetId != widget.assetId) {
      _retried = false;
      _source = _loadUrl();
    }
  }

  @override
  Widget build(BuildContext context) => FutureBuilder<AssetUrl>(
    future: _source,
    builder: (context, snapshot) {
      if (snapshot.connectionState != ConnectionState.done) {
        return const MediaPreviewLoading();
      }
      final url = snapshot.data?.url;
      if (snapshot.hasError || url == null || url.isEmpty) {
        return MediaPreviewError(widget.failedText);
      }
      return MediaPreviewVideo(
        key: ValueKey((widget.assetId, url, _retried)),
        source: url,
        failedText: widget.failedText,
        decorate: widget.decorate,
        subtitles: widget.subtitles,
        onPlaybackError: _onPlaybackError,
      );
    },
  );
}
