import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/download/asset_download.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/message_part.dart';
import '../../../shared/widgets/media_viewer.dart';
import '../api/assets_api.dart';
import '../utils/content_origin.dart';
import 'ai_disclosure.dart';
import 'light_chat_scope.dart';

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
bool isAudioPart(FilePart part) => (part.mimeType ?? '').startsWith('audio/');

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
    this.artifactKind,
  });

  final List<FilePart> parts;
  final bool alignEnd;

  /// Full-width treatment for a final deliverable.
  final bool hero;

  /// Small checkpoint/group treatment inside another card.
  final bool compact;

  /// Origin resolved from persisted generation evidence, including shared copies.
  final String? artifactKind;

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
      crossAxisAlignment: widget.alignEnd
          ? CrossAxisAlignment.end
          : CrossAxisAlignment.start,
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
            childAspectRatio:
                ordered.every((p) => p.relation?.kind == 'qr_code')
                ? 1
                : 16 / 9,
            children: [
              for (final part in shown)
                _MediaThumb(
                  part: part,
                  artifactKind: widget.artifactKind,
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
    final artifactKind = widget.artifactKind;
    showMediaViewer(
      context,
      (_) => _AttachmentViewer(part: part, artifactKind: artifactKind),
    );
  }
}

class _MediaThumb extends ConsumerWidget {
  const _MediaThumb({
    required this.part,
    required this.onOpen,
    this.artifactKind,
  });

  final FilePart part;
  final VoidCallback onOpen;
  final String? artifactKind;

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
            ? _VideoTile(part: part, artifactKind: artifactKind)
            : asset.when(
                loading: () => const SizedBox.expand(),
                error: (_, _) => _failed(t, i18n),
                data: (info) => Image.network(
                  info.url,
                  fit: part.relation?.kind == 'qr_code'
                      ? BoxFit.contain
                      : BoxFit.cover,
                  frameBuilder: (_, child, frame, loaded) =>
                      frame != null || loaded
                      ? AiMediaOverlay(
                          part: part,
                          artifactKind: artifactKind,
                          child: child,
                        )
                      : child,
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
  const _VideoTile({required this.part, this.artifactKind});

  final FilePart part;
  final String? artifactKind;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final frame = ref.watch(videoThumbnailProvider(part.assetId!)).valueOrNull;
    return Stack(
      fit: StackFit.expand,
      children: [
        ColoredBox(color: t.term),
        if (frame != null)
          AiMediaOverlay(
            part: part,
            artifactKind: artifactKind,
            bottom: 32,
            child: Image.memory(
              frame,
              fit: BoxFit.contain,
              gaplessPlayback: true,
            ),
          ),
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
            child: const Icon(
              Icons.play_arrow,
              size: 22,
              color: Colors.black87,
            ),
          ),
        ),
        // A file name is pro-chat detail; a Light page shows only the clip.
        if (!LightChatScope.isLight(context))
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

class _AttachmentViewer extends ConsumerWidget {
  const _AttachmentViewer({required this.part, this.artifactKind});

  final FilePart part;
  final String? artifactKind;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final i18n = ref.watch(i18nProvider);
    final asset = ref.watch(assetUrlProvider(part.assetId!));
    final failed = MediaPreviewError(i18n.t('chat:gallery.failed'));
    Widget decorate(Widget child) =>
        AiMediaOverlay(part: part, artifactKind: artifactKind, child: child);

    return MediaViewer(
      title: _baseName(part.path),
      assetId: part.assetId,
      size: part.size,
      mimeType: part.mimeType,
      disclosure: isGeneratedMedia(part, artifactKind: artifactKind)
          ? const AiGeneratedLabel(onMedia: true)
          : null,
      child: asset.when(
        loading: MediaPreviewLoading.new,
        error: (_, _) => failed,
        data: (info) {
          if (info.url.isEmpty) return failed;
          return (part.mimeType ?? '').startsWith('video/')
              ? MediaPreviewVideo(
                  key: ValueKey(info.url),
                  source: info.url,
                  failedText: i18n.t('first_video:result.playFailed'),
                  decorate: decorate,
                )
              : MediaPreviewImage(
                  url: info.url,
                  failedText: failed.message,
                  decorate: decorate,
                );
        },
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
      await saveAssetToDevice(ref.read, assetId: assetId, name: widget.name);
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
