import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../../shared/appearance/tokens.dart';
import '../../../../shared/appearance/type_scale.dart';
import '../../../../shared/i18n/i18n.dart';
import '../../../../shared/models/interaction.dart';

/// A question card put away with 「稍后再说」: one row saying what it asks and
/// that it will lapse. Tapping it opens the card again, so it can still be
/// answered until the server expires it.
class FoldedQuestionRow extends ConsumerStatefulWidget {
  const FoldedQuestionRow({
    super.key,
    required this.request,
    required this.onExpand,
    this.expiresAt,
  });

  final QuestionRequest request;
  final VoidCallback onExpand;

  /// When the server will expire the card, if known: shown as minutes left.
  final DateTime? expiresAt;

  @override
  ConsumerState<FoldedQuestionRow> createState() => _FoldedQuestionRowState();
}

final _markup = RegExp(r'^[#>\-*\s]+|[*_`]+');

class _FoldedQuestionRowState extends ConsumerState<FoldedQuestionRow> {
  Timer? _tick;

  @override
  void initState() {
    super.initState();
    // Only the minutes left change while the row sits there.
    _tick = Timer.periodic(const Duration(seconds: 30), (_) {
      if (mounted && widget.expiresAt != null) setState(() {});
    });
  }

  @override
  void dispose() {
    _tick?.cancel();
    super.dispose();
  }

  /// The card's header, else the first line of what it asks.
  String get _title {
    for (final question in widget.request.questions) {
      final header = question.header?.trim() ?? '';
      if (header.isNotEmpty) return header;
      for (final line in question.question.split('\n')) {
        final text = line.replaceAll(_markup, '').trim();
        if (text.isNotEmpty) return text;
      }
    }
    return '';
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final left = widget.expiresAt?.difference(DateTime.now());
    final hint = left != null && !left.isNegative
        ? i18n.t(
            'light:card.foldedMinutes',
            vars: {'minutes': (left.inSeconds / 60).ceil().clamp(1, 9999)},
          )
        : i18n.t('light:card.folded');
    final title = _title;
    return Padding(
      padding: const EdgeInsets.fromLTRB(12, 0, 12, 6),
      child: Semantics(
        button: true,
        hint: i18n.t('light:card.foldedExpand'),
        child: Material(
          color: t.card,
          shape: RoundedRectangleBorder(
            borderRadius: BorderRadius.circular(Radii.lg),
            side: BorderSide(color: t.hair),
          ),
          clipBehavior: Clip.antiAlias,
          child: InkWell(
            key: const Key('question-folded-row'),
            onTap: widget.onExpand,
            child: ConstrainedBox(
              constraints: const BoxConstraints(minHeight: 48),
              child: Padding(
                padding: const EdgeInsets.fromLTRB(14, 8, 8, 8),
                child: Row(
                  children: [
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          if (title.isNotEmpty)
                            Text(
                              title,
                              maxLines: 1,
                              overflow: TextOverflow.ellipsis,
                              style: TextStyle(
                                fontSize: FontSizes.sm,
                                color: t.ink,
                              ),
                            ),
                          Text(
                            hint,
                            maxLines: 1,
                            overflow: TextOverflow.ellipsis,
                            style: TextStyle(
                              fontSize: FontSizes.xs,
                              color: t.n500,
                            ),
                          ),
                        ],
                      ),
                    ),
                    Icon(Icons.expand_more, size: 20, color: t.n500),
                  ],
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}
