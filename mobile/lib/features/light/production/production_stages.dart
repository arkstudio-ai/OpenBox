import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/api_error.dart';
import '../../../shared/appearance/tokens.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/utils/error_text.dart';
import 'production_api.dart';
import 'production_media.dart';
import 'production_voice.dart'
    show productionVoicesProvider, savedVoiceName, voiceLabel;
import 'production_widgets.dart';

String productionErrorText(I18nState i18n, Object error) {
  final api = apiErrorOf(error);
  final voiceKey = switch (api?.code) {
    'PRODUCTION_VOICE_UNAVAILABLE' => 'voiceUnavailable',
    'PRODUCTION_VOICE_SAMPLE' => 'sampleError',
    'PRODUCTION_VOICE_CONSENT' => 'consent',
    'INSUFFICIENT_CREDITS' => 'needCredits',
    'PRODUCTION_VOICE_PROVIDER' ||
    'PRODUCTION_VOICE_CHANGED' => 'providerError',
    'PRODUCTION_NARRATION_PLAN' => 'planError',
    'PRODUCTION_NARRATION_DETECTION' => 'speechUnknown',
    'PRODUCTION_NARRATION_PREVIEW' => 'previewError',
    'PRODUCTION_NARRATION_CONFIRM' ||
    'PRODUCTION_NARRATION_STATE' => 'confirmError',
    _ => null,
  };
  if (voiceKey != null) return i18n.t('light:production.voice.$voiceKey');
  if (api?.code == 'LIGHT_VIDEO_TIER_UNAVAILABLE') {
    return i18n.t('light:production.videoTier.manualUnavailable');
  }
  if (api?.code == 'LIGHT_VIDEO_FRAME_PLAN') {
    return i18n.t('light:production.videoTier.prepareFrames');
  }
  if (api?.code == 'LIGHT_PRODUCTION_TEMPLATE_RETIRED') {
    return i18n.t('light:production.retiredTemplate');
  }
  if (api?.code == 'LIGHT_PRODUCTION_POSTER') {
    return i18n.t('light:production.poster.titleRequired');
  }
  if (api?.code == 'LIGHT_PRODUCTION_EXAMPLE_UNAVAILABLE') {
    return i18n.t('light:production.examples.unavailable');
  }
  if (api?.code == 'LIGHT_REMAKE_VIDEO_LIMIT') {
    return i18n.t(
      'light:production.videoLimit',
      vars: {'needed': api!.meta['needed'], 'remaining': api.meta['remaining']},
    );
  }
  return errorText(i18n, error);
}

class ProductionRemakeCopy extends ConsumerWidget {
  const ProductionRemakeCopy({
    super.key,
    required this.controller,
    required this.enabled,
    required this.task,
    required this.onChanged,
  });

  final TextEditingController controller;
  final bool enabled;
  final LightProduction task;
  final ValueChanged<String> onChanged;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final i18n = ref.watch(i18nProvider);
    final t = context.tokens;
    final productReference = task.template == productStory;
    final gallery = task.template == galleryWishes;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          i18n.t(
            gallery
                ? 'light:production.template.gallery_wishes.copyTitle'
                : 'light:production.referenceTitle',
          ),
          style: TextStyle(color: t.ink, fontWeight: FontWeight.w700),
        ),
        const SizedBox(height: 5),
        Text(
          i18n.t(
            productReference
                ? 'light:production.template.product_story.referenceHelp'
                : gallery
                ? 'light:production.template.gallery_wishes.copyHelp'
                : task.wanRemake
                ? 'light:production.wanReferenceHelp'
                : 'light:production.referenceHelp',
          ),
          style: TextStyle(color: t.n800, height: 1.4),
        ),
        const SizedBox(height: 10),
        ProductionCopyField(
          controller: controller,
          enabled: enabled,
          hint: i18n.t(
            productReference
                ? 'light:production.template.product_story.referencePlaceholder'
                : gallery
                ? 'light:production.template.gallery_wishes.copyPlaceholder'
                : 'light:production.referencePlaceholder',
          ),
          minLines: 3,
          maxLength: gallery ? 2520 : 2400,
          onChanged: onChanged,
        ),
      ],
    );
  }
}

class ProductionRemakeScript extends ConsumerWidget {
  const ProductionRemakeScript(this.script, {super.key});

