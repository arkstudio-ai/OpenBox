import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/light_autopilot.dart';

const lightAutopilotRepeats = ['daily', 'weekdays', 'weekly'];
const lightAutopilotTimes = ['08:00', '10:00', '12:00', '18:00'];
const lightAutopilotCaps = ['10', '20', '50'];

/// The three questions behind 「以后省心托管这个」 (plan §4.3): when, make only
/// or make and publish, and the most one run may spend. Publishing needs a
/// cloud desktop, so without one that answer is shown but locked. Null when
/// the person closes the sheet.
Future<LightAutopilotChoice?> showLightAutopilotSheet(
  BuildContext context, {
  required bool canPublish,
  required bool allowPublish,
}) {
  return showModalBottomSheet<LightAutopilotChoice>(
    context: context,
    useRootNavigator: true,
    isScrollControlled: true,
    backgroundColor: context.tokens.bg,
    shape: const RoundedRectangleBorder(
      borderRadius: BorderRadius.vertical(top: Radius.circular(Radii.xl2)),
    ),
    builder: (_) => _Sheet(canPublish: canPublish, allowPublish: allowPublish),
  );
}

class _Sheet extends ConsumerStatefulWidget {
  const _Sheet({required this.canPublish, required this.allowPublish});

  /// The workspace has a desktop.
  final bool canPublish;

  /// The result can be published at all (a video, not an image).
  final bool allowPublish;

  @override
  ConsumerState<_Sheet> createState() => _SheetState();
}

class _SheetState extends ConsumerState<_Sheet> {
  var _choice = const LightAutopilotChoice();

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    Widget section(String key, List<Widget> chips) => Padding(
      padding: const EdgeInsets.only(top: 18),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            i18n.t('light:autopilot.$key'),
            style: TextStyle(
              fontSize: FontSizes.sm,
              fontWeight: FontWeight.w600,
              color: t.n700,
            ),
          ),
          const SizedBox(height: 8),
          Wrap(spacing: 8, runSpacing: 8, children: chips),
        ],
      ),
    );
    return SafeArea(
      child: Padding(
        padding: const EdgeInsets.fromLTRB(20, 18, 20, 16),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Text(
              i18n.t('light:autopilot.sheetTitle'),
              style: TextStyle(
                fontSize: FontSizes.xl,
                fontWeight: FontWeight.w700,
                color: t.ink,
              ),
            ),
            const SizedBox(height: 4),
            Text(
              i18n.t('light:autopilot.sheetLede'),
              style: TextStyle(fontSize: FontSizes.sm, color: t.n600),
            ),
            section('when', [
              for (final repeat in lightAutopilotRepeats)
                _Chip(
                  key: Key('light-autopilot-repeat-$repeat'),
                  label: i18n.t('light:autopilot.repeat.$repeat'),
                  selected: _choice.repeat == repeat,
                  onTap: () => setState(
                    () => _choice = _choice.copyWith(repeat: repeat),
                  ),
                ),
              for (final time in lightAutopilotTimes)
                _Chip(
                  key: Key('light-autopilot-time-$time'),
                  label: time,
                  selected: _choice.time == time,
                  onTap: () =>
                      setState(() => _choice = _choice.copyWith(time: time)),
                ),
            ]),
            section('how', [
              _Chip(
                key: const Key('light-autopilot-mode-make'),
                label: i18n.t('light:autopilot.mode.make_only'),
                selected: _choice.mode == 'make_only',
                onTap: () => setState(
                  () => _choice = _choice.copyWith(mode: 'make_only'),
                ),
              ),
              if (widget.allowPublish)
                _Chip(
                  key: const Key('light-autopilot-mode-publish'),
                  label: widget.canPublish
                      ? i18n.t('light:autopilot.mode.make_and_publish')
                      : i18n.t('light:autopilot.mode.publishLocked'),
                  selected: _choice.mode == 'make_and_publish',
                  onTap: widget.canPublish
                      ? () => setState(
                          () => _choice = _choice.copyWith(
                            mode: 'make_and_publish',
                          ),
                        )
                      : null,
                ),
            ]),
            section('cap', [
              for (final cap in lightAutopilotCaps)
                _Chip(
                  key: Key('light-autopilot-cap-$cap'),
                  label: i18n.t(
                    'light:autopilot.capValue',
                    vars: {'value': cap},
                  ),
                  selected: _choice.creditsCap == cap,
                  onTap: () => setState(
                    () => _choice = _choice.copyWith(creditsCap: cap),
                  ),
                ),
            ]),
            const SizedBox(height: 22),
            FilledButton(
              key: const Key('light-autopilot-confirm'),
              onPressed: () => Navigator.of(context).pop(_choice),
              style: FilledButton.styleFrom(
                backgroundColor: t.a700,
                foregroundColor: t.bg,
                minimumSize: const Size.fromHeight(52),
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(Radii.full),
                ),
              ),
              child: Text(
                i18n.t('light:autopilot.confirm'),
                style: const TextStyle(
                  fontSize: FontSizes.lg,
                  fontWeight: FontWeight.w700,
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _Chip extends StatelessWidget {
  const _Chip({
    super.key,
    required this.label,
    required this.selected,
    required this.onTap,
  });

  final String label;
  final bool selected;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final enabled = onTap != null;
    return Material(
      color: selected ? t.a700 : t.card,
      shape: RoundedRectangleBorder(
        side: BorderSide(color: selected ? t.a700 : t.hair),
        borderRadius: BorderRadius.circular(Radii.full),
      ),
      child: InkWell(
        borderRadius: BorderRadius.circular(Radii.full),
        onTap: onTap,
        child: ConstrainedBox(
          constraints: const BoxConstraints(minHeight: 40),
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 9),
            child: Text(
              label,
              style: TextStyle(
                fontSize: FontSizes.base,
                fontWeight: FontWeight.w600,
                color: selected ? t.bg : (enabled ? t.ink : t.n500),
              ),
            ),
          ),
        ),
      ),
    );
  }
}
