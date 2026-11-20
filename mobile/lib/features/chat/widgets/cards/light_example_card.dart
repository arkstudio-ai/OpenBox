import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../../shared/appearance/tokens.dart';
import '../../../../shared/appearance/type_scale.dart';
import '../../../../shared/i18n/i18n.dart';
import '../../../../shared/models/interaction.dart';
import '../../../../shared/models/light.dart';
import '../../../../shared/widgets/media_viewer.dart';
import '../../../../shared/widgets/sample_player.dart';
import '../../../light/api/light_api.dart';
import '../../cards/server_card.dart';

/// The 挑样例 card of a guided Light conversation (backend
/// `tool/example_pick.py`, kind `light_example_pick`): up to three finished
/// pieces from the example library side by side, each with 「就照这个做」, and
/// the card's cancel option underneath.
///
/// Presentation only, like [LightQuestionCard]: [QuestionDock] owns the
/// reply and hands it in as [onAnswer], which takes the option's label (a
/// server card always answers by label).
class LightExampleCard extends ConsumerStatefulWidget {
  const LightExampleCard({
    super.key,
    required this.card,
    required this.onAnswer,
    this.submitError,
  });

  static const kind = 'light_example_pick';

  /// The example card [request] carries, or null when it is some other card
  /// or has nothing to show (the plain card then answers it by label).
  static ServerCard? of(QuestionRequest request) {
    final card = ServerCard.of(request);
    if (card == null || card.kind != kind) return null;
    final examples = card.options.where((option) => !option.cancel);
    if (examples.isEmpty) return null;
    if (examples.any((o) => LightExamplePreview.fromJson(o.extra) == null)) {
      return null;
    }
    return card;
  }

  final ServerCard card;

  /// Answers with an option label. Null while a reply is in flight.
  final ValueChanged<String>? onAnswer;
  final String? submitError;

  @override
  ConsumerState<LightExampleCard> createState() => _LightExampleCardState();
}

class _LightExampleCardState extends ConsumerState<LightExampleCard> {
  /// Links fetched again after the card's own stopped loading, by example id.
  final _fresh = <String, LightExamplePreview>{};

  /// Examples whose links were already fetched again: once each.
  final _refreshed = <String>{};

  /// A still failed to load, most likely because its signed link expired.
  /// Called from the image's error builder, so nothing is rebuilt until the
  /// fresh links are here; the placeholder holds the tile meanwhile.
  Future<void> _posterFailed(String exampleId) async {
    if (!_refreshed.add(exampleId)) return;
    try {
      final preview = await ref
          .read(lightApiProvider)
          .examplePreview(exampleId);
      if (!mounted || preview.posterUrl == null) return;
      setState(() => _fresh[exampleId] = preview);
    } catch (_) {
      // Gone or offline: the placeholder stays and the tile can still be chosen.
    }
  }

  void _open(LightExamplePreview preview) {
    final url = preview.mediaUrl ?? preview.posterUrl;
    if (url == null) return;
    final i18n = ref.read(i18nProvider);
    if (preview.isVideo && preview.mediaUrl != null) {
      showSamplePlayer(
        context,
        url,
        failedText: i18n.t('light:gallery.playFailed'),
        title: preview.title,
      );
    } else {
      final failedText = i18n.t('light:example.viewFailed');
      showMediaViewer(
        context,
        (_) => MediaViewer(
          title: preview.title,
          child: MediaPreviewImage(url: url, failedText: failedText),
        ),
      );
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final card = widget.card;
    final examples = [
      for (final option in card.options)
        if (!option.cancel) option,
    ];
    final declines = [
      for (final option in card.options)
        if (option.cancel) option,
    ];
    final onAnswer = widget.onAnswer;
    return Container(
      key: const Key('light-example-card'),
      margin: const EdgeInsets.fromLTRB(12, 0, 12, 6),
      padding: const EdgeInsets.fromLTRB(16, 14, 16, 8),
      decoration: BoxDecoration(
        color: t.card,
        borderRadius: BorderRadius.circular(Radii.xl2),
        border: Border.all(color: t.hair),
        boxShadow: const [
          BoxShadow(
            color: Color(0x14000000),
            blurRadius: 10,
            offset: Offset(0, 3),
          ),
        ],
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          if (card.header.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(bottom: 8),
              child: Text(
                card.header,
                style: TextStyle(
                  fontSize: FontSizes.xs,
                  fontWeight: FontWeight.w600,
                  letterSpacing: 0.4,
                  color: t.n500,
                ),
              ),
            ),
          Text(
            card.question,
            style: TextStyle(
              fontSize: FontSizes.lg,
              fontWeight: FontWeight.w600,
              height: 1.4,
              color: t.ink,
            ),
          ),
          Padding(
            padding: const EdgeInsets.only(top: 2, bottom: 12),
            child: Text(
              i18n.t('light:example.view'),
              style: TextStyle(fontSize: FontSizes.xs, color: t.n500),
            ),
          ),
          // Equal columns whatever the titles' line counts, so the three
          // buttons sit on one line.
          IntrinsicHeight(
            child: Row(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                for (final (index, option) in examples.indexed) ...[
                  if (index > 0) const SizedBox(width: 8),
                  Expanded(
                    child: _ExampleColumn(
                      option: option,
                      preview:
                          _fresh[option.id] ??
                          LightExamplePreview.fromJson(option.extra)!,
                      shade: index,
                      useLabel: i18n.t('light:example.use'),
                      lastPickLabel: i18n.t('light:example.lastPick'),
                      viewLabel: i18n.t('light:example.view'),
                      onOpen: _open,
                      onPosterFailed: _posterFailed,
                      onUse: onAnswer == null
                          ? null
                          : () => onAnswer(option.label),
                    ),
                  ),
                ],
              ],
            ),
          ),
          const SizedBox(height: 4),
          for (final option in declines)
            TextButton(
              key: Key('light-example-decline-${option.id}'),
              onPressed: onAnswer == null ? null : () => onAnswer(option.label),
              style: TextButton.styleFrom(
                foregroundColor: t.n700,
                minimumSize: const Size.fromHeight(44),
              ),
              child: Text(
                option.label,
                textAlign: TextAlign.center,
                style: const TextStyle(fontSize: FontSizes.base),
              ),
            ),
          if (widget.submitError != null)
            Padding(
              padding: const EdgeInsets.only(bottom: 6),
              child: Text(
                widget.submitError!,
                style: TextStyle(color: t.danger, fontSize: FontSizes.xs),
              ),
            ),
        ],
      ),
    );
  }
}

