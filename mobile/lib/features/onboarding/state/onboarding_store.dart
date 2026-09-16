import 'dart:async';
import 'dart:convert';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/auth_store.dart';
import '../../../shared/api/providers.dart';

/// Guide keys (docs/MOBILE_ONBOARDING_PLAN.md §3). Values in the server map
/// are `true` once seen; `industry` carries the starter-card industry.
abstract final class Guides {
  static const welcome = 'welcome';
  static const starterCards = 'starter_cards';
  static const composer = 'composer';
  static const drawer = 'drawer';
  static const panelEntry = 'panel_entry';
  static const desktopControl = 'desktop_control';
  static const notifyPrePermission = 'notify_prepermission';
  static const desktopTakeover = 'desktop_takeover';
  static const authCenter = 'auth_center';
  // M2
  static const inbox = 'inbox';
  static const credits = 'credits';
  static const cardQuestion = 'card_question';
  static const cardPlan = 'card_plan';
  static const cardPermission = 'card_permission';
  static const cardVideoReview = 'card_video_review';
  static const industryKey = 'industry';
}

/// Per-account onboarding progress. The server copy
/// (`GET/PUT /api/auth/me/preferences` → `onboarding`) is the source of
/// truth so a new phone does not replay; the local cache only bridges the
/// first paint and an offline start.
class OnboardingState {
  const OnboardingState({
    required this.userId,
    required this.values,
    required this.loaded,
  });

  final String userId;

  /// `key → true | string`.
  final Map<String, Object> values;

  /// Whether the server has answered (or definitively failed) for [userId].
  /// Guides never trigger before that: an existing account on a new device
  /// would otherwise see the welcome again.
  final bool loaded;

  bool seen(String key) => values[key] == true;
  String? get industry => values[Guides.industryKey] as String?;

  OnboardingState copyWith({Map<String, Object>? values, bool? loaded}) =>
      OnboardingState(
        userId: userId,
        values: values ?? this.values,
        loaded: loaded ?? this.loaded,
      );
}

class OnboardingController extends Notifier<OnboardingState> {
  Completer<void>? _loading;

  static String _cacheKey(String userId) => 'bossip:onboarding:$userId';

  @override
  OnboardingState build() {
    final userId = ref.watch(authProvider.select((a) => a.userId));
    _loading = null;
    // No I/O here: screens that host guides call [ensureLoaded]. Passive
    // readers (the first-seen hints inside chat cards) just see `loaded ==
    // false` until then, which also keeps widget tests network-free.
    return OnboardingState(
      userId: userId,
      values: _readCache(userId),
      loaded: false,
    );
  }

  /// Starts the server fetch for the signed-in account once.
  void ensureLoaded() {
    if (state.loaded || _loading != null) return;
    if (!ref.read(authProvider).isAuthenticated) return;
    unawaited(_load(state.userId));
  }

  Map<String, Object> _readCache(String userId) {
    try {
      final raw = ref.read(prefsProvider).getString(_cacheKey(userId));
      if (raw == null) return const {};
      final decoded = jsonDecode(raw);
      return decoded is Map ? _normalize(decoded) : const {};
    } catch (_) {
      // No prefs (a widget test without the platform overrides) or a bad
      // cache line: start empty and never block on it.
      return const {};
    }
  }

  static Map<String, Object> _normalize(Map<dynamic, dynamic> raw) => {
    for (final entry in raw.entries)
      if (entry.value == true || entry.value is String)
        entry.key.toString(): entry.value as Object,
  };

  Future<void> _load(String userId) async {
    final completer = _loading = Completer<void>();
    Map<String, Object>? values;
    try {
      final response = await ref
          .read(apiDioProvider)
          .get<dynamic>('/api/auth/me/preferences');
      final data = response.data;
      final raw = data is Map ? data['onboarding'] : null;
      values = raw is Map ? _normalize(raw) : const {};
    } catch (_) {
      // Offline or a failed refresh: keep the cache and stop blocking guides
      // only if there is a cache; a first launch without network waits for
      // the next successful load.
      values = null;
    }
    // A user switch rebuilt the notifier; a stale answer must not land.
    if (_loading != completer || state.userId != userId) return;
    if (values != null) {
      state = state.copyWith(values: values, loaded: true);
      _writeCache();
    } else if (state.values.isNotEmpty) {
      state = state.copyWith(loaded: true);
    }
    if (!completer.isCompleted) completer.complete();
  }

  /// Resolves once the server has answered for the current account.
  Future<void> whenLoaded() async {
    if (state.loaded) return;
    ensureLoaded();
    final pending = _loading;
    if (pending != null) await pending.future;
  }

  bool shouldShow(String key) => state.loaded && !state.seen(key);

  Future<void> markSeen(String key) => _set({key: true});

  Future<void> setIndustry(String industry) =>
      _set({Guides.industryKey: industry});

  /// Settings → "重新查看新手引导": clears every account-level mark.
  Future<void> reset() async {
    state = state.copyWith(values: const {}, loaded: true);
    _writeCache();
    await _push(const {});
  }

  Future<void> _set(Map<String, Object> patch) async {
    if (patch.entries.every((e) => state.values[e.key] == e.value)) return;
    state = state.copyWith(values: {...state.values, ...patch});
    _writeCache();
    await _push(state.values);
  }

  void _writeCache() {
    try {
      unawaited(
        ref
            .read(prefsProvider)
            .setString(_cacheKey(state.userId), jsonEncode(state.values)),
      );
    } catch (_) {
      // Same as the read: an environment without prefs simply has no cache.
    }
  }

  Future<void> _push(Map<String, Object> values) async {
    try {
      await ref.read(apiDioProvider).put<dynamic>(
        '/api/auth/me/preferences',
        data: {'onboarding': values},
      );
    } catch (_) {
      // Best-effort, same as appearance sync; the cache re-syncs on the next
      // successful write.
    }
  }
}

final onboardingProvider =
    NotifierProvider<OnboardingController, OnboardingState>(
      OnboardingController.new,
    );

/// Only one guide (sheet, coach marks, pre-permission page) may be on screen.
/// Holders set their key on start and clear it on dismiss; `whenIdle`
/// resolves when nothing is showing.
class GuideQueue extends Notifier<String?> {
  final List<Completer<void>> _waiters = [];

  @override
  String? build() => null;

  bool get busy => state != null;

  /// Claims the screen for [key]; false when another guide is showing.
  bool claim(String key) {
    if (state != null) return false;
    state = key;
    return true;
  }

  void release(String key) {
    if (state != key) return;
    state = null;
    for (final w in _waiters) {
      if (!w.isCompleted) w.complete();
    }
    _waiters.clear();
  }

  Future<void> whenIdle() {
    if (state == null) return Future.value();
    final c = Completer<void>();
    _waiters.add(c);
    return c.future;
  }
}

final guideQueueProvider = NotifierProvider<GuideQueue, String?>(
  GuideQueue.new,
);

/// Device-level flag for the pre-login intro banner (there is no account to
/// attach it to). Never reset by the account-level replay.
const introSeenKey = 'bossip:intro_seen';

final introSeenProvider = StateProvider<bool>(
  (ref) => ref.read(prefsProvider).getBool(introSeenKey) ?? false,
);
