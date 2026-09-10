import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/widgets/fold.dart';
import '../utils/content_view.dart';
import 'attachment_gallery.dart';
import 'audio_preview.dart';
import 'traces/douyin_tool_actions.dart';

/// Materials precede the final deliverable. Only an attached final video
/// folds the materials; pending/failed renders leave them visible.
class ResultArtifacts extends ConsumerWidget {
  const ResultArtifacts({
    super.key,
    required this.groups,
    required this.verification,
  });

  final List<ArtifactGroup> groups;
  final ArtifactGroup? verification;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    if (groups.isEmpty && verification == null) {
      return const SizedBox.shrink();
    }
    final finals = groups
        .where((g) => g.role == 'final' && g.artifactKind != 'video_segment')
        .toList();
    final segments = groups
        .where((g) => g.artifactKind == 'video_segment')
        .toList();
    final ordinary = groups
        .where((g) => g.role != 'final' && g.artifactKind != 'video_segment')
        .toList();
    final finalVideoAssets =
        groups
            .where(isFinalVideoArtifact)
            .expand((g) => g.parts)
            .where(
              (p) =>
                  (p.assetId?.isNotEmpty ?? false) &&
                  (p.mimeType?.startsWith('video/') ?? false),
            )
            .map((p) => p.assetId!)
            .toList()
          ..sort();

    return Padding(
      padding: const EdgeInsets.only(top: 10),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          if (segments.isNotEmpty)
            _SegmentCollection(
              key: ValueKey(jsonEncode(finalVideoAssets)),
              groups: segments,
              collapsible: finalVideoAssets.isNotEmpty,
            ),
          for (final group in finals)
            _ArtifactCard(key: ValueKey(group.id), group: group, hero: true),
          for (final group in ordinary)
            _ArtifactCard(key: ValueKey(group.id), group: group),
          if (verification != null) _VerificationCard(group: verification!),
        ],
      ),
    );
  }
}

class _SegmentCollection extends ConsumerStatefulWidget {
  const _SegmentCollection({
    super.key,
    required this.groups,
    required this.collapsible,
  });

  final List<ArtifactGroup> groups;
  final bool collapsible;

  @override
  ConsumerState<_SegmentCollection> createState() => _SegmentCollectionState();
}

class _SegmentCollectionState extends ConsumerState<_SegmentCollection> {
  bool _expanded = false;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final open = !widget.collapsible || _expanded;
    final title = i18n.t(
      'chat:artifacts.segmentCollection',
      count: widget.groups.length,
    );
    final titleStyle = TextStyle(
      fontSize: FontSizes.xs,
      fontWeight: FontWeight.w500,
      color: t.n600,
    );

    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        if (widget.collapsible)
          Semantics(
            expanded: open,
            child: TextButton(
              onPressed: () => setState(() => _expanded = !_expanded),
              style: TextButton.styleFrom(
                foregroundColor: t.n600,
                minimumSize: const Size(0, 44),
                padding: const EdgeInsets.symmetric(horizontal: 4),
              ),
              child: Row(
                children: [
                  AnimatedRotation(
                    turns: open ? 0 : -0.25,
                    duration: const Duration(milliseconds: 150),
                    child: const Icon(Icons.expand_more, size: 18),
                  ),
                  const SizedBox(width: 6),
                  Expanded(child: Text(title, style: titleStyle)),
                  const SizedBox(width: 8),
                  Text(
                    i18n.t(
                      open
                          ? 'chat:toolDetail.collapse'
                          : 'chat:toolDetail.expand',
                    ),
                    style: titleStyle,
                  ),
                ],
              ),
            ),
          )
        else
          Padding(
            padding: const EdgeInsets.only(top: 2, bottom: 6),
            child: Text(title, style: titleStyle),
          ),
        Fold(
          open: open,
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              for (final (index, group) in widget.groups.indexed)
                _ArtifactCard(
                  key: ValueKey(group.id),
                  group: group,
                  segmentNumber: index + 1,
                ),
            ],
          ),
        ),
      ],
    );
  }
}

