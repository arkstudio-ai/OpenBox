import 'dart:async';
import 'dart:convert';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/auth_store.dart';
import '../../../shared/api/first_video_entry.dart';
import '../../../shared/api/providers.dart';
import '../../../shared/models/first_video.dart';
import '../../../shared/models/json.dart';

/// Per-account first-video progress. The server copy
/// (`GET/PUT /api/auth/me/preferences` → `extra.first_video`) is the source
/// of truth so a new phone does not replay the flow; the local cache only
/// bridges the first paint and an offline start. Same shape as the
/// onboarding store, deliberately separate: `extra` is shallow-merged by the
/// server while `onboarding` is replaced wholesale, and the onboarding
/// normaliser drops object values.
class FirstVideoState {
  const FirstVideoState({
    required this.userId,
    required this.prefs,
    required this.loaded,
  });

  final String userId;

  /// Null until the account starts (or skips) the flow.
  final FirstVideoPrefs? prefs;

  /// Whether the server has answered (or definitively failed) for [userId].
  final bool loaded;

  bool get started => prefs != null;
  bool get skipped => prefs?.status == 'skipped';
  bool get inFlight => prefs?.inFlight ?? false;

  FirstVideoState copyWith({FirstVideoPrefs? prefs, bool? loaded}) =>
      FirstVideoState(
        userId: userId,
        prefs: prefs ?? this.prefs,
        loaded: loaded ?? this.loaded,
      );
}

class FirstVideoController extends Notifier<FirstVideoState> {
  Completer<void>? _loading;

  static String _cacheKey(String userId) => 'bossip:first_video:$userId';

  @override
  FirstVideoState build() {
    final userId = ref.watch(authProvider.select((a) => a.userId));
    _loading = null;
    // No I/O here: screens call [ensureLoaded]; passive readers see
    // `loaded == false` until then, which keeps widget tests network-free.
    return FirstVideoState(
      userId: userId,
      prefs: _readCache(userId),
      loaded: false,
    );
  }

  /// Starts the server fetch for the signed-in account once.
  void ensureLoaded() {
    if (state.loaded || _loading != null) return;
    if (!ref.read(authProvider).isAuthenticated) return;
    unawaited(_load(state.userId));
  }

  /// Resolves once the server has answered for the current account.
  Future<void> whenLoaded() async {
    if (state.loaded) return;
    ensureLoaded();
    final pending = _loading;
    if (pending != null) await pending.future;
  }

  FirstVideoPrefs? _readCache(String userId) {
    try {
      final raw = ref.read(prefsProvider).getString(_cacheKey(userId));
      if (raw == null) return null;
      return FirstVideoPrefs.fromJson(jsonDecode(raw));
    } catch (_) {
      // No prefs (a widget test without the platform overrides) or a bad
      // cache line: start empty and never block on it.
      return null;
    }
  }

  Future<void> _load(String userId) async {
    final completer = _loading = Completer<void>();
    FirstVideoPrefs? prefs;
    var answered = false;
    try {
      final response = await ref
          .read(apiDioProvider)
          .get<dynamic>('/api/auth/me/preferences');
      final data = response.data;
      prefs = data is Map<String, dynamic>
          ? FirstVideoPrefs.fromJson(asMap(data['extra'])['first_video'])
          : null;
      answered = true;
    } catch (_) {
      // Offline or a failed refresh: keep the cache.
    }
    // A user switch rebuilt the notifier; a stale answer must not land.
    if (_loading != completer || state.userId != userId) return;
    if (answered) {
      state = FirstVideoState(userId: userId, prefs: prefs, loaded: true);
      _writeCache();
    } else if (state.prefs != null) {
      state = state.copyWith(loaded: true);
    }
    if (!completer.isCompleted) completer.complete();
  }

  /// Leaving the flow for good. The only status the app writes itself; the
  /// server records submitted / ready / failed from its own side.
  Future<void> markSkipped() async {
    final skipped = FirstVideoPrefs(
      status: 'skipped',
      updatedAt: DateTime.now().toUtc(),
    );
    state = state.copyWith(prefs: skipped, loaded: true);
    _writeCache();
    ref.read(firstVideoEntryProvider.notifier).state = FirstVideoEntry.settled;
    try {
      // `extra` is shallow-merged server-side; `first_video` is replaced
      // whole, so send the complete object.
      await ref
          .read(apiDioProvider)
          .put<dynamic>(
            '/api/auth/me/preferences',
            data: {
              'extra': {
                'first_video': {
                  'status': 'skipped',
                  'updated_at': skipped.updatedAt!.toIso8601String(),
                },
              },
            },
          );
    } catch (_) {
      // Best-effort, same as the onboarding marks; the cache carries it
      // until the next successful load.
    }
  }

  /// Mirrors what the server just told us about the job, so screens opened
  /// before the next preferences fetch see the right status. Local only:
  /// the server already wrote its copy when it changed the job.
  void noteJob(
    FirstVideoJob job, {
    String? template,
    String? shopName,
    String? headline,
  }) {
    if (!job.started) return;
    final current = state.prefs;
    final next = (current ?? const FirstVideoPrefs(status: 'none')).copyWith(
      status: job.status,
      sessionId: job.sessionId,
      jobId: job.jobId,
      assetId: job.assetId,
      template: template,
      shopName: shopName,
      headline: headline,
      updatedAt: DateTime.now().toUtc(),
    );
    state = state.copyWith(prefs: next);
    _writeCache();
    if (!job.inFlight) {
      ref.read(firstVideoEntryProvider.notifier).state =
          FirstVideoEntry.settled;
    }
  }

  void _writeCache() {
    try {
      final prefs = ref.read(prefsProvider);
      final value = state.prefs;
      unawaited(
        value == null
            ? prefs.remove(_cacheKey(state.userId))
            : prefs.setString(
                _cacheKey(state.userId),
                jsonEncode(value.toJson()),
              ),
      );
    } catch (_) {
      // Same as the read: an environment without prefs simply has no cache.
    }
  }
}

final firstVideoProvider =
    NotifierProvider<FirstVideoController, FirstVideoState>(
      FirstVideoController.new,
    );
