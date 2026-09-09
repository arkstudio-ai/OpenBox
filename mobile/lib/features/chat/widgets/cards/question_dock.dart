import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../../shared/appearance/tokens.dart';
import '../../../../shared/appearance/type_scale.dart';
import '../../../../shared/events/bus.dart';
import '../../../../shared/i18n/i18n.dart';
import '../../../../shared/models/interaction.dart';
import '../../../../shared/widgets/toast.dart';
import '../../api/chat_api.dart';
import '../../state/pending_store.dart';
import '../../state/question_draft.dart';
import 'video_approval_detail.dart';

/// Blocking question prompt at the end of the transcript (web `QuestionDock`):
/// answers post as one label-array per question, in order.
///
/// Everything that has to survive a rebuild lives in [questionDraftProvider],
/// not here: this card sits in a lazy list, so its element is discarded the
/// moment the rows above it change length or it scrolls out of view.
class QuestionDock extends ConsumerStatefulWidget {
  const QuestionDock({super.key, required this.request});

  final QuestionRequest request;

  @override
  ConsumerState<QuestionDock> createState() => _QuestionDockState();
}

class _QuestionDockState extends ConsumerState<QuestionDock> {
  late final List<TextEditingController> _custom;
  String? _submitError;

  int get _count => widget.request.questions.length;

  @override
  void initState() {
    super.initState();
    // Seeded from the draft so a card rebuilt after being scrolled away comes
    // back with what was already typed in it.
    final draft = ref
        .read(questionDraftProvider.notifier)
        .of(widget.request.id, _count);
    _custom = [
      for (final text in draft.custom)
        TextEditingController(text: text)
          ..selection = TextSelection.collapsed(offset: text.length),
    ];
  }

  @override
  void dispose() {
    for (final controller in _custom) {
      controller.dispose();
    }
    super.dispose();
  }

  Future<void> _submit(QuestionDraft draft) async {
    if (!draft.complete || draft.submitting) return;
    await _resolve(draft.answers);
  }

  Future<void> _resolve(List<List<String>>? answers) async {
    final drafts = ref.read(questionDraftProvider.notifier);
    if (drafts.of(widget.request.id, _count).submitting) return;
    drafts.setSubmitting(widget.request.id, true);
    setState(() => _submitError = null);
    // Read before awaiting: a transcript row arriving mid-request destroys
    // this element, and `ref` is unusable afterwards. That is what left an
    // answered card on screen — the reply landed, the agent moved on, and the
    // card that could no longer be dismissed took every further tap.
    final api = ref.read(chatApiProvider);
    final pending = ref.read(pendingProvider.notifier);
    final toast = ref.read(toastProvider.notifier);
    final i18n = ref.read(i18nProvider);
    final events = ref.read(appEventBusProvider);
    try {
      if (answers == null) {
        await api.rejectQuestion(widget.request.id);
      } else {
        await api.replyQuestion(widget.request.id, answers);
      }
      // Do not wait for the WS `question.replied` event to take the card
      // away: the socket may be down while the app is backgrounded.
      pending.removeQuestion(widget.request.id);
      events.emit('question.resolved', {'sessionId': widget.request.sessionId});
    } catch (error) {
      final code = error is DioException ? error.response?.statusCode : null;
      if (code == 404 || code == 410) {
        pending.removeQuestion(widget.request.id);
        toast.error(i18n.t('chat:question.gone'));
        await pending.refreshQuestions();
      } else {
        final message = i18n.t(
          code == 409 ? 'chat:question.conflict' : 'chat:question.submitFailed',
        );
        if (mounted) setState(() => _submitError = message);
        toast.error(message);
        if (code == 409) await pending.refreshQuestions();
      }
    } finally {
      drafts.setSubmitting(widget.request.id, false);
    }
  }