class _ArtifactCard extends ConsumerStatefulWidget {
  const _ArtifactCard({
    super.key,
    required this.group,
    this.hero = false,
    this.segmentNumber,
  });

  final ArtifactGroup group;
  final bool hero;
  final int? segmentNumber;

  @override
  ConsumerState<_ArtifactCard> createState() => _ArtifactCardState();
}

class _ArtifactCardState extends ConsumerState<_ArtifactCard> {
  bool _transcriptOpen = false;

  String _title(I18nState i18n) {
    final group = widget.group;
    switch (group.artifactKind) {
      case 'video_final':
        return group.label ?? i18n.t('chat:artifacts.finalVideo');
      case 'video_segment':
        final number = group.ordinal ?? widget.segmentNumber;
        return number == null
            ? i18n.t('chat:artifacts.videoSegment')
            : i18n.t('chat:artifacts.segment', vars: {'number': number});
      case 'generated_image':
        return i18n.t(
          'chat:artifacts.generatedImages',
          count: group.parts.length,
        );
      default:
        return group.label ?? i18n.t('chat:artifacts.result');
    }
  }

  IconData get _icon => switch (widget.group.artifactKind) {
    'generated_image' => Icons.image_outlined,
    'shared_file' => Icons.folder_zip_outlined,
    _ => Icons.movie_outlined,
  };

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final group = widget.group;
    final media = group.parts.where(isGalleryMedia).toList();
    final audio = group.parts
        .where((p) => isAudioPart(p) && p.assetId != null)
        .toList();
    final files = group.parts
        .where(
          (p) => !isGalleryMedia(p) && !(isAudioPart(p) && p.assetId != null),
        )
        .toList();
    final transcript = group.metadataString('transcript');
    final revision = group.revision;

    return Container(
      margin: const EdgeInsets.only(bottom: 10),
      padding: EdgeInsets.all(widget.hero ? 14 : 11),
      decoration: BoxDecoration(
        border: Border.all(color: t.hair),
        borderRadius: BorderRadius.circular(Radii.lg),
        color: t.card.withValues(alpha: 0.45),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Container(
                width: 26,
                height: 26,
                decoration: BoxDecoration(
                  color: t.n200,
                  shape: BoxShape.circle,
                ),
                child: Icon(_icon, size: 14, color: t.n700),
              ),
              const SizedBox(width: 8),
              Expanded(
                child: Text(
                  _title(i18n),
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(
                    fontSize: FontSizes.sm,
                    fontWeight: FontWeight.w500,
                    color: t.ink,
                  ),
                ),
              ),
              if (revision != null && revision > 1)
                _Pill(
                  text: i18n.t(
                    'chat:artifacts.revision',
                    vars: {'number': revision},
                  ),
                  background: t.n200,
                  foreground: t.n600,
                ),
              _QaBadge(group: group),
            ],
          ),
          if (group.caption != null) ...[
            const SizedBox(height: 8),
            RichText(
              text: TextSpan(
                children: [
                  if (group.artifactKind == 'generated_image')
                    TextSpan(
                      text: '${i18n.t('chat:artifacts.prompt')} ',
                      style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
                    ),
                  TextSpan(
                    text: group.caption,
                    style: TextStyle(
                      fontSize: FontSizes.sm,
                      height: 1.55,
                      color: t.n700,
                    ),
                  ),
                ],
              ),
            ),
          ],
          if (transcript != null && transcript != group.caption) ...[
            const SizedBox(height: 6),
            GestureDetector(
              onTap: () => setState(() => _transcriptOpen = !_transcriptOpen),
              child: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  AnimatedRotation(
                    turns: _transcriptOpen ? 0.5 : 0,
                    duration: const Duration(milliseconds: 150),
                    child: Icon(Icons.expand_more, size: 14, color: t.n600),
                  ),
                  const SizedBox(width: 3),
                  Text(
                    i18n.t('chat:artifacts.transcript'),
                    style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
                  ),
                ],
              ),
            ),
            Fold(
              open: _transcriptOpen,
              child: Padding(
                padding: const EdgeInsets.only(top: 4),
                child: Text(
                  transcript,
                  style: TextStyle(
                    fontSize: FontSizes.xs,
                    height: 1.6,
                    color: t.n700,
                  ),
                ),
              ),
            ),
          ],
          if (media.isNotEmpty) ...[
            const SizedBox(height: 8),
            AttachmentGallery(
              parts: media,
              hero: widget.hero || group.artifactKind == 'video_final',
              compact: !widget.hero,
            ),
          ],
          if (group.artifactKind == 'qr_code' &&
              group.sourceTool?.tool == 'douyin_publish') ...[
            const SizedBox(height: 8),
            DouyinToolActions(part: group.sourceTool!),
          ],
          for (final part in audio) AudioPreview(part: part),
          if (files.isNotEmpty) ...[
            const SizedBox(height: 8),
            Wrap(
              spacing: 6,
              runSpacing: 6,
              children: [
                for (final part in files)
                  FileChipRow(
                    name: part.path.split('/').last,
                    assetId: part.assetId,
                  ),
              ],
            ),
          ],
        ],
      ),
    );
  }
}

