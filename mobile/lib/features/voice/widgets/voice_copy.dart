import '../../../shared/i18n/i18n.dart';
import '../state/voice_call_state.dart';

/// The words for a call, shared by the page, the call bar and the end
/// toast so they never disagree (`voice` namespace, spec §7).
abstract final class VoiceCopy {
  /// The main state line.
  static String status(I18nState i18n, VoiceCallState call) =>
      switch (call.status) {
        VoiceCallStatus.idle => '',
        VoiceCallStatus.requestingMic => i18n.t('voice:state.requestingMic'),
        VoiceCallStatus.connecting => i18n.t('voice:state.connecting'),
        VoiceCallStatus.connected => phase(i18n, call),
        VoiceCallStatus.paused => i18n.t('voice:state.paused'),
        VoiceCallStatus.ending => i18n.t('voice:state.ending'),
        VoiceCallStatus.ended => endHeadline(i18n, call.end),
      };

  static String phase(I18nState i18n, VoiceCallState call) =>
      call.phase == VoicePhase.working
      ? i18n.t(call.late ? 'voice:state.late' : 'voice:state.working')
      : i18n.t('voice:state.${call.phase.name}');

  /// The small second line: a request still with the assistant while the
  /// call does something else, or what paused the call.
  static String? detail(I18nState i18n, VoiceCallState call) {
    if (call.status == VoiceCallStatus.paused) {
      return i18n.t(
        call.pauseCause == VoicePauseCause.background
            ? 'voice:interruption.background'
            : 'voice:interruption.phoneCall',
      );
    }
    if (call.status == VoiceCallStatus.connected &&
        call.working &&
        call.phase != VoicePhase.working) {
      return i18n.t(call.late ? 'voice:state.late' : 'voice:state.working');
    }
    return null;
  }

  static String endHeadline(I18nState i18n, VoiceCallEnd? end) =>
      i18n.t(switch (end?.reason) {
        null => 'voice:ended.title',
        VoiceEndReason.hangup => 'voice:ended.hangup',
        VoiceEndReason.error => 'voice:ended.error',
        VoiceEndReason.limit => 'voice:ended.limit',
        VoiceEndReason.quota => 'voice:ended.quota',
        VoiceEndReason.concurrent => 'voice:ended.concurrent',
        VoiceEndReason.micLost => 'voice:ended.micLost',
        VoiceEndReason.network => 'voice:ended.network',
        VoiceEndReason.unsupported => 'voice:errors.unsupported',
        VoiceEndReason.micDenied => 'voice:errors.micDenied',
        VoiceEndReason.micMissing => 'voice:errors.micMissing',
        VoiceEndReason.micBusy => 'voice:errors.micBusy',
      });

  /// Why it failed, when there is more to say than the headline: the
  /// server's own message first, else a local `voice:errors.*` line.
  static String? endDetail(I18nState i18n, VoiceCallEnd end) {
    final message = end.message?.trim();
    if (message != null && message.isNotEmpty) return message;
    final key = end.detailKey;
    return key == null ? null : i18n.t(key);
  }

  /// "时长 02:14 · 消耗约 0.0123 积分"; null when there is neither.
  static String? endFigures(I18nState i18n, VoiceCallEnd end) {
    final parts = [
      if (end.durationSeconds > 0)
        i18n.t(
          'voice:ended.duration',
          vars: {'duration': formatCallDuration(end.durationSeconds)},
        ),
      if (end.cost != null)
        i18n.t(
          'voice:ended.cost',
          vars: {'yuan': formatYuan(end.cost!.totalYuan)},
        ),
    ];
    return parts.isEmpty ? null : parts.join(' · ');
  }

  static String? pendingHint(I18nState i18n, VoiceCallEnd end) =>
      end.pendingTurns > 0
      ? i18n.t('voice:ended.pendingHint', count: end.pendingTurns)
      : null;

  /// "本次消耗 0.0035 积分": calls are paid in credits (1 credit = 1 yuan).
  static String cost(I18nState i18n, VoiceCost cost) =>
      '${i18n.t('voice:controls.cost')} '
      '${i18n.t('voice:cost.credits', vars: {'amount': formatYuan(cost.totalYuan)})}';

  /// Next to the timer once less than five minutes remain.
  static String? remaining(I18nState i18n, VoiceCallState call, int elapsed) {
    final max = call.maxSeconds;
    if (max == null || !call.live) return null;
    final left = max - elapsed;
    if (left >= 300) return null;
    final minutes = left <= 0 ? 0 : (left + 59) ~/ 60;
    return i18n.t('voice:duration.remaining', vars: {'minutes': minutes});
  }
}