  final String script;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final i18n = ref.watch(i18nProvider);
    final t = context.tokens;
    return ExpansionTile(
      key: const Key('production-remake-script'),
      tilePadding: EdgeInsets.zero,
      title: Text(
        i18n.t('light:production.remakeScript'),
        style: TextStyle(color: t.ink, fontWeight: FontWeight.w700),
      ),
      expandedAlignment: Alignment.centerLeft,
      children: [
        SelectableText(script, style: TextStyle(color: t.n800, height: 1.5)),
      ],
    );
  }
}

class ProductionLoading extends ConsumerWidget {
  const ProductionLoading({super.key, this.error, required this.onRetry});

  final Object? error;
  final VoidCallback onRetry;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final i18n = ref.watch(i18nProvider);
    return Center(
      child: error == null
          ? const CircularProgressIndicator()
          : Padding(
              padding: const EdgeInsets.all(24),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Text(errorText(i18n, error!), textAlign: TextAlign.center),
                  TextButton(
                    onPressed: onRetry,
                    child: Text(i18n.t('common:action.retry')),
                  ),
                ],
              ),
            ),
    );
  }
}

class ProductionProgress extends ConsumerWidget {
  const ProductionProgress({super.key, required this.task});

  final LightProduction task;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final i18n = ref.watch(i18nProvider);
    final t = context.tokens;
    final progress = task.shotProgress;
    final style = productionStyleLabel(i18n, task);
    final phase = progress?.phase ?? task.phase ?? 'agent';
    final shots = phase == 'wan_shots' || phase == 'wan_edit' ? progress : null;
    final silkRoad = task.template == 'silk_road';
    final phaseLabel = silkRoad
        ? switch (phase) {
            'wan_shots' => 'silk_shots',
            'wan_edit' => 'silk_edit',
            'wan_delivery' => 'silk_delivery',
            _ => phase,
          }
        : phase;
    return Center(
      child: SingleChildScrollView(
        padding: const EdgeInsets.symmetric(horizontal: 32, vertical: 28),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            if (task.materials.isNotEmpty)
              ProductionMediaStrip(media: task.materials, height: 120)
            else
              Icon(
                switch (task.kind) {
                  ProductionKind.video => Icons.videocam_outlined,
                  ProductionKind.footage => Icons.movie_outlined,
                  ProductionKind.menu => Icons.restaurant_menu,
                  ProductionKind.poster => Icons.image_outlined,
                },
                size: 42,
                color: t.a700,
              ),
            const SizedBox(height: 24),
            Text(
              i18n.t('light:production.phase.$phaseLabel'),
              textAlign: TextAlign.center,
              style: TextStyle(
                fontSize: 25,
                fontWeight: FontWeight.w800,
                color: t.ink,
              ),
            ),
            const SizedBox(height: 28),
            LinearProgressIndicator(
              key: const Key('production-progress'),
              value: shots == null
                  ? null
                  : (shots.filmed + shots.reviewed) / (2 * shots.total),
              color: t.a700,
              backgroundColor: t.n300,
              minHeight: 8,
              borderRadius: BorderRadius.circular(8),
            ),
            if (shots != null) ...[
              const SizedBox(height: 17),
              Text(
                i18n.t(
                  silkRoad
                      ? 'light:production.silkShotCounts'
                      : 'light:production.shotCounts',
                  vars: {
                    'filmed': shots.filmed,
                    'reviewed': shots.reviewed,
                    'total': shots.total,
                  },
                ),
                textAlign: TextAlign.center,
                style: TextStyle(color: t.n800),
              ),
            ] else if (style.isNotEmpty) ...[
              const SizedBox(height: 17),
              Text(style, style: TextStyle(color: t.n800)),
            ],
            if (progress != null && progress.retaking > 0) ...[
              const SizedBox(height: 8),
              Text(
                i18n.t(
                  'light:production.retaking',
                  vars: {'count': progress.retaking},
                ),
                textAlign: TextAlign.center,
                style: TextStyle(color: t.n800),
              ),
            ],
          ],
        ),
      ),
    );
  }
}

class ProductionResult extends ConsumerWidget {
  const ProductionResult({super.key, required this.task});

