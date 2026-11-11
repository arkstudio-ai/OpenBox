import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../models/json.dart';
import '../models/ui_mode.dart';
import 'auth_store.dart';
import 'providers.dart';

/// Per-account interface mode (docs `LIGHT_MODE_IMPL_PLAN.md` §4.2 T1). The
/// server copy (`extra.ui_mode`, read and written through `/api/light/mode`)
/// is the truth; a device cache bridges the first paint and an offline start
/// so the router can pick the right home before the network answers.
///
/// Lives in shared/ rather than in the light feature because the router, the
/// workspace shell, Settings and the Light tabs all read it, and features
/// never import each other.
class UiModeState {
  const UiModeState({
    required this.userId,
    required this.mode,
    required this.loaded,
  });

  final String userId;
  final UiMode mode;

  /// Whether the server has answered for [userId] this run.
  final bool loaded;

  bool get isLight => mode == UiMode.light;

  UiModeState copyWith({UiMode? mode, bool? loaded}) => UiModeState(
    userId: userId,
    mode: mode ?? this.mode,
    loaded: loaded ?? this.loaded,
  );
}

class UiModeController extends Notifier<UiModeState> {
  Completer<void>? _loading;

  static String cacheKey(String userId) => 'bossip:ui_mode:$userId';

  @override
  UiModeState build() {
    final userId = ref.watch(authProvider.select((a) => a.userId));
    _loading = null;
    // No I/O here: the app shell calls [ensureLoaded] once signed in.
    // Widget tests without preferences see `pro`, which is today's UI.
    return UiModeState(
      userId: userId,
      mode: _readCache(userId) ?? UiMode.pro,
      loaded: false,
    );
  }

  UiMode? _readCache(String userId) {
    try {
      final raw = ref.read(prefsProvider).getString(cacheKey(userId));
      return raw == null ? null : uiModeFromWire(raw);
    } catch (_) {
      return null;
    }
  }

  void _writeCache() {
    try {
      unawaited(
        ref
            .read(prefsProvider)
            .setString(cacheKey(state.userId), state.mode.wire),
      );
    } catch (_) {
      // An environment without preferences simply has no cache.
    }
  }

  /// Starts the server fetch for the signed-in account once per run.
  void ensureLoaded() {
    if (state.loaded || _loading != null) return;
    if (!ref.read(authProvider).isAuthenticated) return;
    unawaited(_load(state.userId));
  }

  Future<void> whenLoaded() async {
    if (state.loaded) return;
    ensureLoaded();
    final pending = _loading;
    if (pending != null) await pending.future;
  }

  Future<void> _load(String userId) async {
    final completer = _loading = Completer<void>();
    UiMode? mode;
    try {
      final response = await ref
          .read(apiDioProvider)
          .get<Map<String, dynamic>>('/api/light/mode');
      mode = uiModeFromWire(asString(response.data?['ui_mode']));
    } catch (_) {
      // Offline or an older server: keep the cache (or today's UI).
    }
    if (_loading != completer || state.userId != userId) return;
    if (mode != null) {
      state = UiModeState(userId: userId, mode: mode, loaded: true);
      _writeCache();
    }
    if (!completer.isCompleted) completer.complete();
  }

  /// Writes the device cache for an account about to be signed in, so the
  /// rebuild that `setAuth` causes already reads the right mode.
  void prime(String userId, UiMode mode) {
    try {
      unawaited(ref.read(prefsProvider).setString(cacheKey(userId), mode.wire));
    } catch (_) {
      // See [_writeCache].
    }
  }

  /// What a sign-in just learned from the account preferences. Marks the
  /// account loaded so the cold-start fetch is not repeated this run.
  void adopt(UiMode mode) {
    state = UiModeState(userId: state.userId, mode: mode, loaded: true);
    _writeCache();
  }

  /// The person's own switch. Optimistic: the interface changes at once,
  /// and comes back if the server refuses.
  Future<void> setMode(UiMode mode) async {
    final before = state;
    if (before.mode == mode && before.loaded) return;
    state = before.copyWith(mode: mode, loaded: true);
    _writeCache();
    try {
      await ref
          .read(apiDioProvider)
          .put<dynamic>('/api/light/mode', data: {'ui_mode': mode.wire});
    } catch (_) {
      if (state.userId == before.userId) {
        state = before;
        _writeCache();
      }
      rethrow;
    }
  }
}

final uiModeProvider = NotifierProvider<UiModeController, UiModeState>(
  UiModeController.new,
);
