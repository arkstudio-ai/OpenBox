import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/skill_job.dart';
import '../../../shared/utils/format.dart';
import '../api/jobs_api.dart';

/// Status → (dot color, i18n label key). Mirrors web `statusTone`.
(Color, String) skillJobTone(BossipTokens t, String status) => switch (status) {
      'running' => (t.accent, 'jobs:status.running'),
      'queued' => (t.n400, 'jobs:status.queued'),
      'retry_scheduled' => (t.n400, 'jobs:status.retry_scheduled'),
      'waiting_external' => (t.accent, 'jobs:status.waiting_external'),
      'waiting_user' => (t.accent, 'jobs:status.waiting_user'),
      'waiting_agent' => (t.accent, 'jobs:status.waiting_agent'),
      'succeeded' => (t.sage, 'jobs:status.succeeded'),
      'failed' => (t.danger, 'jobs:status.failed'),
      _ => (t.n400, 'jobs:status.cancelled'),
    };

/// One background job (web `SkillJobCard`): status dot + skill·operation,
/// phase label from the manifest's i18n key, waiting_user answer box, cancel.
class SkillJobCard extends ConsumerStatefulWidget {
  const SkillJobCard({super.key, required this.job, required this.sessionId});

  final SkillJobSnapshot job;
  final String sessionId;

  @override
  ConsumerState<SkillJobCard> createState() => _SkillJobCardState();
}

class _SkillJobCardState extends ConsumerState<SkillJobCard> {
  final _answer = TextEditingController();
  bool _busy = false;

  @override
  void dispose() {
    _answer.dispose();
    super.dispose();
  }

  Future<void> _act(Future<void> Function() action) async {
    setState(() => _busy = true);
    try {
      await action();
    } finally {
      if (mounted) setState(() => _busy = false);
    }
    ref.invalidate(sessionSkillJobsProvider(widget.sessionId));
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final job = widget.job;
    final (dot, labelKey) = skillJobTone(t, job.status);

    String? phaseLabel;
    if (!job.terminal && (job.phase ?? '').isNotEmpty) {
      final key = 'jobs:${job.phaseLabelKey ?? ''}';
      final resolved = job.phaseLabelKey == null ? '' : i18n.t(key);
      phaseLabel = (resolved.isEmpty || resolved == key) ? job.phase : resolved;
    }

    final prompt = job.progress['prompt'];
    final summary = job.terminal && job.status == 'succeeded'
        ? job.result.entries
            .where((e) => e.value != null && e.value is! Map && e.value is! List)
            .map((e) => '${e.key}: ${e.value}')
            .join(' · ')
        : null;

    return Container(
      margin: const EdgeInsets.only(top: 8),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: t.surface,
        border: Border.all(color: t.hair),
        borderRadius: BorderRadius.circular(12),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(children: [
            Container(
              width: 8,
              height: 8,
              decoration: BoxDecoration(color: dot, shape: BoxShape.circle),
            ),
            const SizedBox(width: 8),
            Expanded(
              child: Text(
                '${job.displayName} · ${job.operation}',
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: TextStyle(
                    color: t.n800, fontSize: FontSizes.md, fontWeight: FontWeight.w500),
              ),
            ),
            Text(i18n.t(labelKey),
                style: TextStyle(color: t.n500, fontSize: FontSizes.xs)),
          ]),
          if (phaseLabel != null)
            Padding(
              padding: const EdgeInsets.only(top: 4),
              child: Text(phaseLabel,
                  style: TextStyle(color: t.n600, fontSize: FontSizes.xs)),
            ),
          if (summary != null && summary.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(top: 4),
              child: Text(summary,
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(color: t.n600, fontSize: FontSizes.xs)),
            ),
          if (job.errorMessage != null && job.errorMessage!.isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(top: 4),
              child: Text(job.errorMessage!,
                  style: TextStyle(color: t.danger, fontSize: FontSizes.xs)),
            ),
          if (job.status == 'waiting_user') _answerBox(t, i18n, prompt),
          Padding(
            padding: const EdgeInsets.only(top: 8),
            child: Row(children: [
              if (job.updatedAt != null)
                Text(formatRelative(job.updatedAt!, i18n.language),
                    style: TextStyle(color: t.n500, fontSize: FontSizes.xs)),
              const Spacer(),
              if (!job.terminal && job.desiredState != 'cancel')
                GestureDetector(
                  onTap: _busy
                      ? null
                      : () => _act(() => ref.read(jobsApiProvider).cancel(job.jobId)),
                  child: Text(i18n.t('jobs:card.cancel'),
                      style: TextStyle(color: t.n500, fontSize: FontSizes.xs)),
                ),
              if (!job.terminal && job.desiredState == 'cancel')
                Text(i18n.t('jobs:card.cancelling'),
                    style: TextStyle(color: t.n500, fontSize: FontSizes.xs)),
            ]),
          ),
        ],
      ),
    );
  }

  Widget _answerBox(BossipTokens t, I18nState i18n, Object? prompt) {
    // An empty input_schema marks a prompt-only park (operator review): the
    // handler will not consume free text, so offering a box would be a lie.
    final schema = widget.job.progress['input_schema'];
    final acceptsInput = schema is Map && schema.isNotEmpty;
    return Padding(
      padding: const EdgeInsets.only(top: 8),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        if (prompt is String && prompt.isNotEmpty)
          Padding(
            padding: const EdgeInsets.only(bottom: 6),
            child: Text(prompt,
                style: TextStyle(color: t.n700, fontSize: FontSizes.md)),
          ),
        if (acceptsInput)
        Row(children: [
          Expanded(
            child: TextField(
              controller: _answer,
              style: TextStyle(color: t.ink, fontSize: FontSizes.md),
              decoration: InputDecoration(
                isDense: true,
                hintText: i18n.t('jobs:card.answerPlaceholder'),
                hintStyle: TextStyle(color: t.n400, fontSize: FontSizes.md),
                contentPadding:
                    const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
                enabledBorder: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(8),
                  borderSide: BorderSide(color: t.n200),
                ),
                focusedBorder: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(8),
                  borderSide: BorderSide(color: t.accent),
                ),
              ),
            ),
          ),
          const SizedBox(width: 6),
          FilledButton(
            style: FilledButton.styleFrom(
              backgroundColor: t.accent,
              padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
              minimumSize: Size.zero,
            ),
            onPressed: _busy
                ? null
                : () {
                    final text = _answer.text.trim();
                    if (text.isEmpty) return;
                    // Keyed to the prompt round: replays cannot double-consume
                    // (skill_job_inputs unique key), same as web.
                    _act(() => ref.read(jobsApiProvider).answer(
                          widget.job.jobId,
                          {'text': text},
                          idempotencyKey:
                              'ui:${widget.job.jobId}:${widget.job.lastEventSeq}',
                        ));
                    _answer.clear();
                  },
            child: Text(i18n.t('jobs:card.answerSend'),
                style: TextStyle(fontSize: FontSizes.sm)),
          ),
        ]),
      ]),
    );
  }
}