/// Speech-to-text verdict for a video segment, when the pipeline recorded one.
class _QaBadge extends ConsumerWidget {
  const _QaBadge({required this.group});

  final ArtifactGroup group;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final verdict = group.metadataString('stt_verdict');
    if (verdict == null) return const SizedBox.shrink();
    final ok = verdict == 'ok';
    final similarity = group.metadataNumber('stt_similarity');
    final suffix = similarity == null
        ? ''
        : ' · ${(similarity * 100).round()}%';
    return Padding(
      padding: const EdgeInsets.only(left: 6),
      child: _Pill(
        text:
            (ok
                ? i18n.t('chat:artifacts.sttOk')
                : i18n.t('chat:artifacts.sttReview')) +
            suffix,
        background: ok ? t.s100 : t.dangerSoft,
        foreground: ok ? t.s700 : t.dangerInk,
        icon: ok ? Icons.check_circle_outline : Icons.error_outline,
      ),
    );
  }
}

class _Pill extends StatelessWidget {
  const _Pill({
    required this.text,
    required this.background,
    required this.foreground,
    this.icon,
  });

  final String text;
  final Color background;
  final Color foreground;
  final IconData? icon;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 7, vertical: 2),
      decoration: BoxDecoration(
        color: background,
        borderRadius: BorderRadius.circular(Radii.full),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          if (icon != null) ...[
            Icon(icon, size: 11, color: foreground),
            const SizedBox(width: 3),
          ],
          Text(
            text,
            style: TextStyle(
              fontSize: FontSizes.xs2,
              fontWeight: FontWeight.w500,
              color: foreground,
            ),
          ),
        ],
      ),
    );
  }
}

class _VerificationCard extends ConsumerWidget {
  const _VerificationCard({required this.group});

  final ArtifactGroup group;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return Container(
      margin: const EdgeInsets.only(top: 2),
      padding: const EdgeInsets.all(11),
      decoration: BoxDecoration(
        border: Border.all(color: t.hair),
        borderRadius: BorderRadius.circular(Radii.lg),
        color: t.card.withValues(alpha: 0.35),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Icon(Icons.desktop_windows_outlined, size: 15, color: t.n700),
              const SizedBox(width: 6),
              Text(
                i18n.t('chat:artifacts.verification'),
                style: TextStyle(
                  fontSize: FontSizes.xs,
                  fontWeight: FontWeight.w500,
                  color: t.n700,
                ),
              ),
            ],
          ),
          const SizedBox(height: 8),
          AttachmentGallery(
            parts: group.parts.where(isGalleryMedia).toList(),
            hero: true,
          ),
        ],
      ),
    );
  }
}
