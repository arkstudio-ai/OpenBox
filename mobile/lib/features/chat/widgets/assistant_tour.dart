import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/assistant_profile.dart';
import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';

/// The four-step tour of how the person and the assistant work together
/// (web `AssistantTour`, VOICE_SEAM_PLAN §12.6): you only talk to me; I open
/// a task card and send a helper; tap the card to see the work; I report
/// when it is done. Shown once by itself (recorded on the profile, so no
/// other app shows it again), and any time from the app bar.
const tourSteps = [
  ('talk', Icons.chat_bubble_outline),
  ('card', Icons.assignment_outlined),
  ('site', Icons.meeting_room_outlined),
  ('report', Icons.notifications_active_outlined),
];

/// Opens the tour as a bottom sheet; marks it seen when [record] is set.
Future<void> showAssistantTour(
  BuildContext context,
  WidgetRef ref, {
  bool record = false,
}) async {
  FocusScope.of(context).unfocus();
  await showModalBottomSheet<void>(
    context: context,
    isScrollControlled: true,
    useSafeArea: true,
    backgroundColor: context.tokens.bg,
    shape: const RoundedRectangleBorder(
      borderRadius: BorderRadius.vertical(top: Radius.circular(Radii.xl)),
    ),
    builder: (sheetContext) =>
        AssistantTour(onClose: () => Navigator.of(sheetContext).pop()),
  );
  if (record) {
    await ref
        .read(assistantProfileProvider.notifier)
        .recordIntro({'event': 'tour'})
        .catchError((_) {});
  }
}

/// Opens the tour by itself, once, for someone who has not seen it and is
/// not being asked the first meeting's questions. Renders nothing.
class AssistantTourAutoOpen extends ConsumerStatefulWidget {
  const AssistantTourAutoOpen({super.key});

  @override
  ConsumerState<AssistantTourAutoOpen> createState() =>
      _AssistantTourAutoOpenState();
}

class _AssistantTourAutoOpenState extends ConsumerState<AssistantTourAutoOpen> {
  bool _offered = false;

  void _offer(AssistantProfile? profile) {
    if (_offered || profile == null) return;
    if (profile.intro.tourSeen || introStartsByItself(profile)) return;
    _offered = true;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted) showAssistantTour(context, ref, record: true);
    });
  }

  @override
  Widget build(BuildContext context) {
    ref.listen(assistantProfileProvider, (_, next) => _offer(next.valueOrNull));
    _offer(ref.watch(assistantProfileProvider).valueOrNull);
    return const SizedBox.shrink();
  }
}

class AssistantTour extends ConsumerStatefulWidget {
  const AssistantTour({super.key, required this.onClose});

  final VoidCallback onClose;

  @override
  ConsumerState<AssistantTour> createState() => _AssistantTourState();
}

class _AssistantTourState extends ConsumerState<AssistantTour> {
  int _index = 0;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final (key, icon) = tourSteps[_index];
    final last = _index == tourSteps.length - 1;
    return Padding(
      padding: const EdgeInsets.fromLTRB(24, 20, 24, 24),
      child: Column(
        key: const Key('assistant-tour'),
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.center,
        children: [
          Text(
            '${i18n.t('chat:assistant.tour.title')} · '
            '${i18n.t('chat:assistant.tour.position', vars: {'index': _index + 1, 'count': tourSteps.length})}',
            style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
          ),
          const SizedBox(height: 16),
          Container(
            width: 64,
            height: 64,
            decoration: BoxDecoration(color: t.a100, shape: BoxShape.circle),
            child: Icon(icon, size: 30, color: t.a700),
          ),
          const SizedBox(height: 16),
          Text(
            i18n.t('chat:assistant.tour.steps.$key.title'),
            key: const Key('assistant-tour-title'),
            textAlign: TextAlign.center,
            style: TextStyle(
              fontSize: FontSizes.xl,
              fontWeight: FontWeight.w500,
              color: t.ink,
            ),
          ),
          const SizedBox(height: 8),
          Text(
            i18n.t('chat:assistant.tour.steps.$key.body'),
            textAlign: TextAlign.center,
            style: TextStyle(
              fontSize: FontSizes.base,
              height: 1.5,
              color: t.n800,
            ),
          ),
          const SizedBox(height: 16),
          Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              for (var i = 0; i < tourSteps.length; i++)
                Container(
                  width: 6,
                  height: 6,
                  margin: const EdgeInsets.symmetric(horizontal: 3),
                  decoration: BoxDecoration(
                    color: i == _index ? t.ink : t.n300,
                    shape: BoxShape.circle,
                  ),
                ),
            ],
          ),
          const SizedBox(height: 20),
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: [
              TextButton(
                key: const Key('assistant-tour-skip'),
                onPressed: widget.onClose,
                child: Text(
                  i18n.t('chat:assistant.tour.skip'),
                  style: TextStyle(fontSize: FontSizes.sm, color: t.n600),
                ),
              ),
              FilledButton(
                key: const Key('assistant-tour-next'),
                style: FilledButton.styleFrom(
                  backgroundColor: t.ink,
                  foregroundColor: t.bg,
                  shape: const StadiumBorder(),
                ),
                onPressed: last
                    ? widget.onClose
                    : () => setState(() => _index += 1),
                child: Text(
                  i18n.t(
                    last
                        ? 'chat:assistant.tour.done'
                        : 'chat:assistant.tour.next',
                  ),
                ),
              ),
            ],
          ),
        ],
      ),
    );
  }
}
