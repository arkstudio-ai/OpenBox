import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:url_launcher/url_launcher.dart';
import 'package:video_player/video_player.dart';

import '../../../shared/api/providers.dart';
import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/json.dart';
import '../../../shared/models/message_part.dart';
import '../../../shared/utils/format.dart';
import '../api/assets_api.dart';

const _visibleByDefault = 6;

/// Media the gallery can preview: uploaded assets with an image or video
/// mime. Web renders images only (`isGalleryImage`); mobile also plays
/// video parts instead of degrading them to filename chips.
bool isGalleryMedia(FilePart part) {
  final mime = part.mimeType ?? '';
  return part.assetId != null &&
      (mime.startsWith('image/') || mime.startsWith('video/'));
}

/// An uploaded audio asset — rendered as a player rather than a thumbnail
/// (web `isAudioPart`).
bool isAudioPart(FilePart part) =>
    (part.mimeType ?? '').startsWith('audio/');

String _baseName(String path) => path.split('/').last;

/// Preview one semantic resource group (web `AttachmentGallery`).
///
/// Computer-use evidence is handled by [WorkLogTrace]; this gallery therefore
/// preserves producer order instead of reversing a whole turn's unrelated
/// media into a contact sheet. Tapping opens the full-size viewer — the only
/// place a download belongs.
class AttachmentGallery extends ConsumerStatefulWidget {
  const AttachmentGallery({
    super.key,
    required this.parts,
    this.alignEnd = false,
    this.hero = false,
    this.compact = false,
  });

  final List<FilePart> parts;
  final bool alignEnd;

  /// Full-width treatment for a final deliverable.
  final bool hero;

  /// Small checkpoint/group treatment inside another card.
  final bool compact;

  @override
  ConsumerState<AttachmentGallery> createState() => _AttachmentGalleryState();
}

class _AttachmentGalleryState extends ConsumerState<AttachmentGallery> {
  bool _expanded = false;

  @override
  Widget build(BuildContext context) {
    if (widget.parts.isEmpty) return const SizedBox.shrink();
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    // Producer order. Each group is one semantic result now, so reversing
    // would show a video's segments backwards.
    final ordered = widget.parts;
    final limit = widget.compact ? 3 : _visibleByDefault;
    final shown = _expanded ? ordered : ordered.take(limit).toList();
    final hidden = ordered.length - shown.length;
    // One or two images are the subject, not a contact sheet — don't shrink
    // them into a third of the column just to keep the grid uniform.
    final columns = widget.hero || ordered.length == 1
        ? 1
        : (ordered.length == 2 ? 2 : 3);

    return Column(
      crossAxisAlignment:
          widget.alignEnd ? CrossAxisAlignment.end : CrossAxisAlignment.start,
      children: [
        ConstrainedBox(
          constraints: BoxConstraints(
            maxWidth: widget.hero || widget.compact
                ? double.infinity
                : (ordered.length == 1 ? 360 : 660),
          ),
          child: GridView.count(
            crossAxisCount: columns,
            shrinkWrap: true,
            physics: const NeverScrollableScrollPhysics(),
            mainAxisSpacing: 6,
            crossAxisSpacing: 6,
            childAspectRatio: 16 / 9,
            children: [
              for (final part in shown)
                _MediaThumb(
                  part: part,
                  onOpen: () => _openViewer(context, part),
                ),
            ],
          ),
        ),
        if (hidden > 0 || _expanded)
          GestureDetector(
            onTap: () => setState(() => _expanded = !_expanded),
            child: Padding(
              padding: const EdgeInsets.only(top: 6),
              child: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  AnimatedRotation(
                    turns: _expanded ? 0.5 : 0,
                    duration: const Duration(milliseconds: 150),
                    child: Icon(Icons.expand_more, size: 14, color: t.n600),
                  ),
                  const SizedBox(width: 3),
                  Text(
                    _expanded
                        ? i18n.t('chat:gallery.less')
                        : i18n.t('chat:gallery.more', count: hidden),
                    style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
                  ),
                ],
              ),
            ),
          ),
      ],
    );
  }

  void _openViewer(BuildContext context, FilePart part) {
    Navigator.of(context, rootNavigator: true).push(
      PageRouteBuilder<void>(
        opaque: false,
        barrierDismissible: true,
        pageBuilder: (_, _, _) => _MediaViewer(part: part),
        transitionsBuilder: (_, animation, _, child) =>
            FadeTransition(opacity: animation, child: child),
      ),
    );
  }
}

class _MediaThumb extends ConsumerWidget {
  const _MediaThumb({required this.part, required this.onOpen});

  final FilePart part;
  final VoidCallback onOpen;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final isVideo = (part.mimeType ?? '').startsWith('video/');
    final asset = ref.watch(assetUrlProvider(part.assetId!));

