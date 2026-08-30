import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../../shared/appearance/tokens.dart';
import '../../../../shared/appearance/type_scale.dart';
import '../../../../shared/i18n/i18n.dart';
import '../../../../shared/models/message_part.dart';
import '../../api/assets_api.dart';
import '../attachment_gallery.dart';

/// Historical receipts render from stored part data alone, without a live API.
Color _receiptColor(BossipTokens t, String status) => switch (status.trim()) {
  'succeeded' => t.sage,
  'failed' => t.danger,
  _ => t.n400,
};

String skillJobReceiptTitle(SkillJobPart part) {
  final skill = part.skillKey
      .replaceFirst(RegExp(r'^(builtin|user):'), '')
      .trim();
  final operation = part.operation.trim();
  return [skill, operation].where((value) => value.isNotEmpty).join(' · ');
}

String skillJobReceiptStatusLabel(I18nState i18n, String status) {
  final normalized = status.trim();
  return switch (normalized) {
    'succeeded' => i18n.t('jobs:status.succeeded'),
    'failed' => i18n.t('jobs:status.failed'),
    'cancelled' => i18n.t('jobs:status.cancelled'),
    _ when normalized.isNotEmpty => normalized,
    _ => i18n.t('common:state.unavailable'),
  };
}

enum SkillJobReceiptArtifactKind { video, image, file }

SkillJobReceiptArtifactKind skillJobReceiptArtifactKind(String? mime) {
  final normalized = mime?.trim().toLowerCase() ?? '';
  if (normalized.startsWith('video/')) {
    return SkillJobReceiptArtifactKind.video;
  }
  if (normalized.startsWith('image/')) {
    return SkillJobReceiptArtifactKind.image;
  }
  return SkillJobReceiptArtifactKind.file;
}

/// Read-only rendering for durable receipts stored in historical transcripts.
class SkillJobReceipts extends ConsumerWidget {
  const SkillJobReceipts({super.key, required this.parts});

  final List<MessagePart> parts;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final receipts = parts.whereType<SkillJobPart>().toList();
    if (receipts.isEmpty) return const SizedBox.shrink();

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [for (final part in receipts) _Receipt(part: part)],
    );
  }
}

class _Receipt extends ConsumerWidget {
  const _Receipt({required this.part});

  final SkillJobPart part;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final title = skillJobReceiptTitle(part);

    return Padding(
      padding: const EdgeInsets.only(top: 8),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Container(
            width: double.infinity,
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
            decoration: BoxDecoration(
              color: t.n100.withValues(alpha: 0.5),
              border: Border.all(color: t.hair),
              borderRadius: BorderRadius.circular(8),
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    Container(
                      width: 8,
                      height: 8,
                      decoration: BoxDecoration(
                        color: _receiptColor(t, part.status),
                        shape: BoxShape.circle,
                      ),
                    ),
                    const SizedBox(width: 8),
                    if (title.isNotEmpty) ...[
                      Flexible(
                        child: Text(
                          title,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: TextStyle(
                            color: t.n800,
                            fontSize: FontSizes.sm,
                            fontWeight: FontWeight.w500,
                          ),
                        ),
                      ),
                      const SizedBox(width: 8),
                    ],
                    Text(
                      skillJobReceiptStatusLabel(i18n, part.status),
                      style: TextStyle(color: t.n500, fontSize: FontSizes.xs),
                    ),
                  ],
                ),
                if (part.summary.isNotEmpty)
                  Padding(
                    padding: const EdgeInsets.only(top: 4, left: 16),
                    child: Text(
                      part.summary,
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(color: t.n600, fontSize: FontSizes.xs),
                    ),
                  ),
              ],
            ),
          ),
          for (final artifact in part.artifacts)
            SkillJobReceiptArtifactPreview(artifact: artifact),
        ],
      ),
    );
  }
}

class SkillJobReceiptArtifactPreview extends ConsumerWidget {
  const SkillJobReceiptArtifactPreview({super.key, required this.artifact});

  final SkillJobArtifact artifact;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final assetId = artifact.assetId.trim();
    if (assetId.isEmpty) {
      return _ArtifactUnavailable(name: artifact.name);
    }

    final asset = ref.watch(assetUrlProvider(assetId));
    return asset.when(
      loading: () => _ArtifactLoading(name: _artifactName(artifact, null)),
      error: (_, _) =>
          _ArtifactUnavailable(name: _artifactName(artifact, null)),
      data: (info) {
        final name = _artifactName(artifact, info);
        if (info.url.trim().isEmpty) {
          return _ArtifactUnavailable(name: name);
        }
        final mime = artifact.mime?.trim().isNotEmpty == true
            ? artifact.mime
            : info.mime;
        final part = FilePart(
          id: 'skill-job-artifact-$assetId',
          path: name,
          mimeType: mime,
          assetId: assetId,
        );
        return switch (skillJobReceiptArtifactKind(mime)) {
          SkillJobReceiptArtifactKind.video => Padding(
            padding: const EdgeInsets.only(top: 8),
            child: AttachmentGallery(
              key: ValueKey('skill-job-artifact-video-$assetId'),
              parts: [part],
              hero: true,
            ),
          ),
          SkillJobReceiptArtifactKind.image => Padding(
            padding: const EdgeInsets.only(top: 8),
            child: AttachmentGallery(
              key: ValueKey('skill-job-artifact-image-$assetId'),
              parts: [part],
              hero: true,
            ),
          ),
          SkillJobReceiptArtifactKind.file => Padding(
            padding: const EdgeInsets.only(top: 6),
            child: FileChipRow(
              key: ValueKey('skill-job-artifact-file-$assetId'),
              assetId: assetId,
              name: name,
            ),
          ),
        };
      },
    );
  }
}

String _artifactName(SkillJobArtifact artifact, AssetUrl? asset) {
  final embedded = artifact.name.trim();
  if (embedded.isNotEmpty) return embedded;
  final resolved = asset?.name?.trim() ?? '';
  if (resolved.isNotEmpty) return resolved;
  return artifact.assetId.trim();
}

class _ArtifactLoading extends StatelessWidget {
  const _ArtifactLoading({required this.name});

  final String name;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Padding(
      padding: const EdgeInsets.only(top: 6),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          SizedBox(
            width: 12,
            height: 12,
            child: CircularProgressIndicator(strokeWidth: 1.5, color: t.n500),
          ),
          const SizedBox(width: 6),
          Flexible(
            child: Text(
              name,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(color: t.n600, fontSize: FontSizes.xs),
            ),
          ),
        ],
      ),
    );
  }
}

class _ArtifactUnavailable extends ConsumerWidget {
  const _ArtifactUnavailable({required this.name});

  final String name;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final unavailable = ref.watch(i18nProvider).t('common:state.unavailable');
    final label = name.trim();
    return Padding(
      padding: const EdgeInsets.only(top: 6),
      child: Text(
        label.isEmpty || label == unavailable
            ? unavailable
            : '$label · $unavailable',
        key: const ValueKey('skill-job-artifact-unavailable'),
        maxLines: 1,
        overflow: TextOverflow.ellipsis,
        style: TextStyle(color: t.n600, fontSize: FontSizes.xs),
      ),
    );
  }
}