  Future<void> _reject() => _resolve(null);

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final draft =
        ref.watch(questionDraftProvider)[widget.request.id] ??
        QuestionDraft.empty(_count);
    final complete = draft.complete;
    ref.listen(
      questionDraftProvider.select((drafts) => drafts[widget.request.id]),
      (_, next) {
        if (next == null) return;
        for (var i = 0; i < _custom.length; i++) {
          if (_custom[i].text != next.custom[i]) {
            _custom[i].value = TextEditingValue(
              text: next.custom[i],
              selection: TextSelection.collapsed(offset: next.custom[i].length),
            );
          }
        }
      },
    );
    return Container(
      margin: const EdgeInsets.fromLTRB(12, 0, 12, 6),
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: t.card,
        borderRadius: BorderRadius.circular(Radii.xl),
        border: Border.all(color: t.hair),
        boxShadow: const [
          BoxShadow(
            color: Color(0x14000000),
            blurRadius: 8,
            offset: Offset(0, 2),
          ),
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
                    _question(t, i18n, index, question, draft),
                ],
              ),
            ),
          ),
          const SizedBox(height: 10),
          Wrap(
            spacing: 8,
            crossAxisAlignment: WrapCrossAlignment.center,
            children: [
              FilledButton(
                onPressed: complete && !draft.submitting
                    ? () => _submit(draft)
                    : null,
                style: FilledButton.styleFrom(
                  backgroundColor: t.ink,
                  foregroundColor: t.bg,
                  disabledBackgroundColor: t.ink.withValues(alpha: 0.4),
                  disabledForegroundColor: t.bg,
                  padding: const EdgeInsets.symmetric(
                    horizontal: 16,
                    vertical: 6,
                  ),
                  shape: RoundedRectangleBorder(
                    borderRadius: BorderRadius.circular(Radii.full),
                  ),
                ),
                child: Text(
                  i18n.t(
                    draft.submitting
                        ? 'chat:question.submitting'
                        : 'chat:question.submit',
                  ),
                  style: const TextStyle(fontSize: FontSizes.sm),
                ),
              ),
              TextButton(
                onPressed: draft.submitting ? null : _reject,
                child: Text(
                  i18n.t(
                    _count > 1 ? 'chat:question.skipAll' : 'chat:question.skip',
                  ),
                  style: TextStyle(fontSize: FontSizes.sm, color: t.n600),
                ),
              ),
              if (_count > 1)
                Text(
                  i18n.t(
                    'chat:question.progress',
                    vars: {
                      'count': _count,
                      'answered': draft.answers
                          .where((a) => a.isNotEmpty)
                          .length,
                    },
                  ),
                  style: TextStyle(fontSize: FontSizes.xs, color: t.n500),
                ),
            ],
          ),
          if (_submitError != null)
            Text(
              _submitError!,
              style: TextStyle(color: t.n700, fontSize: FontSizes.xs),
            ),
          if (draft.saving)
            Text(
              i18n.t('chat:question.saving'),
              style: TextStyle(color: t.n500, fontSize: FontSizes.xs),
            ),
          if (draft.saveError != null) ...[
            Text(
              i18n.t(
                draft.saveError == 'conflict'
                    ? 'chat:question.draftConflict'
                    : 'chat:question.draftFailed',
              ),
              style: TextStyle(color: t.n700, fontSize: FontSizes.xs),
            ),
            TextButton(
              onPressed: draft.submitting
                  ? null
                  : () => ref
                        .read(questionDraftProvider.notifier)
                        .retry(widget.request.id),
              child: Text(i18n.t('chat:question.retrySave')),
            ),
          ],
        ],
      ),
    );
  }

  Widget _question(
    BossipTokens t,
    I18nState i18n,
    int index,
    QuestionItem question,
    QuestionDraft draft,
  ) {
    final selected = draft.useCustom[index] ? <String>{} : draft.picked[index];
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
            _count > 1
                ? '${index + 1}. ${question.question}'
                : question.question,
            style: TextStyle(
              fontSize: FontSizes.base,
              color: t.ink,
              height: 1.5,
            ),
          ),
          VideoApprovalDetail(item: question),
          const SizedBox(height: 6),
          Wrap(
            spacing: 6,
            runSpacing: 6,
            children: [
              for (final option in question.options)
                ChoiceChip(
                  label: Text(
                    option.label,
                    style: const TextStyle(fontSize: FontSizes.sm),
                  ),
                  selected: selected.contains(option.label),
                  showCheckmark: false,
                  selectedColor: t.a200,
                  backgroundColor: t.bg,
                  side: BorderSide(
                    color: selected.contains(option.label) ? t.a700 : t.hair,
                  ),
                  labelStyle: TextStyle(color: t.ink),
                  onSelected: draft.submitting
                      ? null
                      : (on) => ref
                            .read(questionDraftProvider.notifier)
                            .toggle(
                              widget.request.id,
                              _count,
                              index,
                              option.label,
                              multiple: question.multiple,
                              on: on,
                            ),
                ),
            ],
          ),
          if (question.custom) ...[
            const SizedBox(height: 6),
            TextField(
              controller: _custom[index],
              enabled: !draft.submitting,
              maxLength: 5000,
              onTap: () => ref
                  .read(questionDraftProvider.notifier)
                  .write(widget.request.id, _count, index, _custom[index].text),
              onChanged: (text) => ref
                  .read(questionDraftProvider.notifier)
                  .write(widget.request.id, _count, index, text),
              style: TextStyle(fontSize: FontSizes.sm, color: t.ink),
              decoration: InputDecoration(
                counterText: '',
                hintText: i18n.t('chat:question.answer'),
                hintStyle: TextStyle(fontSize: FontSizes.sm, color: t.n500),
                isDense: true,
                contentPadding: const EdgeInsets.symmetric(
                  horizontal: 10,
                  vertical: 8,
                ),
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
