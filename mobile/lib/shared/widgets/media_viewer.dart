import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:video_player/video_player.dart';

import '../appearance/tokens.dart';
import '../appearance/type_scale.dart';
import '../download/asset_download.dart';
import '../i18n/i18n.dart';
import '../utils/format.dart';

Future<void> showMediaViewer(BuildContext context, WidgetBuilder builder) =>
    Navigator.of(context, rootNavigator: true).push<void>(
      MaterialPageRoute<void>(builder: builder, fullscreenDialog: true),
    );

/// Shared full-screen surface for image and video previews.
class MediaViewer extends ConsumerStatefulWidget {
  const MediaViewer({
    super.key,
    required this.child,
    this.title,
    this.size,
    this.disclosure,
    this.assetId,
    this.mimeType,
  });

  final Widget child;
  final String? title;
  final int? size;
  final Widget? disclosure;
  final String? assetId;
  final String? mimeType;

  @override
  ConsumerState<MediaViewer> createState() => _MediaViewerState();
}

class _MediaViewerState extends ConsumerState<MediaViewer> {
  double? _progress;

  Future<void> _save(AssetSaveTarget target) async {
    if (_progress != null) return;
    setState(() => _progress = 0);
    try {
      await saveAssetToDevice(
        ProviderScope.containerOf(context, listen: false).read,
        assetId: widget.assetId!,
        name: widget.title ?? '',
        mimeType: widget.mimeType,
        target: target,
        onProgress: (fraction) {
          if (mounted) setState(() => _progress = fraction);
        },
      );
    } finally {
      if (mounted) setState(() => _progress = null);
    }
  }

  @override
  Widget build(BuildContext context) {
    final i18n = ref.watch(i18nProvider);
    final foreground = context.tokens.mediaLabel;
    final title = widget.title;
    final size = widget.size;
    final progress = _progress;
    final hasDetails = size != null || widget.disclosure != null;
    return AnnotatedRegion<SystemUiOverlayStyle>(
      value: SystemUiOverlayStyle.light,
      child: Scaffold(
        backgroundColor: context.tokens.term,
        body: SafeArea(
          child: Column(
            children: [
              Padding(
                padding: const EdgeInsetsDirectional.fromSTEB(16, 4, 8, 4),
                child: Row(
                  children: [
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          if (title != null && title.isNotEmpty)
                            Text(
                              title,
                              maxLines: 1,
                              overflow: TextOverflow.ellipsis,
                              style: TextStyle(
                                color: foreground,
                                fontSize: FontSizes.sm,
                                fontWeight: FontWeight.w500,
                              ),
                            ),
                          if (hasDetails)
                            FittedBox(
                              fit: BoxFit.scaleDown,
                              alignment: AlignmentDirectional.centerStart,
                              child: Row(
                                children: [
                                  ?widget.disclosure,
                                  if (widget.disclosure != null && size != null)
                                    const SizedBox(width: 8),
                                  if (size != null)
                                    Text(
                                      formatBytes(size),
                                      style: TextStyle(
                                        color: foreground.withValues(
                                          alpha: 0.72,
                                        ),
                                        fontSize: FontSizes.xs2,
                                      ),
                                    ),
                                ],
                              ),
                            ),
                        ],
                      ),
                    ),
                    if (widget.assetId != null) ...[
                      IconButton(
                        color: foreground,
                        tooltip: i18n.t('chat:download.saveToAlbum'),
                        onPressed: progress == null
                            ? () => _save(AssetSaveTarget.album)
                            : null,
                        icon: const Icon(Icons.photo_library_outlined),
                      ),
                      IconButton(
                        color: foreground,
                        tooltip: i18n.t('chat:gallery.download'),
                        onPressed: progress == null
                            ? () => _save(AssetSaveTarget.files)
                            : null,
                        icon: const Icon(Icons.file_download_outlined),
                      ),
                    ],
                    IconButton(
                      key: const Key('media-viewer-close'),
                      color: foreground,
                      tooltip: i18n.t('chat:gallery.close'),
                      onPressed: () => Navigator.of(context).pop(),
                      icon: const Icon(Icons.close),
                    ),
                  ],
                ),
              ),
              if (progress != null)
                Padding(
                  padding: const EdgeInsets.symmetric(horizontal: 16),
                  child: LinearProgressIndicator(
                    value: progress == 0 ? null : progress,
                    color: foreground,
                    backgroundColor: foreground.withValues(alpha: 0.2),
                    semanticsLabel: i18n.t(
                      'chat:download.downloading',
                      vars: {'percent': (progress * 100).round().toString()},
                    ),
                  ),
                ),
              Expanded(child: widget.child),
            ],
          ),
        ),
      ),
    );
  }
}

class MediaPreviewLoading extends StatelessWidget {
  const MediaPreviewLoading({super.key});

  @override
  Widget build(BuildContext context) => Center(
    child: CircularProgressIndicator(
      strokeWidth: 2,
      color: context.tokens.mediaLabel,
    ),
  );
}

class MediaPreviewError extends StatelessWidget {
  const MediaPreviewError(this.message, {super.key});

  final String message;

  @override
  Widget build(BuildContext context) => Center(
    child: Padding(
      padding: const EdgeInsets.all(24),
      child: Text(
        message,
        textAlign: TextAlign.center,
        style: TextStyle(color: context.tokens.mediaLabel),
      ),
    ),
  );
}

