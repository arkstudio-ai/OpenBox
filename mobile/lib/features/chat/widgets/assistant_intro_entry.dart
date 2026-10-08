import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/assistant_profile.dart';
import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import 'assistant_intro.dart';

/// Above the composer of a personal-assistant conversation that has started
/// (web `AssistantIntroEntry`): the way back into the first meeting while
/// something is still undecided. Once, after the person went straight to work
/// instead, a one-line reminder; otherwise a quiet "让我更懂你". Settings'
/// "重新认识一下" lands here too ([requested] `all`), going through every
/// question with today's values as the defaults.
class AssistantIntroEntry extends ConsumerStatefulWidget {
  const AssistantIntroEntry({
    super.key,
    required this.quiet,
    required this.onPick,
    this.requested,
    this.onRequestDone,
  });

  /// An answer has come and nothing is running.
  final bool quiet;

  /// Puts a first task's prompt into the composer.
  final ValueChanged<String> onPick;

  /// `all` when the page was opened to go through the meeting again.
  final String? requested;

  /// The requested meeting closed; the page forgets the request.
  final VoidCallback? onRequestDone;

  @override
  ConsumerState<AssistantIntroEntry> createState() =>
      _AssistantIntroEntryState();
}

class _AssistantIntroEntryState extends ConsumerState<AssistantIntroEntry> {
  bool _reminding = false;
  bool _nudgeSent = false;
  bool _open = false;

  Future<void> _openMeeting(String mode, {bool requested = false}) async {
    if (_open) return;
    _open = true;
    FocusScope.of(context).unfocus();
    // Arriving from Settings, which the drawer opened: the conversation, not
    // the drawer, sits behind the meeting.
    final scaffold = Scaffold.maybeOf(context);
    if (scaffold?.isDrawerOpen ?? false) scaffold!.closeDrawer();
    await showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      useSafeArea: true,
      backgroundColor: context.tokens.bg,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(Radii.xl)),
      ),
      builder: (sheetContext) => Padding(
        padding: EdgeInsets.fromLTRB(
          20,
          20,
          20,
          20 + MediaQuery.viewInsetsOf(sheetContext).bottom,
        ),
        child: SingleChildScrollView(
          child: AssistantIntro(
            mode: mode,
            framed: false,
            onPick: widget.onPick,
            onClose: () => Navigator.of(sheetContext).pop(),
          ),
        ),
      ),
    );
    _open = false;
    if (requested && mounted) widget.onRequestDone?.call();
  }

  void _followRequest() {
    if (widget.requested != 'all' || _open) return;
    // Opened once the person's settings are known, so today's values show.
    if (ref.read(assistantProfileProvider).valueOrNull == null) return;
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted && widget.requested == 'all') {
        _openMeeting('all', requested: true);
      }
    });
  }

  @override
  void initState() {
    super.initState();
    _followRequest();
  }

  @override
  void didUpdateWidget(AssistantIntroEntry oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.requested != widget.requested) _followRequest();
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    ref.listen(assistantProfileProvider, (previous, next) {
      if (previous?.valueOrNull == null && next.valueOrNull != null) {
        _followRequest();
      }
    });
    final profile = ref.watch(assistantProfileProvider).valueOrNull;
    final undecided = profile == null
        ? 0
        : introStepsFor(profile, 'undecided').length;
    final remind =
        widget.quiet &&
        profile != null &&
        profile.intro.status == 'bypassed' &&
        !profile.intro.nudged &&
        undecided > 0;
    // The reminder is shown once: recorded the moment it appears, and kept
    // on screen until answered.
    if (remind && !_nudgeSent) {
      _nudgeSent = true;
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (!mounted) return;
        setState(() => _reminding = true);
        ref
            .read(assistantProfileProvider.notifier)
            .recordIntro({'event': 'nudged'})
            .catchError((_) {});
      });
    }
    if (_reminding) {
      return Padding(
        key: const ValueKey('assistant-intro-nudge'),
        padding: const EdgeInsets.fromLTRB(12, 0, 12, 6),
        child: Semantics(
          liveRegion: true,
          child: Container(
            padding: const EdgeInsets.fromLTRB(14, 10, 8, 10),
            decoration: BoxDecoration(
              color: t.card,
              border: Border.all(color: t.hair),
              borderRadius: BorderRadius.circular(Radii.xl),
            ),
            child: Wrap(
              crossAxisAlignment: WrapCrossAlignment.center,
              spacing: 6,
              runSpacing: 6,
              children: [
                Text(
                  i18n.t('chat:assistant.intro.nudge'),
                  style: TextStyle(fontSize: FontSizes.sm, color: t.ink),
                ),
                FilledButton(
                  key: const ValueKey('intro-nudge-yes'),
                  onPressed: () {
                    setState(() => _reminding = false);
                    _openMeeting('undecided');
                  },
                  style: FilledButton.styleFrom(
                    minimumSize: const Size(0, 32),
                    padding: const EdgeInsets.symmetric(horizontal: 14),
                  ),
                  child: Text(i18n.t('chat:assistant.intro.nudgeYes')),
                ),
                TextButton(
                  key: const ValueKey('intro-nudge-no'),
                  onPressed: () {
                    setState(() => _reminding = false);
                    ref
                        .read(assistantProfileProvider.notifier)
                        .recordIntro({'event': 'dismiss'})
                        .catchError((_) {});
                  },
                  child: Text(
                    i18n.t('chat:assistant.intro.nudgeNo'),
                    style: TextStyle(fontSize: FontSizes.sm, color: t.n700),
                  ),
                ),
              ],
            ),
          ),
        ),
      );
    }
    if (!widget.quiet || undecided == 0) return const SizedBox.shrink();
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 0, 16, 2),
      child: Align(
        alignment: Alignment.centerLeft,
        child: TextButton.icon(
          key: const ValueKey('assistant-intro-entry'),
          onPressed: () => _openMeeting('undecided'),
          style: TextButton.styleFrom(
            minimumSize: const Size(0, 32),
            padding: const EdgeInsets.symmetric(horizontal: 6),
            foregroundColor: t.n600,
          ),
          icon: Icon(Icons.auto_awesome_outlined, size: 14, color: t.n600),
          label: Text(
            i18n.t('chat:assistant.intro.entry'),
            style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
          ),
        ),
      ),
    );
  }
}
