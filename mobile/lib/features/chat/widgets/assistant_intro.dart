import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/assistant_profile.dart';
import '../../../shared/api/auth_store.dart';
import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/widgets/toast.dart';

/// The first meeting with the personal assistant (web `AssistantIntro`,
/// backend `assistant/profile.py` `intro_event`): at most four short
/// questions, one at a time, each answered with a tap or a few words and each
/// skippable, then one real thing to start with. The app asks, not the model,
/// so nothing is asked twice, restated or lost; every answer is saved at once
/// as the person's decision.
class AssistantIntro extends ConsumerStatefulWidget {
  const AssistantIntro({
    super.key,
    required this.mode,
    required this.onPick,
    required this.onClose,
    this.framed = true,
  });

  /// `auto` (by itself on the welcome page), `undecided` (opened later),
  /// `all` ("重新认识一下" in Settings) — see [introStepsFor].
  final String mode;

  /// Puts a first task's prompt into the composer; it is not sent.
  final ValueChanged<String> onPick;

  /// The meeting is over here: finished, put off, or closed.
  final VoidCallback onClose;

  /// Its own card on the welcome page; bare inside a sheet.
  final bool framed;

  @override
  ConsumerState<AssistantIntro> createState() => _AssistantIntroState();
}

class _AssistantIntroState extends ConsumerState<AssistantIntro> {
  // Fixed when the meeting opens with the person's settings known, so an
  // answer never reshuffles what is left.
  List<String>? _steps;
  final _handled = <String>[];
  bool _busy = false;

