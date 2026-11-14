import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../../shared/appearance/tokens.dart';
import '../../../../shared/appearance/type_scale.dart';
import '../../../../shared/i18n/i18n.dart';
import '../../../../shared/models/interaction.dart';
import '../../state/question_draft.dart';
import '../markdown_view.dart';
import 'desktop_takeover_detail.dart';
import 'question_resources.dart';
import 'video_approval_detail.dart';

/// The Light 模式 skin of a question card (docs `LIGHT_MODE_PLAN.md` §4.4):
/// the same question, drawn as one big chip per option so the answer is a
/// tap, with the text box and attachments below for when it is not.
///
/// Presentation only — [QuestionDock] keeps the draft, the paging and the
/// submit, and hands them in; this widget never touches the stores itself.
class LightQuestionCard extends ConsumerWidget {
  const LightQuestionCard({
    super.key,
    required this.request,
    required this.page,
    required this.draft,
    required this.custom,
    required this.onPick,
    required this.onCustom,
    required this.onSubmit,
    required this.onReject,
    required this.onPage,
    this.onLater,
    this.showLater = false,
    this.submitError,
  });

  final QuestionRequest request;
  final int page;
  final QuestionDraft draft;
  final TextEditingController custom;

  /// A chip tapped: (label, now selected). Null while a reply is in flight.
  final void Function(String label, bool on)? onPick;
  final ValueChanged<String> onCustom;
  final VoidCallback? onSubmit;
  final VoidCallback? onReject;

  /// 「稍后再说」: put the card away without answering. Shown when
  /// [showLater]; null while a reply is in flight.
  final VoidCallback? onLater;
  final bool showLater;
  final ValueChanged<int> onPage;
  final String? submitError;

  int get _count => request.questions.length;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final question = request.questions[page];
    final busy = draft.submitting || draft.transferring;
    final selected = draft.useCustom[page] ? <String>{} : draft.picked[page];
    final isLast = page == _count - 1;
    return Container(
      key: const Key('light-question-card'),
      margin: const EdgeInsets.fromLTRB(12, 0, 12, 6),
      padding: const EdgeInsets.fromLTRB(16, 14, 16, 14),
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
          if (_count > 1 || (question.header?.isNotEmpty ?? false))
            Padding(
              padding: const EdgeInsets.only(bottom: 8),
              child: Row(
                children: [
                  if (question.header?.isNotEmpty ?? false)
                    Expanded(
                      child: Text(
                        question.header!,
                        style: TextStyle(
                          fontSize: FontSizes.xs,
                          fontWeight: FontWeight.w600,
                          letterSpacing: 0.4,
                          color: t.n500,
                        ),
                      ),
                    )
                  else
                    const Spacer(),
                  if (_count > 1)
                    Text(
                      i18n.t(
                        'light:card.pageOf',
                        vars: {'current': page + 1, 'count': _count},
                      ),
                      style: TextStyle(fontSize: FontSizes.xs, color: t.n500),
                    ),
                ],
              ),
            ),
          MarkdownView(question.question),
          VideoApprovalDetail(item: question),
          DesktopTakeoverDetail(item: question, sessionId: request.sessionId),
          const SizedBox(height: 10),
          for (final option in question.options)
            Padding(
              padding: const EdgeInsets.only(bottom: 8),
              child: _BigChip(
                label: option.label,
                description: option.description,
                selected: selected.contains(option.label),
                onTap: onPick == null || busy
                    ? null
                    : () => onPick!(
                        option.label,
                        !selected.contains(option.label),
                      ),
              ),
            ),
          if (question.custom)
            Padding(
              padding: const EdgeInsets.only(top: 2, bottom: 8),
              child: TextField(
                controller: custom,
                enabled: !busy,
                maxLength: 5000,
                minLines: 1,
                maxLines: 4,
                textInputAction: isLast
                    ? TextInputAction.done
                    : TextInputAction.next,
                onTap: () => onCustom(custom.text),
                onChanged: onCustom,
                style: TextStyle(fontSize: FontSizes.base, color: t.ink),
                decoration: InputDecoration(
                  counterText: '',
                  hintText: i18n.t('light:card.typeHint'),
                  hintStyle: TextStyle(fontSize: FontSizes.sm, color: t.n500),
                  isDense: true,
                  filled: true,
                  fillColor: t.bg,
                  contentPadding: const EdgeInsets.symmetric(
                    horizontal: 14,
                    vertical: 12,
                  ),
                  enabledBorder: OutlineInputBorder(
                    borderRadius: BorderRadius.circular(Radii.lg),
                    borderSide: BorderSide(color: t.hair),
                  ),
                  focusedBorder: OutlineInputBorder(
                    borderRadius: BorderRadius.circular(Radii.lg),
                    borderSide: BorderSide(color: t.accent),
                  ),
                  disabledBorder: OutlineInputBorder(
                    borderRadius: BorderRadius.circular(Radii.lg),
                    borderSide: BorderSide(color: t.hair),
                  ),
                ),
              ),
            ),
          if (question.allowAttachments)
            QuestionResources(request: request, index: page),
          const SizedBox(height: 4),
          Row(
            children: [
              if (_count > 1 && page > 0)
                IconButton(
                  onPressed: busy ? null : () => onPage(page - 1),
                  icon: const Icon(Icons.chevron_left),
                  tooltip: i18n.t('chat:question.previous'),
                ),
              Expanded(
                child: FilledButton(
                  key: const Key('light-question-submit'),
                  onPressed: onSubmit,
                  style: FilledButton.styleFrom(
                    backgroundColor: t.a700,
                    foregroundColor: t.bg,
                    disabledBackgroundColor: t.n300,
                    disabledForegroundColor: t.n600,
                    minimumSize: const Size.fromHeight(48),
                    shape: RoundedRectangleBorder(
                      borderRadius: BorderRadius.circular(Radii.full),
                    ),
                  ),
                  child: Text(
                    i18n.t(
                      busy
                          ? 'chat:question.submitting'
                          : isLast
                          ? 'light:card.confirm'
                          : 'light:card.next',
                    ),
                    style: const TextStyle(
                      fontSize: FontSizes.lg,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                ),
              ),
              const SizedBox(width: 8),
              if (showLater)
                TextButton(
                  key: const Key('light-question-later'),
                  onPressed: onLater,
                  style: TextButton.styleFrom(
                    minimumSize: const Size(48, 48),
                    padding: const EdgeInsets.symmetric(horizontal: 8),
                  ),
                  child: Text(
                    i18n.t('chat:card.later'),
                    style: TextStyle(fontSize: FontSizes.sm, color: t.n600),
                  ),
                ),
              TextButton(
                onPressed: onReject,
                style: TextButton.styleFrom(
                  minimumSize: const Size(48, 48),
                  padding: const EdgeInsets.symmetric(horizontal: 8),
                ),
                child: Text(
                  i18n.t('light:card.skip'),
                  style: TextStyle(fontSize: FontSizes.sm, color: t.n600),
                ),
              ),
            ],
          ),
          if (submitError != null)
            Padding(
              padding: const EdgeInsets.only(top: 6),
              child: Text(
                submitError!,
                style: TextStyle(color: t.danger, fontSize: FontSizes.xs),
              ),
            ),
          if (draft.saveError != null)
            Padding(
              padding: const EdgeInsets.only(top: 6),
              child: Text(
                i18n.t(
                  draft.saveError == 'conflict'
                      ? 'chat:question.draftConflict'
                      : 'chat:question.draftFailed',
                ),
                style: TextStyle(color: t.n700, fontSize: FontSizes.xs),
              ),
            ),
        ],
      ),
    );
  }
}

