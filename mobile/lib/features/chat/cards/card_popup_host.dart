import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/auth_store.dart';
import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../state/pending_store.dart';
import '../widgets/markdown_view.dart';
import 'card_answer.dart';
import 'card_registry.dart';
import 'server_card.dart';

/// Pops server cards that ask to be shown as a sheet (`detail.present ==
/// 'sheet'`) over whatever screen is open — including over the voice call
/// sheet, so a card the call is talking about is on screen while it is
/// discussed. Wraps the app above [VoiceCallHost] in `MaterialApp.builder`.
///
/// The pending card stays in its conversation as an ordinary question card:
/// "稍后再说" only folds the popup, it never answers. A card answered
/// anywhere else (the transcript, another device, by voice) leaves the
/// pending list and the popup goes with it.
class CardPopupHost extends ConsumerStatefulWidget {
  const CardPopupHost({super.key, required this.child});

  final Widget child;

  @override
  ConsumerState<CardPopupHost> createState() => _CardPopupHostState();
}

final _epoch = DateTime.fromMillisecondsSinceEpoch(0);

class _CardPopupHostState extends ConsumerState<CardPopupHost> {
  /// Folded by the person in this run of the app; not shown again.
  final _folded = <String>{};
  String? _busy;

  @override
  void initState() {
    super.initState();
    // Chat screens seed pending cards when they open; a card filed by a task
    // in the background must pop up without one.
    WidgetsBinding.instance.addPostFrameCallback((_) => _seed());
  }

  void _seed() {
    if (!mounted || ref.read(authProvider).user == null) return;
    unawaited(ref.read(pendingProvider.notifier).refreshAll());
  }

  ServerCard? _next(PendingState pending) {
    final cards =
        [
          for (final list in pending.questions.values)
            for (final request in list)
              if (ServerCard.of(request) case final card?)
                if (card.popsUp && !_folded.contains(request.id)) card,
        ]..sort(
          (a, b) => (a.request.createdAt ?? _epoch).compareTo(
            b.request.createdAt ?? _epoch,
          ),
        );
    return cards.isEmpty ? null : cards.first;
  }

  Future<void> _answer(ServerCard card, ServerCardOption? option) async {
    if (_busy != null) return;
    setState(() => _busy = card.request.id);
    final settled = await answerServerCard(ref, card, option);
    if (!mounted) return;
    setState(() {
      _busy = null;
      if (settled) _folded.add(card.request.id);
    });
  }

  @override
  Widget build(BuildContext context) {
    ref.listen(authProvider.select((s) => s.userId), (_, _) {
      _folded.clear();
      _seed();
    });
    final card = _next(ref.watch(pendingProvider));
    return Stack(
      children: [
        widget.child,
        if (card != null) ...[
          Positioned.fill(
            child: GestureDetector(
              key: const Key('card-popup-scrim'),
              behavior: HitTestBehavior.opaque,
              onTap: () => setState(() => _folded.add(card.request.id)),
              child: const ColoredBox(color: Color(0x66000000)),
            ),
          ),
          Align(
            alignment: Alignment.bottomCenter,
            child: _CardSheet(
              key: ValueKey(card.request.id),
              card: card,
              busy: _busy == card.request.id,
              onPick: (option) => _answer(card, option),
              onLater: () => setState(() => _folded.add(card.request.id)),
            ),
          ),
        ],
      ],
    );
  }
}

class _CardSheet extends ConsumerWidget {
  const _CardSheet({
    super.key,
    required this.card,
    required this.busy,
    required this.onPick,
    required this.onLater,
  });

  final ServerCard card;
  final bool busy;
  final ValueChanged<ServerCardOption> onPick;
  final VoidCallback onLater;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final renderer = cardRenderers[card.kind];
    final media = MediaQuery.of(context);
    return TweenAnimationBuilder<double>(
      tween: Tween(begin: 1, end: 0),
      duration: const Duration(milliseconds: 220),
      curve: Curves.easeOutCubic,
      builder: (context, shift, child) =>
          FractionalTranslation(translation: Offset(0, shift), child: child),
      child: Material(
        key: const Key('card-popup-sheet'),
        color: t.card,
        borderRadius: const BorderRadius.vertical(
          top: Radius.circular(Radii.xl2),
        ),
        child: ConstrainedBox(
          constraints: BoxConstraints(maxHeight: media.size.height * 0.8),
          child: SingleChildScrollView(
            padding: EdgeInsets.fromLTRB(20, 12, 20, 16 + media.padding.bottom),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              mainAxisSize: MainAxisSize.min,
              children: [
                Center(
                  child: Container(
                    width: 36,
                    height: 4,
                    decoration: BoxDecoration(
                      color: t.n300,
                      borderRadius: BorderRadius.circular(Radii.full),
                    ),
                  ),
                ),
                const SizedBox(height: 12),
                if (card.header.isNotEmpty)
                  Padding(
                    padding: const EdgeInsets.only(bottom: 6),
                    child: Text(
                      card.header,
                      style: TextStyle(
                        fontSize: FontSizes.lg,
                        fontWeight: FontWeight.w600,
                        color: t.ink,
                      ),
                    ),
                  ),
                MarkdownView(card.question),
                const SizedBox(height: 14),
                for (final option in card.options)
                  Padding(
                    padding: const EdgeInsets.only(bottom: 8),
                    child: _OptionTile(
                      option: option,
                      detail: renderer?.optionDetail?.call(context, option),
                      recommendedLabel: i18n.t('chat:card.recommended'),
                      onTap: busy ? null : () => onPick(option),
                    ),
                  ),
                if (busy)
                  const Padding(
                    padding: EdgeInsets.symmetric(vertical: 6),
                    child: Center(
                      child: SizedBox(
                        width: 18,
                        height: 18,
                        child: CircularProgressIndicator(strokeWidth: 2),
                      ),
                    ),
                  ),
                TextButton(
                  key: const Key('card-popup-later'),
                  onPressed: busy ? null : onLater,
                  child: Text(i18n.t('chat:card.later')),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class _OptionTile extends StatelessWidget {
  const _OptionTile({
    required this.option,
    required this.detail,
    required this.recommendedLabel,
    required this.onTap,
  });

  final ServerCardOption option;
  final Widget? detail;
  final String recommendedLabel;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final strong = !option.cancel;
    return Material(
      color: strong ? t.a100 : t.surface,
      borderRadius: BorderRadius.circular(Radii.lg),
      child: InkWell(
        key: Key('card-option-${option.id}'),
        borderRadius: BorderRadius.circular(Radii.lg),
        onTap: onTap,
        child: Container(
          padding: const EdgeInsets.fromLTRB(14, 12, 14, 12),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(Radii.lg),
            border: Border.all(color: strong ? t.a300 : t.hair),
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Expanded(
                    child: Text(
                      option.label,
                      style: TextStyle(
                        fontSize: FontSizes.base,
                        fontWeight: FontWeight.w600,
                        color: t.ink,
                      ),
                    ),
                  ),
                  if (option.recommended)
                    Text(
                      recommendedLabel,
                      style: TextStyle(fontSize: FontSizes.xs, color: t.a700),
                    ),
                ],
              ),
              if (option.description.isNotEmpty)
                Padding(
                  padding: const EdgeInsets.only(top: 2),
                  child: Text(
                    option.description,
                    style: TextStyle(fontSize: FontSizes.sm, color: t.n600),
                  ),
                ),
              ?detail,
            ],
          ),
        ),
      ),
    );
  }
}