  Future<void> _record(Map<String, Object> event, String step) async {
    final i18n = ref.read(i18nProvider);
    final toast = ref.read(toastProvider.notifier);
    setState(() => _busy = true);
    try {
      await ref.read(assistantProfileProvider.notifier).recordIntro(event);
      if (mounted) setState(() => _handled.add(step));
    } catch (_) {
      toast.error(i18n.t('chat:assistant.intro.failed'));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  void _later() {
    // Only the meeting that came by itself is put off; one the person opened
    // just closes.
    if (widget.mode == 'auto') {
      ref
          .read(assistantProfileProvider.notifier)
          .recordIntro({'event': 'dismiss'})
          .catchError((_) {});
    }
    widget.onClose();
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final profile = ref.watch(assistantProfileProvider).valueOrNull;
    if (profile == null) return const SizedBox.shrink();
    final steps = _steps ??= introStepsFor(profile, widget.mode);
    // A question decided elsewhere meanwhile (the phone, Settings, the
    // assistant in chat) is not asked here.
    final current = steps
        .where(
          (step) =>
              !_handled.contains(step) &&
              (widget.mode == 'all' || !profile.decided.contains(step)),
        )
        .firstOrNull;
    final body = current == null
        ? _closing(i18n, t, profile)
        : _question(i18n, t, profile, steps, current);
    if (!widget.framed) return body;
    return Container(
      key: const ValueKey('assistant-intro'),
      constraints: const BoxConstraints(maxWidth: 520),
      padding: const EdgeInsets.all(18),
      decoration: BoxDecoration(
        color: t.card,
        border: Border.all(color: t.hair),
        borderRadius: BorderRadius.circular(Radii.xl),
      ),
      child: body,
    );
  }

  Widget _closing(I18nState i18n, BossipTokens t, AssistantProfile profile) {
    final kind = businessKinds.contains(profile.business)
        ? profile.business
        : 'general';
    final ideas = i18n.tList('chat:assistant.intro.suggest.$kind');
    return Column(
      key: const ValueKey('assistant-intro-done'),
      crossAxisAlignment: CrossAxisAlignment.stretch,
      mainAxisSize: MainAxisSize.min,
      children: [
        Text(
          profile.address.isNotEmpty
              ? i18n.t(
                  'chat:assistant.intro.doneTitle',
                  vars: {'name': profile.address},
                )
              : i18n.t('chat:assistant.intro.doneTitlePlain'),
          style: TextStyle(
            fontSize: FontSizes.md,
            fontWeight: FontWeight.w500,
            color: t.ink,
          ),
        ),
        const SizedBox(height: 12),
        for (final idea in ideas)
          if (idea is Map)
            Padding(
              padding: const EdgeInsets.only(bottom: 8),
              child: OutlinedButton(
                onPressed: () {
                  widget.onPick('${idea['prompt'] ?? ''}');
                  widget.onClose();
                },
                style: OutlinedButton.styleFrom(
                  alignment: Alignment.centerLeft,
                  padding: const EdgeInsets.symmetric(
                    horizontal: 14,
                    vertical: 12,
                  ),
                  side: BorderSide(color: t.hair),
                  shape: RoundedRectangleBorder(
                    borderRadius: BorderRadius.circular(Radii.lg),
                  ),
                ),
                child: Text(
                  '${idea['title'] ?? ''}',
                  style: TextStyle(fontSize: FontSizes.sm, color: t.ink),
                ),
              ),
            ),
        Align(
          alignment: Alignment.centerLeft,
          child: TextButton(
            onPressed: widget.onClose,
            child: Text(
              i18n.t('chat:assistant.intro.doneLater'),
              style: TextStyle(fontSize: FontSizes.sm, color: t.n600),
            ),
          ),
        ),
      ],
    );
  }

  Widget _question(
    I18nState i18n,
    BossipTokens t,
    AssistantProfile profile,
    List<String> steps,
    String step,
  ) {
    final position = steps.indexOf(step) + 1;
    return Column(
      key: ValueKey('intro-step-$step'),
      crossAxisAlignment: CrossAxisAlignment.start,
      mainAxisSize: MainAxisSize.min,
      children: [
        if (widget.mode == 'auto' && position == 1) ...[
          Text(
            i18n.t('chat:assistant.intro.lead'),
            style: TextStyle(fontSize: FontSizes.sm, color: t.n700),
          ),
          const SizedBox(height: 10),
        ],
        Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Expanded(
              child: Text(
                i18n.t('chat:assistant.intro.$step.question'),
                style: TextStyle(
                  fontSize: FontSizes.md,
                  fontWeight: FontWeight.w500,
                  color: t.ink,
                ),
              ),
            ),
            Text(
              i18n.t(
                'chat:assistant.intro.progress',
                vars: {'current': position, 'total': steps.length},
              ),
              style: TextStyle(fontSize: FontSizes.xs, color: t.n500),
            ),
          ],
        ),
        const SizedBox(height: 12),
        _Question(
          key: ValueKey(step),
          step: step,
          today: widget.mode == 'all' ? profile.valueOf(step) : '',
          showToday: widget.mode == 'all',
          busy: _busy,
          onAnswer: (value) =>
              _record({'event': 'answer', 'step': step, 'value': value}, step),
        ),
        const SizedBox(height: 6),
        Row(
          children: [
            TextButton(
              key: const ValueKey('intro-skip'),
              onPressed: _busy
                  ? null
                  : () => _record({'event': 'skip', 'step': step}, step),
              child: Text(
                i18n.t('chat:assistant.intro.skip'),
                style: TextStyle(fontSize: FontSizes.sm, color: t.n600),
              ),
            ),
            TextButton(
              key: const ValueKey('intro-later'),
              onPressed: _busy
                  ? null
                  : widget.mode == 'auto'
                  ? _later
                  : widget.onClose,
              child: Text(
                i18n.t(
                  widget.mode == 'auto'
                      ? 'chat:assistant.intro.later'
                      : 'chat:assistant.intro.close',
                ),
                style: TextStyle(fontSize: FontSizes.sm, color: t.n600),
              ),
            ),
          ],
        ),
      ],
    );
  }
}

/// One question: its quick choices and, where it takes words, a short field.
/// Going through again ([showToday]), today's choice is the one shown as
/// chosen and today's words fill the field, so it is a matter of confirming.
class _Question extends ConsumerStatefulWidget {
  const _Question({
    super.key,
    required this.step,
    required this.today,
    required this.showToday,
    required this.busy,
    required this.onAnswer,
  });

  final String step;
  final String today;
  final bool showToday;
  final bool busy;
  final ValueChanged<String> onAnswer;

  @override
  ConsumerState<_Question> createState() => _QuestionState();
}

