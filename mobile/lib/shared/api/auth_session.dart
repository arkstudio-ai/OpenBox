import 'dart:async';

/// In-memory access token + single-flight refresh, mirroring frontend-v2
/// `shared/api/auth-store.ts`: the access token never touches disk; the
/// refresh token lives in the HttpOnly-style cookie jar and is replayed to
/// `/api/auth/*` automatically.
class AuthSession {
  String? accessToken;

  /// Wired by the auth controller: performs POST /api/auth/refresh (+ /me)
  /// and returns the new access token, or null on failure (→ logged out).
  Future<String?> Function()? refreshFn;

  Future<String?>? _inflight;

  /// Concurrent 401s share exactly one refresh round-trip (web `doRefresh`
  /// mutex).
  Future<String?> refresh() {
    final fn = refreshFn;
    if (fn == null) return Future.value(null);
    return _inflight ??= fn().whenComplete(() => _inflight = null);
  }
}
