import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/message_part.dart';
import '../../../shared/router/paths.dart';
import '../utils/content_origin.dart';

class AiGeneratedLabel extends ConsumerWidget {
  const AiGeneratedLabel({super.key, this.onMedia = false});

  final bool onMedia;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    return Text(
      ref.watch(i18nProvider).t('chat:aigc.label'),
      style: TextStyle(
        fontSize: FontSizes.sm,
        fontWeight: onMedia ? FontWeight.w500 : FontWeight.normal,
        color: onMedia ? t.mediaLabel.withValues(alpha: 0.72) : t.n700,
        letterSpacing: onMedia ? 0.5 : null,
        shadows: onMedia
            ? [
                Shadow(
                  color: t.mediaShadow,
                  offset: const Offset(0, 1),
                  blurRadius: 2,
                ),
                Shadow(color: t.mediaShadow, blurRadius: 1),
              ]
            : null,
      ),
    );
  }
}

/// A preview overlay; this does not modify the downloaded asset's bytes.
class AiMediaOverlay extends StatelessWidget {
  const AiMediaOverlay({
    super.key,
    required this.part,
    required this.child,
    this.bottom = 12,
    this.artifactKind,
  });

  final FilePart part;
  final Widget child;
  final double bottom;
  final String? artifactKind;

  @override
  Widget build(BuildContext context) {
    if (!isGeneratedMedia(part, artifactKind: artifactKind)) return child;
    return Stack(
      fit: StackFit.passthrough,
      children: [
        child,
        PositionedDirectional(
          start: 12,
          end: 12,
          bottom: bottom,
          child: const IgnorePointer(
            child: FittedBox(
              fit: BoxFit.scaleDown,
              alignment: AlignmentDirectional.centerEnd,
              child: AiGeneratedLabel(onMedia: true),
            ),
          ),
        ),
      ],
    );
  }
}

class AiDisclosure extends ConsumerWidget {
  const AiDisclosure({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    void showInfo() => context.push(Paths.legalDocument('ai'));
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 0, 16, 4),
      child: Wrap(
        alignment: WrapAlignment.center,
        crossAxisAlignment: WrapCrossAlignment.center,
        spacing: 8,
        children: [
          Text(
            i18n.t('chat:aigc.notice'),
            textAlign: TextAlign.center,
            style: TextStyle(
              fontSize: FontSizes.xs,
              height: 1.5,
              color: t.n700,
            ),
          ),
          Tooltip(
            message: i18n.t('chat:aigc.serviceTitle'),
            child: TextButton(
              onPressed: showInfo,
              style: TextButton.styleFrom(
                foregroundColor: t.n700,
                minimumSize: const Size(0, 24),
                padding: const EdgeInsets.symmetric(vertical: 3),
                tapTargetSize: MaterialTapTargetSize.shrinkWrap,
                textStyle: const TextStyle(
                  fontSize: FontSizes.xs,
                  height: 1.5,
                  decoration: TextDecoration.underline,
                ),
              ),
              child: Text(i18n.t('chat:aigc.serviceTitle')),
            ),
          ),
        ],
      ),
    );
  }
}
