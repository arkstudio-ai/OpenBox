import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/skill_job.dart';
import '../api/jobs_api.dart';
import 'skill_job_card.dart';

/// Session-scoped background job cards at the end of the transcript (web
/// `SkillJobsDock`). The agent turn may be long over; these keep updating
/// from the job ledger.
class SkillJobsDock extends ConsumerWidget {
  const SkillJobsDock({super.key, required this.sessionId});

  final String sessionId;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final jobs = ref.watch(sessionSkillJobsProvider(sessionId)).value ?? const [];
    final visible = visibleSkillJobs(jobs);
    if (visible.isEmpty) return const SizedBox.shrink();

    return Padding(
      padding: const EdgeInsets.only(top: 12),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(i18n.t('jobs:dock.title'),
              style: TextStyle(
                  color: t.n500,
                  fontSize: FontSizes.xs,
                  fontWeight: FontWeight.w500)),
          for (final job in visible)
            SkillJobCard(key: ValueKey(job.jobId), job: job, sessionId: sessionId),
        ],
      ),
    );
  }
}