    return GestureDetector(
      onTap: onOpen,
      child: Container(
        clipBehavior: Clip.antiAlias,
        decoration: BoxDecoration(
          borderRadius: BorderRadius.circular(Radii.md),
          border: Border.all(color: t.hair),
          color: t.n200.withValues(alpha: 0.5),
        ),
        child: isVideo
            ? _VideoTile(part: part)
            : asset.when(
                loading: () => const SizedBox.expand(),
                error: (_, _) => _failed(t, i18n),
                data: (info) => Image.network(
                  info.url,
                  fit: BoxFit.cover,
                  errorBuilder: (_, _, _) => _failed(t, i18n),
                ),
              ),
      ),
    );
  }

  Widget _failed(BossipTokens t, I18nState i18n) => Center(
        child: Text(
          i18n.t('chat:gallery.failed'),
          style: TextStyle(fontSize: FontSizes.xs2, color: t.n600),
        ),
      );
}

/// Video gallery tile: first frame (natively extracted from the presigned
/// URL) under a play badge; falls back to the dark tile while the frame
/// loads or when extraction fails.
class _VideoTile extends ConsumerWidget {
  const _VideoTile({required this.part});

  final FilePart part;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final frame =
        ref.watch(videoThumbnailProvider(part.assetId!)).valueOrNull;
    return Stack(
      fit: StackFit.expand,
      children: [
        ColoredBox(color: t.term),
        if (frame != null)
          Image.memory(frame, fit: BoxFit.contain, gaplessPlayback: true),
        // Bottom scrim keeps the filename legible over any frame.
        Positioned(
          left: 0,
          right: 0,
          bottom: 0,
          height: 32,
          child: DecoratedBox(
            decoration: BoxDecoration(
              gradient: LinearGradient(
                begin: Alignment.bottomCenter,
                end: Alignment.topCenter,
                colors: [
                  Colors.black.withValues(alpha: 0.55),
                  Colors.black.withValues(alpha: 0),
                ],
              ),
            ),
          ),
        ),
        Center(
          child: Container(
            width: 34,
            height: 34,
            decoration: BoxDecoration(
              color: Colors.white.withValues(alpha: 0.9),
              shape: BoxShape.circle,
            ),
            child: const Icon(Icons.play_arrow, size: 22, color: Colors.black87),
          ),
        ),
        Positioned(
          left: 6,
          right: 6,
          bottom: 4,
          child: Text(
            _baseName(part.path),
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: const TextStyle(
              fontSize: FontSizes.xs2,
              color: Colors.white,
              fontFamily: 'Menlo',
              fontFamilyFallback: ['monospace'],
            ),
          ),
        ),
      ],
    );
  }
}

/// Full-screen viewer (web Lightbox): dark scrim, mono filename + size +
/// download + close header; pinch-zoom for images, playback for videos.
class _MediaViewer extends ConsumerWidget {
  const _MediaViewer({required this.part});

  final FilePart part;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final i18n = ref.watch(i18nProvider);
    final isVideo = (part.mimeType ?? '').startsWith('video/');
    final asset = ref.watch(assetUrlProvider(part.assetId!));
    final name = _baseName(part.path);