class MediaPreviewImage extends StatelessWidget {
  const MediaPreviewImage({
    super.key,
    required this.url,
    required this.failedText,
    this.decorate,
  });

  final String url;
  final String failedText;
  final Widget Function(Widget image)? decorate;

  @override
  Widget build(BuildContext context) => InteractiveViewer(
    maxScale: 5,
    child: Center(
      child: Image.network(
        url,
        fit: BoxFit.contain,
        frameBuilder: (_, child, frame, synchronous) {
          if (!synchronous && frame == null) return const MediaPreviewLoading();
          return decorate?.call(child) ?? child;
        },
        errorBuilder: (_, _, _) => MediaPreviewError(failedText),
      ),
    ),
  );
}

class MediaPreviewVideo extends ConsumerStatefulWidget {
  const MediaPreviewVideo({
    super.key,
    required this.source,
    required this.failedText,
    this.bundled = false,
    this.loop = false,
    this.decorate,
    this.subtitles,
  });

  final String source;
  final String failedText;
  final bool bundled;
  final bool loop;
  final Widget Function(Widget video)? decorate;
  final String? subtitles;

  @override
  ConsumerState<MediaPreviewVideo> createState() => _MediaPreviewVideoState();
}

class _MediaPreviewVideoState extends ConsumerState<MediaPreviewVideo> {
  late final VideoPlayerController _controller = widget.bundled
      ? VideoPlayerController.asset(widget.source)
      : VideoPlayerController.networkUrl(
          Uri.parse(widget.source),
          closedCaptionFile: widget.subtitles == null
              ? null
              : Future.value(SubRipCaptionFile(widget.subtitles!)),
        );
  late final Future<void> _load;
  bool _playbackFailed = false;

  @override
  void initState() {
    super.initState();
    _load = _start();
  }

  Future<void> _start() async {
    await _controller.initialize();
    if (!mounted) return;
    if (widget.loop) await _controller.setLooping(true);
    if (mounted) await _controller.play();
  }

  Future<void> _toggle() async {
    try {
      if (_controller.value.isPlaying) {
        await _controller.pause();
      } else {
        await _controller.play();
      }
    } catch (_) {
      if (mounted) setState(() => _playbackFailed = true);
    }
  }

  @override
  void dispose() {
    unawaited(_controller.dispose());
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final i18n = ref.watch(i18nProvider);
    final foreground = context.tokens.mediaLabel;
    return FutureBuilder<void>(
      future: _load,
      builder: (context, snapshot) {
        if (snapshot.hasError || _playbackFailed) {
          return MediaPreviewError(widget.failedText);
        }
        if (snapshot.connectionState != ConnectionState.done) {
          return const MediaPreviewLoading();
        }
        final video = VideoPlayer(_controller);
        return ValueListenableBuilder<VideoPlayerValue>(
          valueListenable: _controller,
          child: widget.decorate?.call(video) ?? video,
          builder: (context, value, video) {
            if (value.hasError) return MediaPreviewError(widget.failedText);
            final toggleLabel = i18n.t(
              value.isPlaying
                  ? 'light:production.pausePreview'
                  : 'light:production.playPreview',
            );
            return Column(
              children: [
                Expanded(
                  child: Center(
                    child: AspectRatio(
                      aspectRatio: value.aspectRatio,
                      child: GestureDetector(
                        onTap: _toggle,
                        child: Stack(
                          fit: StackFit.expand,
                          children: [
                            video!,
                            if (value.caption.text.isNotEmpty)
                              Positioned(
                                left: 12,
                                right: 12,
                                bottom: 8,
                                child: IgnorePointer(
                                  child: ClosedCaption(
                                    text: value.caption.text,
                                  ),
                                ),
                              ),
                            if (value.isBuffering) const MediaPreviewLoading(),
                          ],
                        ),
                      ),
                    ),
                  ),
                ),
                Padding(
                  padding: const EdgeInsetsDirectional.fromSTEB(8, 4, 16, 4),
                  child: Row(
                    children: [
                      IconButton(
                        tooltip: toggleLabel,
                        color: foreground,
                        onPressed: _toggle,
                        icon: Icon(
                          value.isPlaying
                              ? Icons.pause_rounded
                              : value.isCompleted
                              ? Icons.replay_rounded
                              : Icons.play_arrow_rounded,
                        ),
                      ),
                      Expanded(
                        child: VideoProgressIndicator(
                          _controller,
                          allowScrubbing: true,
                          padding: const EdgeInsets.symmetric(vertical: 20),
                          colors: VideoProgressColors(
                            playedColor: foreground,
                            bufferedColor: foreground.withValues(alpha: 0.45),
                            backgroundColor: foreground.withValues(alpha: 0.2),
                          ),
                        ),
                      ),
                      const SizedBox(width: 12),
                      Text(
                        '${_clock(value.position)} / ${_clock(value.duration)}',
                        style: TextStyle(
                          color: foreground.withValues(alpha: 0.82),
                          fontSize: FontSizes.xs,
                          fontFeatures: const [FontFeature.tabularFigures()],
                        ),
                      ),
                    ],
                  ),
                ),
              ],
            );
          },
        );
      },
    );
  }
}

String _clock(Duration duration) {
  final hours = duration.inHours;
  final minutes = duration.inMinutes.remainder(60).toString().padLeft(2, '0');
  final seconds = duration.inSeconds.remainder(60).toString().padLeft(2, '0');
  return hours == 0 ? '$minutes:$seconds' : '$hours:$minutes:$seconds';
}
