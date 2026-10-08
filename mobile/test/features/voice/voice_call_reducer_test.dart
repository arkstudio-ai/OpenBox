import 'package:bossip_mobile/features/voice/api/voice_events.dart';
import 'package:bossip_mobile/features/voice/state/voice_call_reducer.dart';
import 'package:bossip_mobile/features/voice/state/voice_call_state.dart';
import 'package:bossip_mobile/shared/models/app_config.dart';
import 'package:flutter_test/flutter_test.dart';

final _t0 = DateTime(2026, 10, 7, 9);

VoiceServerEvent _event(Map<String, dynamic> json) =>
    VoiceServerEvent.fromJson(json)!;

VoiceCallState _connecting() =>
    const VoiceCallState(status: VoiceCallStatus.connecting);

VoiceCallState _connected() => reduceVoiceEvent(
  _connecting(),
  _event({'type': 'ready', 'call_id': 'call-1', 'max_seconds': 1200}),
  now: _t0,
);

void main() {
  group('server events parse to the contract', () {
    test('every type, unknown types and junk', () {
      expect(
        VoiceServerEvent.decode(
          '{"type":"ready","call_id":"c","max_seconds":60,'
          '"input_sample_rate":16000,"output_sample_rate":24000}',
        ),
        isA<VoiceReadyEvent>()
            .having((e) => e.callId, 'callId', 'c')
            .having((e) => e.maxSeconds, 'maxSeconds', 60)
            .having((e) => e.outputSampleRate, 'rate', 24000),
      );
      expect(
        _event({'type': 'phase', 'value': 'speaking', 'working': true}),
        isA<VoicePhaseEvent>()
            .having((e) => e.phase, 'phase', VoicePhase.speaking)
            .having((e) => e.working, 'working', isTrue)
            .having((e) => e.late, 'late', isFalse),
      );
      expect(
        _event({'type': 'playback.clear'}),
        isA<VoicePlaybackClearEvent>(),
      );
      expect(
        _event({'type': 'phrase', 'key': 'greeting'}),
        isA<VoicePhraseEvent>().having((e) => e.key, 'key', 'greeting'),
      );
      expect(
        _event({
          'type': 'turn',
          'turn_id': 't1',
          'state': 'accepted',
          'inbox_id': 'i1',
          'message_id': 'm1',
        }),
        isA<VoiceTurnEvent>()
            .having((e) => e.turn.messageId, 'messageId', 'm1')
            .having((e) => e.turn.open, 'open', isTrue),
      );
      final cost = _event({
        'type': 'cost',
        'total_yuan': '0.0123',
        'costs_yuan': {'input_audio': 0.01, 'output_audio': '0.0023'},
        'settled_rounds': 2,
        'unreported_rounds': 1,
        'pending': true,
        'final': false,
      });
      expect(
        cost,
        isA<VoiceCostEvent>()
            .having((e) => e.cost.totalYuan, 'total', closeTo(0.0123, 1e-9))
            .having((e) => e.cost.costsYuan['output_audio'], 'out', 0.0023)
            .having((e) => e.cost.costsYuan['input_text'], 'text', 0)
            .having((e) => e.cost.partial, 'partial', isTrue),
      );
      expect(
        _event({'type': 'heartbeat', 'elapsed_seconds': 40}),
        isA<VoiceHeartbeatEvent>().having((e) => e.elapsedSeconds, 's', 40),
      );
      expect(
        _event({'type': 'limit', 'reason': 'credits'}),
        isA<VoiceLimitEvent>().having((e) => e.reason, 'r', 'credits'),
      );
      expect(
        _event({'type': 'error', 'code': 'provider_error', 'message': '稍后'}),
        isA<VoiceErrorEvent>().having((e) => e.message, 'message', '稍后'),
      );
      expect(
        _event({
          'type': 'ended',
          'reason': 'mic_lost',
          'duration_seconds': 9,
          'pending_turns': 1,
          'cost': {'total_yuan': 0.5},
        }),
        isA<VoiceEndedEvent>()
            .having((e) => e.reason, 'reason', VoiceEndReason.micLost)
            .having((e) => e.cost?.totalYuan, 'cost', 0.5),
      );
      expect(_event({'type': 'caption'}), isA<VoiceUnknownEvent>());
      expect(VoiceServerEvent.decode('not json'), isNull);
      expect(VoiceServerEvent.decode('[1, 2]'), isNull);
      expect(VoiceServerEvent.decode('{"no":"type"}'), isNull);
    });

    test('an end reason from a newer server reads as an error', () {
      expect(VoiceEndReason.parse('teleported'), VoiceEndReason.error);
      expect(VoiceEndReason.parse('mic_busy'), VoiceEndReason.micBusy);
    });
  });

  group('reducer', () {
    test('ready connects into greeting with the call details', () {
      final state = _connected();
      expect(state.status, VoiceCallStatus.connected);
      expect(state.phase, VoicePhase.greeting);
      expect(state.callId, 'call-1');
      expect(state.maxSeconds, 1200);
      expect(state.connectedAt, _t0);
      expect(state.live, isTrue);
    });

    test('ready only counts while connecting', () {
      const idle = VoiceCallState();
      final ready = _event({'type': 'ready', 'call_id': 'x'});
      expect(reduceVoiceEvent(idle, ready, now: _t0), same(idle));
      final connected = _connected();
      expect(reduceVoiceEvent(connected, ready, now: _t0), same(connected));
    });

    test('phase events are the only source of the phase', () {
      var state = _connected();
      state = reduceVoiceEvent(
        state,
        _event({'type': 'phase', 'value': 'listening'}),
        now: _t0,
      );
      expect(state.phase, VoicePhase.listening);
      state = reduceVoiceEvent(
        state,
        _event({'type': 'phase', 'value': 'working', 'working': true}),
        now: _t0,
      );
      expect(
        (state.phase, state.working, state.late),
        (VoicePhase.working, true, false),
      );
      // Chatting while a request is still out: working stays a side mark.
      state = reduceVoiceEvent(
        state,
        _event({
          'type': 'phase',
          'value': 'speaking',
          'working': true,
          'late': true,
        }),
        now: _t0,
      );
      expect(
        (state.phase, state.working, state.late),
        (VoicePhase.speaking, true, true),
      );
      // Ignored before the call is up.
      final dialling = reduceVoiceEvent(
        _connecting(),
        _event({'type': 'phase', 'value': 'speaking'}),
        now: _t0,
      );
      expect(dialling.phase, VoicePhase.greeting);
    });

    test('turns accumulate by id and count while open', () {
      var state = _connected();
      for (final (id, turnState) in [
        ('t1', 'accepted'),
        ('t2', 'accepted'),
        ('t1', 'delivered'),
        ('t3', 'late'),
      ]) {
        state = reduceVoiceEvent(
          state,
          _event({'type': 'turn', 'turn_id': id, 'state': turnState}),
          now: _t0,
        );
      }
      expect(state.turns.keys, ['t1', 't2', 't3']);
      expect(state.turns['t1']!.state, 'delivered');
      expect(state.openTurns, 2);
    });

    test('limit closes with the limit reason; ended follows', () {
      var state = reduceVoiceEvent(
        _connected(),
        _event({'type': 'limit', 'reason': 'max_duration'}),
        now: _t0,
      );
      expect(state.status, VoiceCallStatus.ending);
      expect(state.endingReason, VoiceEndReason.limit);
      // A close without `ended` still reads as the limit.
      expect(voiceCloseOutcome(1006, state).reason, VoiceEndReason.limit);
      state = reduceVoiceEvent(
        state,
        _event({'type': 'ended', 'reason': 'limit', 'duration_seconds': 1800}),
        now: _t0,
      );
      expect(state.status, VoiceCallStatus.ended);
      expect(state.end!.reason, VoiceEndReason.limit);
      expect(state.end!.durationSeconds, 1800);
    });

    test('error ends at once and keeps the server message', () {
      final state = reduceVoiceEvent(
        _connected(),
        _event({'type': 'error', 'code': 'provider_error', 'message': '没接通'}),
        now: _t0.add(const Duration(seconds: 42)),
      );
      expect(state.status, VoiceCallStatus.ended);
      expect(state.end!.reason, VoiceEndReason.error);
      expect(state.end!.message, '没接通');
      expect(state.end!.durationSeconds, 42);
    });

    test('an error while hanging up is still a hang-up', () {
      final hangingUp = _connected().copyWith(
        status: VoiceCallStatus.ending,
        endingReason: VoiceEndReason.hangup,
      );
      final state = reduceVoiceEvent(
        hangingUp,
        _event({'type': 'error', 'code': 'internal', 'message': 'x'}),
        now: _t0,
      );
      expect(state.end!.reason, VoiceEndReason.hangup);
      expect(state.end!.message, isNull);
    });

    test('ended passes reason, duration, pending turns and cost through', () {
      final state = reduceVoiceEvent(
        _connected(),
        _event({
          'type': 'ended',
          'reason': 'hangup',
          'duration_seconds': 134,
          'pending_turns': 2,
          'cost': {'total_yuan': 0.0035, 'final': true},
        }),
        now: _t0,
      );
      expect(state.status, VoiceCallStatus.ended);
      final end = state.end!;
      expect(
        (end.reason, end.durationSeconds, end.pendingTurns),
        (VoiceEndReason.hangup, 134, 2),
      );
      expect(end.cost!.totalYuan, 0.0035);
      expect(end.cost!.isFinal, isTrue);
    });

    test('a late ended or cost fills in a local end without changing why', () {
      var state = endVoiceCall(
        _connected(),
        VoiceEndReason.network,
        now: _t0.add(const Duration(seconds: 5)),
      );
      expect(state.end!.durationSeconds, 5);
      state = reduceVoiceEvent(
        state,
        _event({'type': 'ended', 'reason': 'error', 'duration_seconds': 6}),
        now: _t0,
      );
      expect(state.end!.reason, VoiceEndReason.network);
      expect(state.end!.durationSeconds, 6);
      state = reduceVoiceEvent(
        state,
        _event({'type': 'cost', 'total_yuan': 0.2, 'final': true}),
        now: _t0,
      );
      expect(state.end!.cost!.totalYuan, 0.2);
    });

    test('a local end counts open requests and time since ready', () {
      var state = _connected();
      state = reduceVoiceEvent(
        state,
        _event({'type': 'turn', 'turn_id': 't1', 'state': 'working'}),
        now: _t0,
      );
      state = endVoiceCall(
        state,
        VoiceEndReason.hangup,
        now: _t0.add(const Duration(minutes: 2, seconds: 14)),
      );
      expect(state.end!.durationSeconds, 134);
      expect(state.end!.pendingTurns, 1);
      // Ending twice changes nothing.
      expect(endVoiceCall(state, VoiceEndReason.error, now: _t0), same(state));
    });

    test('redial is offered only where the web offers it', () {
      for (final reason in VoiceEndReason.values) {
        expect(
          VoiceCallEnd(reason: reason).canRedial,
          {
            VoiceEndReason.hangup,
            VoiceEndReason.error,
            VoiceEndReason.network,
            VoiceEndReason.limit,
          }.contains(reason),
          reason: reason.wire,
        );
      }
    });
  });

  group('close codes (spec §5.5)', () {
    final live = _connected();
    final dialling = _connecting();

    test('map to end reasons and explanations', () {
      expect(voiceCloseOutcome(4009, live).reason, VoiceEndReason.concurrent);
      expect(voiceCloseOutcome(4029, live).reason, VoiceEndReason.quota);
      expect(voiceCloseOutcome(4503, dialling), (
        reason: VoiceEndReason.error,
        detailKey: 'voice:errors.disabled',
      ));
      expect(voiceCloseOutcome(4001, dialling), (
        reason: VoiceEndReason.error,
        detailKey: 'voice:errors.connectFailed',
      ));
      expect(
        voiceCloseOutcome(1011, live).detailKey,
        'voice:errors.connectFailed',
      );
      expect(
        voiceCloseOutcome(4404, dialling).detailKey,
        'voice:errors.assistantUnavailable',
      );
      for (final code in [4003, 4400, 1000]) {
        expect(voiceCloseOutcome(code, live), (
          reason: VoiceEndReason.error,
          detailKey: null,
        ));
      }
    });

    test('a dropped socket is the network once connected', () {
      expect(voiceCloseOutcome(1006, live).reason, VoiceEndReason.network);
      expect(voiceCloseOutcome(null, live).reason, VoiceEndReason.network);
      expect(voiceCloseOutcome(null, dialling), (
        reason: VoiceEndReason.error,
        detailKey: 'voice:errors.connectFailed',
      ));
    });

    test('a hang-up under way keeps its reason', () {
      final hangingUp = live.copyWith(
        status: VoiceCallStatus.ending,
        endingReason: VoiceEndReason.hangup,
      );
      expect(voiceCloseOutcome(1011, hangingUp).reason, VoiceEndReason.hangup);
    });
  });

  test('the entry follows GET /api/agent/config voice_enabled', () {
    expect(AppConfig.fromJson({'voice_enabled': true}).voiceEnabled, isTrue);
    expect(AppConfig.fromJson({'voice_enabled': false}).voiceEnabled, isFalse);
    // An older backend says nothing: no entry.
    expect(AppConfig.fromJson({'models': <Object>[]}).voiceEnabled, isFalse);
  });

  test('durations and money read the way the call shows them', () {
    expect(formatCallDuration(0), '00:00');
    expect(formatCallDuration(134), '02:14');
    expect(formatCallDuration(3725), '1:02:05');
    expect(formatCallDuration(-3), '00:00');
    expect(formatYuan(0.00349), '0.0035');
  });
}
