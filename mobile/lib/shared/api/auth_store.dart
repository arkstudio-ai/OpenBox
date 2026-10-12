import 'dart:async';

import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../i18n/i18n.dart';
import '../models/auth_user.dart';
import '../models/json.dart';
import '../notifications/system_notifications.dart';
import '../widgets/toast.dart';
import '../ws/ws_client.dart';
import 'api_error.dart';
import 'logto_session.dart';
import 'providers.dart';

/// The backend's refresh cookie (`backend/auth/routes.py`, path `/api/auth`).
const _refreshCookie = 'refresh_token';

/// Global auth store, mirroring frontend-v2 `shared/api/auth-store.ts`:
/// access token in memory only (on [AuthSession]); refresh token rides the
/// cookie jar; `isLoading` is true until the boot refresh settles.
class AuthState {
  const AuthState({
    this.user,
    this.isLoading = true,
    this.mobileSessionId,
    this.offline = false,
  });

  final String? mobileSessionId;

  final AuthUser? user;
  final bool isLoading;

  /// The app started with a saved sign-in that the server could not check —
  /// offline, a timeout or a server error. That is not a sign-out: the
  /// refresh cookie is kept, nobody is sent to the login page, and
  /// [AuthController.retryRestore] asks again.
  final bool offline;

  bool get isAuthenticated => user != null;

  String get userId => user?.id ?? 'anonymous';
}

class AuthController extends Notifier<AuthState> {
  @override
  AuthState build() {
    ref.read(authSessionProvider).refreshFn = _doRefresh;
    ref.read(authSessionProvider).invalidateFn = invalidateMobileSession;
    return const AuthState();
  }

  /// Boot-time silent sign-in (web `refreshAccessToken()` before first
  /// paint): tries the refresh cookie; settles `isLoading`.
  Future<void> bootstrap() async {
    await ref.read(authSessionProvider).refresh();
    if (state.isLoading) {
      state = AuthState(
        user: state.user,
        mobileSessionId: state.mobileSessionId,
        offline: state.offline,
        isLoading: false,
      );
    }
  }

  /// The offline screen's retry: the boot refresh again, with its spinner.
  Future<void> retryRestore() async {
    if (!state.offline || state.isLoading) return;
    state = const AuthState(offline: true);
    await bootstrap();
  }

  void setAuth(String accessToken, AuthUser user, {String? mobileSessionId}) {
    final session = ref.read(authSessionProvider);
    session.revision++;
    session.mobileSessionId = mobileSessionId;
    session.accessToken = accessToken;
    session.userId = user.id;
    state = AuthState(
      user: user,
      mobileSessionId: mobileSessionId,
      isLoading: false,
    );
  }

  void clearAuth() {
    final session = ref.read(authSessionProvider);
    session.revision++;
    session.mobileSessionId = null;
    session.accessToken = null;
    session.userId = null;
    ref.read(wsClientProvider).disconnect();
    ref.read(workspaceScopeProvider).clear();
    state = const AuthState(isLoading: false);
  }

  void invalidateMobileSession(String code) {
    final hadUser = state.isAuthenticated;
    clearAuth();
    unawaited(ref.read(cookieJarProvider).deleteAll());
    unawaited(clearLocalLogtoTokens().catchError((Object _) {}));
    unawaited(ref.read(systemNotificationsProvider).clear());
    if (hadUser) {
      ref
          .read(toastProvider.notifier)
          .push(ToastKind.warning, ref.read(i18nProvider).t('errors:$code'));
    }
  }