  final LightProduction task;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final i18n = ref.watch(i18nProvider);
    final media = task.output!;
    final aspectRatio = media.video ? (media.aspectRatio ?? 9 / 16) : 3 / 4;
    final style = productionStyleLabel(i18n, task);
    final voiced =
        task.kind == ProductionKind.footage &&
        task.redoNarration &&
        task.voiceId.isNotEmpty;
    final voices = voiced && task.voiceId.startsWith('voice:')
        ? ref.watch(productionVoicesProvider).valueOrNull
        : null;
    return ListView(
      padding: const EdgeInsets.fromLTRB(16, 12, 16, 24),
      children: [
        ProductionHeading(i18n.t('light:production.done.${task.kind.id}')),
        if (style.isNotEmpty) ...[
          const SizedBox(height: 10),
          Align(
            alignment: Alignment.centerLeft,
            child: ProductionTag(text: style),
          ),
        ],
        if (voiced) ...[
          const SizedBox(height: 10),
          Text(
            i18n.t(
              'light:production.voice.doneVoice',
              vars: {
                'voice': voiceLabel(
                  i18n,
                  task.voiceId,
                  name: savedVoiceName(voices, task.voiceId),
                ),
              },
            ),
          ),
        ],
        const SizedBox(height: 18),
        Center(
          child: ConstrainedBox(
            constraints: BoxConstraints(maxWidth: aspectRatio > 1 ? 480 : 276),
            child: AspectRatio(
              aspectRatio: aspectRatio,
              child: ProductionResultPreviewButton(
                media: media,
                child: Stack(
                  fit: StackFit.expand,
                  children: [
                    ProductionThumbnail(
                      media: media,
                      fit: media.video ? BoxFit.cover : BoxFit.contain,
                      showVideoBadge: false,
                    ),
                    if (media.video)
                      const Center(
                        child: Icon(
                          Icons.play_circle_fill,
                          color: Colors.white,
                          size: 52,
                        ),
                      ),
                  ],
                ),
              ),
            ),
          ),
        ),
      ],
    );
  }
}

class ProductionDecision extends ConsumerWidget {
  const ProductionDecision({super.key, required this.task});

  final LightProduction task;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final i18n = ref.watch(i18nProvider);
    final proposal = task.proposal;
    final t = context.tokens;
    final changes = proposal?['changes'];
    final template = changes is Map ? changes['template'] : null;
    final script = changes is Map ? changes['script'] : null;
    final notes = changes is Map ? changes['notes'] : null;
    final feedback = task.reviewFeedback;
    return ListView(
      padding: const EdgeInsets.fromLTRB(16, 12, 16, 24),
      children: [
        ProductionHeading(i18n.t('light:production.decision.${task.status}')),
        const SizedBox(height: 16),
        if (proposal != null)
          Card(
            color: t.card,
            margin: EdgeInsets.zero,
            child: Padding(
              padding: const EdgeInsets.all(16),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(proposal['summary'] as String? ?? ''),
                  if (template is String && template.isNotEmpty) ...[
                    const SizedBox(height: 10),
                    Text(
                      productionTemplateLabel(i18n, template),
                      style: const TextStyle(fontWeight: FontWeight.w700),
                    ),
                  ],
                  if (script is String && script.isNotEmpty) ...[
                    const SizedBox(height: 10),
                    Text(script),
                  ],
                  if (notes is String && notes.isNotEmpty) ...[
                    const SizedBox(height: 10),
                    Text(notes),
                  ],
                ],
              ),
            ),
          )
        else if (feedback['reason'] case final String reason)
          Card(
            color: t.card,
            margin: EdgeInsets.zero,
            child: Padding(
              padding: const EdgeInsets.all(16),
              child: Column(
                key: const Key('production-review-feedback'),
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  if (feedback['shot'] case final int number)
                    Text(
                      i18n.t(
                        'light:production.decision.repairShot',
                        vars: {'number': number.toString()},
                      ),
                      style: const TextStyle(fontWeight: FontWeight.w700),
                    ),
                  const SizedBox(height: 8),
                  Text(reason),
                  if (feedback['correction'] case final String correction) ...[
                    const SizedBox(height: 8),
                    Text(correction),
                  ],
                ],
              ),
            ),
          )
        else
          Text(
            i18n.t('light:production.decision.${task.status}Body'),
            style: TextStyle(color: t.n800, height: 1.4),
          ),
      ],
    );
  }
}