class _QuestionState extends ConsumerState<_Question> {
  late bool _other =
      widget.step == 'business' &&
      widget.today.isNotEmpty &&
      !businessKinds.contains(widget.today);
  late final _words = TextEditingController(
    text: widget.step == 'business' && !_other ? '' : widget.today,
  );

  @override
  void dispose() {
    _words.dispose();
    super.dispose();
  }

  void _submit() {
    final value = _words.text.trim();
    if (value.isNotEmpty && !widget.busy) widget.onAnswer(value);
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final step = widget.step;
    final username = ref.watch(authProvider).user?.username ?? '';
    final options = <(String, String)>[
      if (step == 'address') ...[
        // The sign-in name is only offered, never assumed.
        if (username.isNotEmpty)
          (
            username,
            i18n.t(
              'chat:assistant.intro.address.useAccount',
              vars: {'name': username},
            ),
          ),
        ('', i18n.t('chat:assistant.intro.address.none')),
      ],
      if (step == 'name') ('', i18n.t('chat:assistant.intro.name.keep')),
      if (step == 'length')
        for (final value in AssistantProfile.lengths)
          (value, i18n.t('chat:assistant.intro.length.$value')),
      if (step == 'business')
        for (final value in businessKinds)
          (value, i18n.t('chat:assistant.intro.business.$value')),
    ];
    final choosable = step == 'length' || step == 'business';
    final takesWords =
        step == 'address' || step == 'name' || (step == 'business' && _other);
    final border = OutlineInputBorder(
      borderRadius: BorderRadius.circular(Radii.full),
      borderSide: BorderSide(color: t.hair),
    );
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      mainAxisSize: MainAxisSize.min,
      children: [
        Wrap(
          spacing: 8,
          runSpacing: 8,
          children: [
            for (final (value, label) in options)
              _Pill(
                key: ValueKey('intro-$step-${value.isEmpty ? 'none' : value}'),
                label: label,
                chosen: choosable && widget.showToday && widget.today == value,
                onTap: widget.busy ? null : () => widget.onAnswer(value),
              ),
            if (step == 'business' && !_other)
              _Pill(
                key: const ValueKey('intro-business-other'),
                label: i18n.t('chat:assistant.intro.business.other'),
                chosen: false,
                onTap: widget.busy ? null : () => setState(() => _other = true),
              ),
          ],
        ),
        if (takesWords) ...[
          const SizedBox(height: 10),
          Row(
            children: [
              Expanded(
                child: TextField(
                  key: ValueKey('intro-words-$step'),
                  controller: _words,
                  maxLength: 20,
                  onChanged: (_) => setState(() {}),
                  onSubmitted: (_) => _submit(),
                  style: TextStyle(fontSize: FontSizes.base, color: t.ink),
                  decoration: InputDecoration(
                    hintText: i18n.t('chat:assistant.intro.$step.placeholder'),
                    hintStyle: TextStyle(fontSize: FontSizes.sm, color: t.n600),
                    counterText: '',
                    isDense: true,
                    filled: true,
                    fillColor: t.card,
                    border: border,
                    enabledBorder: border,
                  ),
                ),
              ),
              const SizedBox(width: 8),
              FilledButton(
                key: ValueKey('intro-confirm-$step'),
                onPressed: widget.busy || _words.text.trim().isEmpty
                    ? null
                    : _submit,
                child: Text(i18n.t('chat:assistant.intro.confirm')),
              ),
            ],
          ),
        ],
      ],
    );
  }
}

class _Pill extends StatelessWidget {
  const _Pill({
    super.key,
    required this.label,
    required this.chosen,
    required this.onTap,
  });
  final String label;
  final bool chosen;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Semantics(
      button: true,
      selected: chosen,
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(Radii.full),
        child: Container(
          constraints: const BoxConstraints(minHeight: 36),
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 8),
          decoration: BoxDecoration(
            color: t.card,
            borderRadius: BorderRadius.circular(Radii.full),
            border: Border.all(color: chosen ? t.ink : t.hair),
          ),
          child: Text(
            label,
            style: TextStyle(fontSize: FontSizes.sm, color: t.ink),
          ),
        ),
      ),
    );
  }
}