  /// Sign out of both OpenBox and Logto. OpenBox state is always cleared even
  /// if either server is unavailable; Logto's SDK also removes native tokens
  /// in its own best-effort cleanup path.
  Future<void> signOut() async {
    // A config fetch that failed earlier is cached as an error; ask again.
    if (ref.read(logtoSsoProvider).hasError) ref.invalidate(logtoSsoProvider);
    // Still unreachable → null: the SDK then only clears its tokens here.
    final logtoConfig = ref
        .read(logtoSsoProvider.future)
        .then<LogtoSso?>((config) => config, onError: (Object _) => null);
    try {
      await ref.read(apiDioProvider).post<dynamic>('/api/auth/logout');
    } catch (_) {
      // Sign out locally regardless.
    }
    ref.read(wsClientProvider).disconnect();
    clearAuth();
    await ref.read(cookieJarProvider).deleteAll();
    unawaited(ref.read(systemNotificationsProvider).clear());
    try {
      await ref.read(logtoSessionProvider).signOut(await logtoConfig);
    } catch (_) {
      // The local OpenBox and Logto token state has already been cleared. A
      // canceled/unreachable browser logout must never restore this session.
    }
  }

  /// POST /api/auth/refresh (cookie) → GET /api/auth/me. Returns the new
  /// access token, or null. Wired into [AuthSession] so concurrent 401s
  /// share one round-trip.
  ///
  /// Only the server ending the session signs out: 401/403, or the
  /// single-phone codes (which also drop the cookie). Anything else —
  /// offline, a timeout, a 5xx — says nothing about the session, so the
  /// cookie stays: see [_keepSession].
  Future<String?> _doRefresh() async {
    final dio = ref.read(refreshDioProvider);
    final revision = ref.read(authSessionProvider).revision;
    bool stale() => revision != ref.read(authSessionProvider).revision;
    try {
      final refreshResp = await dio.post<Map<String, dynamic>>(
        '/api/auth/refresh',
      );
      if (stale()) return null;
      final token = asString(refreshResp.data?['access_token']);
      if (token == null) {
        // A 200 without a token is a broken reply, not a verdict.
        await _keepSession(dio, stale);
        return null;
      }
      final meResp = await dio.get<Map<String, dynamic>>(
        '/api/auth/me',
        options: Options(headers: {'Authorization': 'Bearer $token'}),
      );
      final authSession = ref.read(authSessionProvider);
      if (stale()) return null;
      authSession.accessToken = token;
      authSession.mobileSessionId = asString(
        refreshResp.data?['mobile_session_id'],
      );
      final user = AuthUser.fromJson(meResp.data ?? const {});
      authSession.userId = user.id;
      state = AuthState(
        user: user,
        mobileSessionId: authSession.mobileSessionId,
        isLoading: false,
      );
      return token;
    } on DioException catch (error) {
      if (stale()) return null;
      final failure = ApiError.fromDio(error);
      if (failure.code == 'AUTH_MOBILE_SESSION_REPLACED' ||
          failure.code == 'AUTH_MOBILE_LOGIN_REQUIRED') {
        invalidateMobileSession(failure.code);
      } else if (failure.status == 401 || failure.status == 403) {
        clearAuth();
      } else {
        await _keepSession(dio, stale);
      }
      return null;
    }
  }

  /// A refresh that failed without the server ending the session. The user
  /// in memory is the snapshot: a signed-in user stays signed in and the
  /// next request tries again. A cold start has no snapshot, so it shows the
  /// offline screen ([AuthState.offline]) — unless there is no saved sign-in
  /// at all (a first launch without a network), which is plainly signed out.
  Future<void> _keepSession(Dio dio, bool Function() stale) async {
    if (state.isAuthenticated) return;
    final saved = await _hasSavedSession(dio);
    if (stale() || state.isAuthenticated) return;
    if (saved) {
      state = const AuthState(isLoading: false, offline: true);
    } else {
      clearAuth();
    }
  }

  Future<bool> _hasSavedSession(Dio dio) async {
    try {
      // The URL the refresh request itself sends its cookies to.
      final uri = RequestOptions(
        baseUrl: dio.options.baseUrl,
        path: '/api/auth/refresh',
      ).uri;
      final cookies = await ref.read(cookieJarProvider).loadForRequest(uri);
      return cookies.any((cookie) => cookie.name == _refreshCookie);
    } catch (_) {
      // An unreadable jar cannot restore anything.
      return false;
    }
  }
}

final authProvider = NotifierProvider<AuthController, AuthState>(
  AuthController.new,
);
