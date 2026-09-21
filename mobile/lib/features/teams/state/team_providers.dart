import 'dart:async';
import 'dart:math' as math;

import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/api_error.dart';
import '../../../shared/events/app_lifecycle.dart';
import '../../../shared/models/json.dart';
import '../../../shared/models/team.dart';
import '../../../shared/ws/ws_client.dart';
import '../api/teams_api.dart';

typedef TeamRunKey = ({TeamScope scope, String runId});
typedef TeamSessionKey = ({TeamScope scope, String sessionId});

final teamTemplatesProvider = FutureProvider.autoDispose
    .family<List<TeamTemplate>, TeamScope>((ref, scope) async {
      final cancel = CancelToken();
      ref.onDispose(cancel.cancel);
      final api = ref.watch(teamsApiProvider);
      final templates = <TeamTemplate>[];
      String? cursor;
      final seen = <String>{};
      do {
        final page = await api.templates(scope, cursor: cursor, cancel: cancel);
        templates.addAll(
          asList(page['items']).map(asMap).map(TeamTemplate.fromJson),
        );
        cursor = asString(page['next_cursor']);
      } while (cursor != null && cursor.isNotEmpty && seen.add(cursor));
      return templates;
    });

/// Only a viewed run is watched. Events are wake hints; a fresh durable
/// snapshot is authoritative. Coalesce hints and retain old data on errors.
class TeamRunController
    extends AutoDisposeFamilyAsyncNotifier<TeamSnapshot, TeamRunKey> {
  bool _disposed = false;
  bool _reading = false;
  bool _again = false;
  int _hintSeq = 0;
  late CancelToken _cancel;
  Timer? _initialCatchUp;

  @override
  Future<TeamSnapshot> build(TeamRunKey key) async {
    _disposed = false;
    _reading = false;
    _again = false;
    _hintSeq = 0;
    final api = ref.watch(teamsApiProvider);
    final ws = ref.watch(wsClientProvider);
    final cancel = _cancel = CancelToken();
    final sub = ws.events.listen((event) {
      if (event.type == '__connected') {
        unawaited(refresh());
      } else if (event.type == 'team.run.updated' &&
          event.data['teamRunId'] == key.runId) {
        _hintSeq = math.max(asInt(event.data['seq']) ?? 0, _hintSeq);
        unawaited(refresh());
      }
    });
    ref.listen(appVisibleProvider, (_, visible) {
      if (visible) unawaited(refresh());
    });
    final timer = Timer.periodic(const Duration(seconds: 5), (_) {
      if (ref.read(appVisibleProvider) &&
          !(state.valueOrNull?.run.terminal ?? false)) {
        unawaited(refresh());
      }
    });
    ref.onDispose(() {
      _disposed = true;
      cancel.cancel();
      timer.cancel();
      _initialCatchUp?.cancel();
      unawaited(sub.cancel());
    });
    unawaited(ws.connect());
    final snapshot = await api.snapshot(key.scope, key.runId, cancel: cancel);
    if ((_again || _hintSeq > snapshot.seq) && !cancel.isCancelled) {
      // The event queue runs after AsyncNotifier publishes the initial value.
      // A microtask can still observe AsyncLoading and lose this wake-up.
      _initialCatchUp = Timer(Duration.zero, () => unawaited(refresh()));
    }
    return snapshot;
  }

  Future<void> refresh() async {
    if (_disposed || !ref.read(appVisibleProvider)) return;
    if (_reading || state.isLoading) {
      _again = true;
      return;
    }
    _reading = true;
    final cancel = _cancel;
    try {
      do {
        _again = false;
        final api = ref.read(teamsApiProvider);
        final current = state.valueOrNull;
        if (current == null) {
          final value = await api.snapshot(
            arg.scope,
            arg.runId,
            cancel: cancel,
          );
          if (!_disposed && !cancel.isCancelled) state = AsyncData(value);
        } else {
          final events = await api.read(
            arg.scope,
            '/api/team-runs/${Uri.encodeComponent(arg.runId)}/events',
            query: {'after_seq': current.seq},
            cancel: cancel,
          );
          if ((asInt(events['last_seq']) ?? 0) > current.seq) {
            final next = await api.snapshot(
              arg.scope,
              arg.runId,
              cancel: cancel,
            );
            if (!_disposed &&
                !cancel.isCancelled &&
                next.seq >= (state.valueOrNull?.seq ?? 0)) {
              state = AsyncData(next);
            }
          } else if (!_disposed && !cancel.isCancelled && state.hasError) {
            state = AsyncData(current);
          }
        }
      } while (_again &&
          !_disposed &&
          !cancel.isCancelled &&
          ref.read(appVisibleProvider));
    } catch (error, stack) {
      if (!_disposed && !cancel.isCancelled) {
        if (apiErrorOf(error)?.code == 'INVALID_EVENT_WATERMARK') {
          try {
            final value = await ref
                .read(teamsApiProvider)
                .snapshot(arg.scope, arg.runId, cancel: cancel);
            if (!_disposed && !cancel.isCancelled) state = AsyncData(value);
          } catch (error, stack) {
            if (!_disposed && !cancel.isCancelled) {
              state = AsyncError<TeamSnapshot>(
                error,
                stack,
              ).copyWithPrevious(state);
            }
          }
        } else {
          state = AsyncError<TeamSnapshot>(
            error,
            stack,
          ).copyWithPrevious(state);
        }
      }
    } finally {
      _reading = false;
    }
  }
}

final teamRunProvider = AsyncNotifierProvider.autoDispose
    .family<TeamRunController, TeamSnapshot, TeamRunKey>(TeamRunController.new);

final sessionTeamRunsProvider = FutureProvider.autoDispose
    .family<List<TeamRun>, TeamSessionKey>((ref, key) async {
      final cancel = CancelToken();
      ref.onDispose(cancel.cancel);
      final sub = ref.watch(wsClientProvider).events.listen((event) {
        if (event.type == '__connected' ||
            (event.type == 'team.run.updated' &&
                event.sessionId == key.sessionId)) {
          ref.invalidateSelf();
        }
      });
      ref.onDispose(sub.cancel);
      final api = ref.watch(teamsApiProvider);
      final runs = <TeamRun>[];
      final seen = <String>{};
      String? cursor;
      do {
        final page = await api.read(
          key.scope,
          '/api/team-runs',
          query: {'session_id': key.sessionId, 'cursor': ?cursor},
          cancel: cancel,
        );
        runs.addAll(asList(page['items']).map(asMap).map(TeamRun.fromJson));
        cursor = asString(page['next_cursor']);
      } while (cursor != null && cursor.isNotEmpty && seen.add(cursor));
      return runs;
    });
