import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/utils/error_text.dart';
import 'production_api.dart';
import 'production_media.dart';
import 'production_widgets.dart';

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
                },
                size: 42,
                color: t.a700,
              ),
            const SizedBox(height: 24),
            Text(
              i18n.t('light:production.phase.${task.phase ?? 'agent'}'),
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
              color: t.a700,
              backgroundColor: t.n300,
              minHeight: 8,
              borderRadius: BorderRadius.circular(8),
            ),
            if (task.template.isNotEmpty) ...[
              const SizedBox(height: 17),
              Text(
                productionTemplateLabel(i18n, task.template),
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
    return ListView(
      padding: const EdgeInsets.fromLTRB(16, 12, 16, 24),
      children: [
        ProductionHeading(i18n.t('light:production.done.${task.kind.id}')),
        if (task.template.isNotEmpty) ...[
          const SizedBox(height: 10),
          Align(
            alignment: Alignment.centerLeft,
            child: ProductionTag(
              text: productionTemplateLabel(i18n, task.template),
            ),
          ),
        ],
        const SizedBox(height: 18),
        Center(
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 276),
            child: AspectRatio(
              aspectRatio: media.video ? 9 / 16 : 3 / 4,
              child: InkWell(
                borderRadius: BorderRadius.circular(12),
                key: const Key('production-result-preview'),
                onTap: () => openProductionMedia(context, ref, media),
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
        else
          Text(
            i18n.t('light:production.decision.${task.status}Body'),
            style: TextStyle(color: t.n800, height: 1.4),
          ),
      ],
    );
  }
}
