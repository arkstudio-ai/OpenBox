import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:url_launcher/url_launcher.dart';
import 'package:video_player/video_player.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/resource.dart';
import '../api/resources_api.dart';
import '../utils/resource_display.dart';

/// The preview surface (web `ResourcePreview`). Pictures, video and audio play
/// straight off the presigned URL; text and code come back through the API
/// (the bucket refuses a cross-origin read); anything else gets an honest
/// "no preview" card rather than a broken embed.
class ResourcePreview extends ConsumerWidget {
  const ResourcePreview({
    super.key,
    required this.resource,
    required this.onDownload,
  });

  final Resource resource;
  final VoidCallback onDownload;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    if (resource.kind == 'image') {
      return _ImageBody(resource: resource);
    }
    if (resource.kind == 'video' || resource.kind == 'audio') {
      return _MediaBody(
        url: resource.url,
        audioOnly: resource.kind == 'audio',
      );
    }
    if (isTextPreviewable(resource)) {
      return _TextBody(resource: resource);
    }
    // A PDF has no in-app viewer here (web hands it to an <iframe>); opening
    // it externally is the honest mobile equivalent of that.
    return _Unsupported(
      resource: resource,
      onDownload: onDownload,
      openLabelKey:
          isPdf(resource) ? 'resources:actions.open' : 'resources:actions.download',
    );
  }
}

class _ImageBody extends StatelessWidget {
  const _ImageBody({required this.resource});

  final Resource resource;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return InteractiveViewer(
      maxScale: 5,
      child: Center(
        child: Image.network(
          resource.url,
          fit: BoxFit.contain,
          errorBuilder: (_, _, _) =>
              Icon(Icons.broken_image_outlined, size: 32, color: t.n500),
        ),
      ),
    );
  }
}

class _MediaBody extends StatefulWidget {
  const _MediaBody({required this.url, required this.audioOnly});

  final String url;
  final bool audioOnly;

  @override
  State<_MediaBody> createState() => _MediaBodyState();
}

class _MediaBodyState extends State<_MediaBody> {
  late final VideoPlayerController _controller;
  bool _ready = false;
  bool _failed = false;

  @override
  void initState() {
    super.initState();
    _controller = VideoPlayerController.networkUrl(Uri.parse(widget.url))
      ..initialize().then((_) {
        if (mounted) setState(() => _ready = true);
      }).catchError((Object _) {
        if (mounted) setState(() => _failed = true);
      });
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    if (_failed) {
      return Center(
        child: Icon(Icons.error_outline, size: 28, color: t.n500),
      );
    }
    if (!_ready) {
      return const Center(
          child: CircularProgressIndicator(strokeWidth: 2));
    }
    return Center(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          if (!widget.audioOnly)
            AspectRatio(
              aspectRatio: _controller.value.aspectRatio,
              child: ClipRRect(
                borderRadius: BorderRadius.circular(Radii.lg),
                child: VideoPlayer(_controller),
              ),
            )
          else
            Icon(Icons.audiotrack, size: 40, color: t.n500),
          const SizedBox(height: 12),
          VideoProgressIndicator(_controller, allowScrubbing: true),
          IconButton(
            iconSize: 34,
            color: t.ink,
            icon: Icon(_controller.value.isPlaying
                ? Icons.pause_circle_filled
                : Icons.play_circle_fill),
            onPressed: () => setState(() {
              _controller.value.isPlaying
                  ? _controller.pause()
                  : _controller.play();
            }),
          ),
        ],
      ),
    );
  }
}

class _TextBody extends ConsumerWidget {
  const _TextBody({required this.resource});

  final Resource resource;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final text = ref.watch(resourceTextProvider(resource.id));
    return text.when(
      loading: () =>
          const Center(child: CircularProgressIndicator(strokeWidth: 2)),
      error: (_, _) => Center(
        child: Text(
          i18n.t('resources:preview.textFailed'),
          style: TextStyle(fontSize: FontSizes.sm, color: t.n600),
        ),
      ),
      data: (body) => SingleChildScrollView(
        padding: const EdgeInsets.all(14),
        child: Container(
          width: double.infinity,
          padding: const EdgeInsets.all(12),
          decoration: BoxDecoration(
            color: t.card,
            border: Border.all(color: t.hair),
            borderRadius: BorderRadius.circular(Radii.lg),
          ),
          child: SelectableText(
            body,
            style: TextStyle(
              fontSize: FontSizes.xs,
              height: 1.6,
              color: t.ink,
              fontFamily: 'Menlo',
              fontFamilyFallback: const ['monospace'],
            ),
          ),
        ),
      ),
    );
  }
}

class _Unsupported extends ConsumerWidget {
  const _Unsupported({
    required this.resource,
    required this.onDownload,
    required this.openLabelKey,
  });

  final Resource resource;
  final VoidCallback onDownload;
  final String openLabelKey;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return Center(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(kindIcon(resource.kind), size: 36, color: t.n500),
          const SizedBox(height: 12),
          Text(
            i18n.t('resources:preview.unsupported'),
            style: TextStyle(fontSize: FontSizes.base, color: t.n700),
          ),
          const SizedBox(height: 14),
          FilledButton.icon(
            onPressed: () {
              if (openLabelKey == 'resources:actions.open') {
                launchUrl(Uri.parse(resource.url),
                    mode: LaunchMode.externalApplication);
              } else {
                onDownload();
              }
            },
            style: FilledButton.styleFrom(
              backgroundColor: t.ink,
              foregroundColor: t.bg,
              shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(Radii.full),
              ),
            ),
            icon: Icon(
              openLabelKey == 'resources:actions.open'
                  ? Icons.open_in_new
                  : Icons.file_download_outlined,
              size: 16,
            ),
            label: Text(i18n.t(openLabelKey),
                style: const TextStyle(fontSize: FontSizes.sm)),
          ),
        ],
      ),
    );
  }
}
