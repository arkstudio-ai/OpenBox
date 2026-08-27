import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../../shared/appearance/tokens.dart';
import '../../../../shared/appearance/type_scale.dart';
import '../../../../shared/i18n/i18n.dart';
import '../../../../shared/models/interaction.dart';
import '../../api/chat_api.dart';
import 'video_approval_detail.dart';

/// Blocking question prompt above the composer (web `QuestionDock`):
/// answers post as one label-array per question, in order.
class QuestionDock extends ConsumerStatefulWidget {
  const QuestionDock({super.key, required this.request});

  final QuestionRequest request;

  @override
  ConsumerState<QuestionDock> createState() => _QuestionDockState();
}

class _QuestionDockState extends ConsumerState<QuestionDock> {
  late final List<Set<String>> _selected =
      List.generate(widget.request.questions.length, (_) => <String>{});
  late final List<TextEditingController> _custom = List.generate(
      widget.request.questions.length, (_) => TextEditingController());
  bool _submitting = false;

  @override
  void dispose() {
    for (final controller in _custom) {
      controller.dispose();
    }
    super.dispose();
  }

  List<String> _answersFor(int index) {
    final custom = _custom[index].text.trim();
    return [
      ..._selected[index],
      if (custom.isNotEmpty) custom,
    ];
  }

  bool get _complete => List.generate(widget.request.questions.length, _answersFor)
      .every((answers) => answers.isNotEmpty);

  Future<void> _submit() async {
    if (!_complete || _submitting) return;
    setState(() => _submitting = true);
    try {
      await ref.read(chatApiProvider).replyQuestion(
            widget.request.id,
            List.generate(widget.request.questions.length, _answersFor),
          );
    } finally {
      if (mounted) setState(() => _submitting = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return Container(
      margin: const EdgeInsets.fromLTRB(12, 0, 12, 6),
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: t.card,
        borderRadius: BorderRadius.circular(Radii.xl),
        border: Border.all(color: t.hair),
        boxShadow: const [
          BoxShadow(color: Color(0x14000000), blurRadius: 8, offset: Offset(0, 2)),
        ],
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            i18n.t('chat:question.title'),
            style: TextStyle(
              fontSize: FontSizes.sm,
              fontWeight: FontWeight.w600,
              color: t.n800,
            ),
          ),
          const SizedBox(height: 8),
          ConstrainedBox(
            constraints: const BoxConstraints(maxHeight: 300),
            child: SingleChildScrollView(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  for (final (index, question)
                      in widget.request.questions.indexed)
                    _question(t, i18n, index, question),
                ],
              ),
            ),
          ),
          const SizedBox(height: 10),
          Row(
            children: [
              FilledButton(
                onPressed: _complete && !_submitting ? _submit : null,
                style: FilledButton.styleFrom(
                  backgroundColor: t.ink,
                  foregroundColor: t.bg,
                  disabledBackgroundColor: t.ink.withValues(alpha: 0.4),
                  disabledForegroundColor: t.bg,
                  padding:
                      const EdgeInsets.symmetric(horizontal: 16, vertical: 6),
                  shape: RoundedRectangleBorder(
                    borderRadius: BorderRadius.circular(Radii.full),
                  ),
                ),
                child: Text(i18n.t('chat:question.submit'),
                    style: const TextStyle(fontSize: FontSizes.sm)),
              ),
              const SizedBox(width: 8),
              TextButton(
                onPressed: () =>
                    ref.read(chatApiProvider).rejectQuestion(widget.request.id),
                child: Text(
                  i18n.t('chat:question.reject'),
                  style: TextStyle(fontSize: FontSizes.sm, color: t.n600),
                ),
              ),
              const Spacer(),
              if (!_complete && widget.request.questions.length > 1)
                Text(
                  i18n.t('chat:question.needAll',
                      vars: {'count': widget.request.questions.length}),
                  style: TextStyle(fontSize: FontSizes.xs, color: t.n500),
                ),
            ],
          ),
        ],
      ),
    );
  }

  Widget _question(
      BossipTokens t, I18nState i18n, int index, QuestionItem question) {
    final selected = _selected[index];
    return Padding(
      padding: const EdgeInsets.only(bottom: 10),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          if (question.header != null && question.header!.isNotEmpty)
            Text(
              question.header!,
              style: TextStyle(
                fontSize: FontSizes.xs2,
                fontWeight: FontWeight.w600,
                letterSpacing: 0.4,
                color: t.n500,
              ),
            ),
          Text(
            question.question,
            style: TextStyle(fontSize: FontSizes.base, color: t.ink, height: 1.5),
          ),
          VideoApprovalDetail(item: question),
          const SizedBox(height: 6),
          Wrap(
            spacing: 6,
            runSpacing: 6,
            children: [
              for (final option in question.options)
                ChoiceChip(
                  label: Text(option.label,
                      style: const TextStyle(fontSize: FontSizes.sm)),
                  selected: selected.contains(option.label),
                  showCheckmark: false,
                  selectedColor: t.a200,
                  backgroundColor: t.bg,
                  side: BorderSide(
                    color: selected.contains(option.label) ? t.a700 : t.hair,
                  ),
                  labelStyle: TextStyle(color: t.ink),
                  onSelected: (on) => setState(() {
                    if (!question.multiple) selected.clear();
                    if (on) {
                      selected.add(option.label);
                    } else {
                      selected.remove(option.label);
                    }
                  }),
                ),
            ],
          ),
          if (question.custom) ...[
            const SizedBox(height: 6),
            TextField(
              controller: _custom[index],
              onChanged: (_) => setState(() {}),
              style: TextStyle(fontSize: FontSizes.sm, color: t.ink),
              decoration: InputDecoration(
                hintText: i18n.t('chat:question.answer'),
                hintStyle: TextStyle(fontSize: FontSizes.sm, color: t.n500),
                isDense: true,
                contentPadding:
                    const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
                enabledBorder: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(Radii.md),
                  borderSide: BorderSide(color: t.hair),
                ),
                focusedBorder: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(Radii.md),
                  borderSide: BorderSide(color: t.accent),
                ),
              ),
            ),
          ],
        ],
      ),
    );
  }
}