/// The model's "(Recommended)" convention, in either script; the Light card
/// shows it as a small 推荐 mark instead of English in the label.
final _recommended = RegExp(
  r'\s*[（(](recommended|推荐)[)）]\s*$',
  caseSensitive: false,
);

/// One option, full width, tall enough for a thumb.
class _BigChip extends StatelessWidget {
  const _BigChip({
    required this.label,
    required this.selected,
    required this.onTap,
    this.description,
  });

  final String label;
  final String? description;
  final bool selected;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final hasDescription = description?.trim().isNotEmpty ?? false;
    final recommended = _recommended.hasMatch(label);
    final shown = recommended ? label.replaceFirst(_recommended, '') : label;
    return Material(
      color: selected ? t.a700 : t.bg,
      borderRadius: BorderRadius.circular(Radii.lg),
      child: InkWell(
        borderRadius: BorderRadius.circular(Radii.lg),
        onTap: onTap,
        child: Container(
          constraints: const BoxConstraints(minHeight: 50),
          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(Radii.lg),
            border: Border.all(color: selected ? t.a700 : t.hair),
          ),
          child: Row(
            children: [
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Text.rich(
                      TextSpan(
                        text: shown,
                        children: [
                          if (recommended)
                            WidgetSpan(
                              alignment: PlaceholderAlignment.middle,
                              child: _RecommendedMark(selected: selected),
                            ),
                        ],
                      ),
                      style: TextStyle(
                        fontSize: FontSizes.lg,
                        fontWeight: FontWeight.w600,
                        color: selected ? t.bg : t.ink,
                      ),
                    ),
                    if (hasDescription)
                      Padding(
                        padding: const EdgeInsets.only(top: 2),
                        child: Text(
                          description!,
                          style: TextStyle(
                            fontSize: FontSizes.sm,
                            height: 1.4,
                            color: selected ? t.a100 : t.n600,
                          ),
                        ),
                      ),
                  ],
                ),
              ),
              Icon(
                selected ? Icons.check_circle : Icons.circle_outlined,
                size: 20,
                color: selected ? t.bg : t.n400,
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _RecommendedMark extends ConsumerWidget {
  const _RecommendedMark({required this.selected});

  final bool selected;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    return Container(
      margin: const EdgeInsets.only(left: 8),
      padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 1),
      decoration: BoxDecoration(
        color: selected ? t.bg : t.a100,
        borderRadius: BorderRadius.circular(Radii.full),
      ),
      child: Text(
        ref.watch(i18nProvider).t('light:card.recommended'),
        style: TextStyle(
          fontSize: FontSizes.xs,
          fontWeight: FontWeight.w600,
          color: t.a800,
        ),
      ),
    );
  }
}