    return Scaffold(
      backgroundColor: Colors.black.withValues(alpha: 0.88),
      body: SafeArea(
        child: Column(
          children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 8, 8, 8),
              child: Row(
                children: [
                  Expanded(
                    child: Text(
                      name,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(
                        color: Colors.white,
                        fontSize: FontSizes.sm,
                        fontFamily: 'Menlo',
                        fontFamilyFallback: ['monospace'],
                      ),
                    ),
                  ),
                  if (part.size != null)
                    Padding(
                      padding: const EdgeInsets.symmetric(horizontal: 8),
                      child: Text(
                        formatBytes(part.size!),
                        style: TextStyle(
                          color: Colors.white.withValues(alpha: 0.7),
                          fontSize: FontSizes.xs2,
                        ),
                      ),
                    ),
                  IconButton(
                    icon: const Icon(Icons.file_download_outlined,
                        color: Colors.white, size: 20),
                    tooltip: i18n.t('chat:gallery.download'),
                    onPressed: () => _download(ref),
                  ),
                  IconButton(
                    icon: const Icon(Icons.close, color: Colors.white, size: 20),
                    tooltip: i18n.t('chat:gallery.close'),
                    onPressed: () => Navigator.of(context).pop(),
                  ),
                ],
              ),
            ),
            Expanded(
              child: GestureDetector(
                onTap: isVideo ? null : () => Navigator.of(context).pop(),
                child: asset.when(
                  loading: () => const Center(
                      child: CircularProgressIndicator(
                          strokeWidth: 2, color: Colors.white70)),
                  error: (_, _) => Center(
                    child: Text(
                      i18n.t('chat:gallery.failed'),
                      style: const TextStyle(
                          color: Colors.white70, fontSize: FontSizes.sm),
                    ),
                  ),
                  data: (info) => isVideo
                      ? _VideoBox(url: info.url)
                      : InteractiveViewer(
                          maxScale: 5,
                          child: Center(
                            child: Image.network(
                              info.url,
                              fit: BoxFit.contain,
                              errorBuilder: (_, _, _) => Text(
                                i18n.t('chat:gallery.failed'),
                                style: const TextStyle(
                                    color: Colors.white70,
                                    fontSize: FontSizes.sm),
                              ),
                            ),
                          ),
                        ),
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }

  Future<void> _download(WidgetRef ref) async {
    // Fresh URL with content-disposition so the browser saves it
    // (web Lightbox download).
    final resp = await ref.read(apiDioProvider).get<Map<String, dynamic>>(
      '/api/assets/${part.assetId}/url',
      queryParameters: {'download': true},
    );
    final url = asString(resp.data?['url']);
    if (url != null) {
      await launchUrl(Uri.parse(url), mode: LaunchMode.externalApplication);
    }
  }
}

class _VideoBox extends StatefulWidget {
  const _VideoBox({required this.url});

  final String url;

  @override
  State<_VideoBox> createState() => _VideoBoxState();
}

class _VideoBoxState extends State<_VideoBox> {
  late final VideoPlayerController _controller;
  bool _ready = false;
  bool _failed = false;

  @override
  void initState() {
    super.initState();
    _controller = VideoPlayerController.networkUrl(Uri.parse(widget.url))
      ..initialize().then((_) {
        if (mounted) {
          setState(() => _ready = true);
          _controller.play();
        }
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
    if (_failed) {
      return const Center(
        child: Icon(Icons.videocam_off_outlined, color: Colors.white54, size: 40),
      );
    }
    if (!_ready) {
      return const Center(
          child:
              CircularProgressIndicator(strokeWidth: 2, color: Colors.white70));
    }
    return GestureDetector(
      onTap: () => setState(() {
        _controller.value.isPlaying ? _controller.pause() : _controller.play();
      }),
      child: Column(
        children: [
          Expanded(
            child: Center(
              child: AspectRatio(
                aspectRatio: _controller.value.aspectRatio,
                child: VideoPlayer(_controller),
              ),
            ),
          ),
          VideoProgressIndicator(
            _controller,
            allowScrubbing: true,
            padding: const EdgeInsets.symmetric(vertical: 10, horizontal: 4),
          ),
        ],
      ),
    );
  }
}

/// Non-media file row (web `FileChip`): icon pill + mono filename. When the
/// file is a stored asset the chip downloads it, so a produced file is
/// reachable rather than merely named.
class FileChipRow extends ConsumerStatefulWidget {
  const FileChipRow({super.key, required this.name, this.assetId});

  final String name;
  final String? assetId;

  @override
  ConsumerState<FileChipRow> createState() => _FileChipRowState();
}

class _FileChipRowState extends ConsumerState<FileChipRow> {
  bool _downloading = false;

  Future<void> _download() async {
    final assetId = widget.assetId;
    if (assetId == null || _downloading) return;
    setState(() => _downloading = true);
    try {
      final resp = await ref.read(apiDioProvider).get<Map<String, dynamic>>(
        '/api/assets/$assetId/url',
        queryParameters: {'download': true},
      );
      final url = asString(resp.data?['url']);
      if (url != null) {
        await launchUrl(Uri.parse(url), mode: LaunchMode.externalApplication);
      }
    } finally {
      if (mounted) setState(() => _downloading = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final downloadable = widget.assetId != null;
    final chip = Container(
      padding: EdgeInsets.fromLTRB(6, 4, downloadable ? 8 : 14, 4),
      decoration: BoxDecoration(
        border: Border.all(color: t.hair),
        borderRadius: BorderRadius.circular(Radii.full),
        color: t.card,
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Container(
            width: 22,
            height: 22,
            decoration: BoxDecoration(color: t.n200, shape: BoxShape.circle),
            child: Icon(Icons.description_outlined, size: 12, color: t.n600),
          ),
          const SizedBox(width: 8),
          Flexible(
            child: Text(
              widget.name,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(
                fontSize: FontSizes.xs,
                color: t.ink,
                fontFamily: 'Menlo',
                fontFamilyFallback: const ['monospace'],
              ),
            ),
          ),
          if (downloadable) ...[
            const SizedBox(width: 8),
            _downloading
                ? SizedBox(
                    width: 13,
                    height: 13,
                    child: CircularProgressIndicator(
                      strokeWidth: 1.6,
                      color: t.n600,
                    ),
                  )
                : Icon(Icons.file_download_outlined, size: 14, color: t.n600),
          ],
        ],
      ),
    );
    if (!downloadable) return chip;
    return GestureDetector(onTap: _download, child: chip);
  }
}