/// One example: its still, its title, its button.
class _ExampleColumn extends StatelessWidget {
  const _ExampleColumn({
    required this.option,
    required this.preview,
    required this.shade,
    required this.useLabel,
    required this.lastPickLabel,
    required this.viewLabel,
    required this.onOpen,
    required this.onPosterFailed,
    required this.onUse,
  });

  final ServerCardOption option;
  final LightExamplePreview preview;
  final int shade;
  final String useLabel;
  final String lastPickLabel;
  final String viewLabel;
  final ValueChanged<LightExamplePreview> onOpen;
  final ValueChanged<String> onPosterFailed;
  final VoidCallback? onUse;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final poster = preview.posterUrl;
    // Neutral while the still downloads, and for good when it never comes.
    final blank = ColoredBox(
      color: [t.n200, t.n300, t.n200][shade % 3],
      child: Center(
        child: Icon(
          preview.isVideo ? Icons.movie_outlined : Icons.image_outlined,
          color: t.n500,
          size: 24,
        ),
      ),
    );
    final lastPick = option.extra['last_pick'] == true;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Semantics(
          button: true,
          label: viewLabel,
          child: GestureDetector(
            key: Key('light-example-tile-${option.id}'),
            behavior: HitTestBehavior.opaque,
            onTap: () => onOpen(preview),
            child: AspectRatio(
              aspectRatio: 3 / 4,
              child: ClipRRect(
                borderRadius: BorderRadius.circular(Radii.md),
                child: Stack(
                  fit: StackFit.expand,
                  children: [
                    if (poster == null)
                      blank
                    else
                      Image.network(
                        poster,
                        // A new link is a new picture, not the failed one.
                        key: ValueKey(poster),
                        fit: BoxFit.cover,
                        frameBuilder: (_, child, frame, sync) =>
                            sync || frame != null ? child : blank,
                        errorBuilder: (_, _, _) {
                          onPosterFailed(preview.exampleId);
                          return blank;
                        },
                      ),
                    if (preview.isVideo)
                      Center(
                        child: Icon(
                          Icons.play_circle_fill,
                          key: Key('light-example-play-${option.id}'),
                          color: t.mediaLabel,
                          size: 30,
                          shadows: [
                            Shadow(blurRadius: 8, color: t.mediaShadow),
                          ],
                        ),
                      ),
                    if (lastPick)
                      PositionedDirectional(
                        top: 6,
                        start: 6,
                        end: 6,
                        child: Align(
                          alignment: AlignmentDirectional.topStart,
                          child: Container(
                            key: Key('light-example-last-${option.id}'),
                            padding: const EdgeInsets.symmetric(
                              horizontal: 6,
                              vertical: 2,
                            ),
                            decoration: BoxDecoration(
                              color: t.a100,
                              borderRadius: BorderRadius.circular(Radii.full),
                            ),
                            child: Text(
                              lastPickLabel,
                              maxLines: 1,
                              overflow: TextOverflow.ellipsis,
                              style: TextStyle(
                                fontSize: FontSizes.xs2,
                                fontWeight: FontWeight.w600,
                                color: t.a800,
                              ),
                            ),
                          ),
                        ),
                      ),
                  ],
                ),
              ),
            ),
          ),
        ),
        Padding(
          padding: const EdgeInsets.only(top: 6, bottom: 8),
          child: Text(
            option.label,
            maxLines: 2,
            overflow: TextOverflow.ellipsis,
            style: TextStyle(
              fontSize: FontSizes.sm,
              fontWeight: FontWeight.w600,
              height: 1.35,
              color: t.ink,
            ),
          ),
        ),
        const Spacer(),
        FilledButton(
          key: Key('light-example-use-${option.id}'),
          onPressed: onUse,
          style: FilledButton.styleFrom(
            backgroundColor: t.a700,
            foregroundColor: t.bg,
            disabledBackgroundColor: t.n300,
            disabledForegroundColor: t.n600,
            minimumSize: const Size.fromHeight(40),
            padding: const EdgeInsets.symmetric(horizontal: 6),
            shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(Radii.full),
            ),
          ),
          // Three columns on a 360 dp phone leave about 96 dp a button; the
          // longer translations shrink to fit instead of wrapping.
          child: FittedBox(
            fit: BoxFit.scaleDown,
            child: Text(
              useLabel,
              maxLines: 1,
              style: const TextStyle(
                fontSize: FontSizes.sm,
                fontWeight: FontWeight.w600,
              ),
            ),
          ),
        ),
      ],
    );
  }
}
