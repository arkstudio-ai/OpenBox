import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/light_autopilot.dart';
import '../../../shared/utils/error_text.dart';
import '../../../shared/widgets/toast.dart';
import '../api/light_api.dart';
import '../state/light_providers.dart';
import 'autopilot_sheet.dart';

/// 「以后省心托管这个」 at the foot of a Light result card. On: the three
/// questions, then a job that makes this again on schedule and posts it back
/// into this conversation. Off: pauses it (cancelling lives in 托管中的事).
class LightAutopilotSwitch extends ConsumerStatefulWidget {
  const LightAutopilotSwitch({
    super.key,
    required this.sessionId,
    required this.assetId,
    required this.isVideo,
  });

  final String sessionId;
  final String assetId;
  final bool isVideo;

  @override
  ConsumerState<LightAutopilotSwitch> createState() =>
      _LightAutopilotSwitchState();
}

class _LightAutopilotSwitchState extends ConsumerState<LightAutopilotSwitch> {
  bool _busy = false;

  LightAutopilot? _job(List<LightAutopilot>? jobs) =>
      jobs?.where((j) => j.sourceAssetId == widget.assetId).firstOrNull;

  Future<void> _toggle(LightAutopilot? job, bool on) async {
    if (_busy) return;
    final i18n = ref.read(i18nProvider);
    final toast = ref.read(toastProvider.notifier);
    final api = ref.read(lightApiProvider);
    setState(() => _busy = true);
    try {
      if (!on) {
        if (job != null) await api.setAutopilotEnabled(job.id, false);
        toast.info(i18n.t('light:autopilot.paused'));
      } else if (job != null) {
        await api.setAutopilotEnabled(job.id, true);
        toast.success(i18n.t('light:autopilot.resumed'));
      } else {
        final view = ref.read(lightEntriesProvider).valueOrNull;
        final choice = await showLightAutopilotSheet(
          context,
          canPublish: view?.desktop ?? false,
          allowPublish: widget.isVideo,
        );
        if (choice == null) return;
        final made = await api.createAutopilot(
          sessionId: widget.sessionId,
          assetId: widget.assetId,
          choice: choice,
        );
        toast.success(
          i18n.t(
            'light:autopilot.started',
            vars: {'when': lightAutopilotWhen(i18n, made.repeat, made.time)},
          ),
        );
      }
      ref.invalidate(lightAutopilotsProvider);
    } on DioException catch (error) {
      toast.error(errorText(i18n, error));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final job = _job(ref.watch(lightAutopilotsProvider).valueOrNull);
    final on = job?.enabled ?? false;
    return Padding(
      padding: const EdgeInsets.only(top: 6),
      child: Row(
        mainAxisAlignment: MainAxisAlignment.end,
        children: [
          Flexible(
            child: Text(
              on && job != null
                  ? i18n.t(
                      'light:autopilot.switchOn',
                      vars: {
                        'when': lightAutopilotWhen(i18n, job.repeat, job.time),
                      },
                    )
                  : i18n.t('light:autopilot.switch'),
              textAlign: TextAlign.end,
              style: TextStyle(
                fontSize: FontSizes.sm,
                color: on ? t.ink : t.n700,
                fontWeight: on ? FontWeight.w600 : FontWeight.w400,
              ),
            ),
          ),
          const SizedBox(width: 6),
          Switch.adaptive(
            key: Key('light-autopilot-switch-${widget.assetId}'),
            value: on,
            onChanged: _busy ? null : (value) => _toggle(job, value),
          ),
        ],
      ),
    );
  }
}

/// 「每天 10:00」 for a schedule.
String lightAutopilotWhen(I18nState i18n, String repeat, String time) =>
    '${i18n.t('light:autopilot.repeat.$repeat')} $time';
