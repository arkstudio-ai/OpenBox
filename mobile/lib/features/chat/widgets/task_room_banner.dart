import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/session.dart';

/// "你在任务间：「…」· 回到总助理" at the top of a task's execution page
/// (web `TaskRoomBanner`, VOICE_SEAM_PLAN §11.3).
///
/// A task's conversation and the personal assistant's own look the same:
/// same bubbles, same composer. People could not tell whether they were
/// talking to the assistant or to one task (2026-11-02). This names the room,
/// its state and the one way back.
class TaskRoomBanner extends ConsumerWidget {
  const TaskRoomBanner({super.key, required this.session, this.onBack});

  final Session? session;
  final VoidCallback? onBack;

  /// Only a task's execution page is a room; the assistant's own page is not.
  static bool visible(Session? session) =>
      session != null &&
      session.assistantManaged &&
      session.kind != 'assistant' &&
      session.taskControl != null;

  /// The task's own title, or the conversation's on older servers.
  static String titleOf(Session session) {
    final title = session.taskControl?['task_title'];
    return title is String && title.isNotEmpty ? title : session.title;
  }

  /// Which word the banner uses for the task's state.
  static String stateOf(Map<String, dynamic> control) {
    final observed = control['observed_state'];
    final desired = control['desired_state'];
    if (observed == 'effect_unknown') return 'effect_unknown';
    if (observed == 'waiting_input') return 'waiting';
    if (desired == 'canceled' || observed == 'canceled') return 'canceled';
    if (desired == 'paused' ||
        observed == 'paused' ||
        observed == 'resume_blocked') {
      return 'paused';
    }
    if (observed == 'completed') return 'completed';
    if (observed == 'error') return 'error';
    if (observed == 'queued') return 'queued';
    return 'running';
  }

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final session = this.session;
    if (!visible(session)) return const SizedBox.shrink();
    final i18n = ref.watch(i18nProvider);
    final t = context.tokens;
    final state = stateOf(session!.taskControl!);
    final alert =
        state == 'effect_unknown' || state == 'waiting' || state == 'error';
    return Semantics(
      container: true,
      label: i18n.t('chat:room.here', vars: {'title': titleOf(session)}),
      child: Container(
        key: const Key('task-room-banner'),
        padding: const EdgeInsets.fromLTRB(14, 6, 8, 6),
        decoration: BoxDecoration(
          color: t.a100,
          border: Border(bottom: BorderSide(color: t.hair)),
        ),
        child: Row(
          children: [
            Icon(Icons.meeting_room_outlined, size: 16, color: t.a700),
            const SizedBox(width: 8),
            Expanded(
              child: Text.rich(
                TextSpan(
                  text: i18n.t(
                    'chat:room.here',
                    vars: {'title': titleOf(session)},
                  ),
                  children: [
                    TextSpan(
                      text: '  ${i18n.t('chat:room.state.$state')}',
                      style: TextStyle(
                        fontSize: FontSizes.xs,
                        color: alert ? t.dangerInk : t.n600,
                      ),
                    ),
                  ],
                ),
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: TextStyle(fontSize: FontSizes.sm, color: t.ink),
              ),
            ),
            TextButton(
              key: const Key('task-room-back'),
              onPressed: onBack,
              style: TextButton.styleFrom(
                visualDensity: VisualDensity.compact,
                padding: const EdgeInsets.symmetric(horizontal: 10),
              ),
              child: Text(
                i18n.t('chat:room.back'),
                style: TextStyle(fontSize: FontSizes.xs, color: t.a700),
              ),
            ),
          ],
        ),
      ),
    );
  }
}
