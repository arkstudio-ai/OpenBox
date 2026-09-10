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

/// Global auth store, mirroring frontend-v2 `shared/api/auth-store.ts`:
/// access token in memory only (on [AuthSession]); refresh token rides the
/// cookie jar; `isLoading` is true until the boot refresh settles.
class AuthState {
  const AuthState({this.user, this.isLoading = true, this.mobileSessionId});

  final String? mobileSessionId;

  final AuthUser? user;
  final bool isLoading;

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
        isLoading: false,
      );
    }
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
    final logtoConfig = ref.read(logtoSsoProvider.future);
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
  /// access token or null (→ signed out). Wired into [AuthSession] so
  /// concurrent 401s share one round-trip.
  Future<String?> _doRefresh() async {
    final dio = ref.read(refreshDioProvider);
    final revision = ref.read(authSessionProvider).revision;
    try {
      final refreshResp = await dio.post<Map<String, dynamic>>(
        '/api/auth/refresh',
      );
      if (revision != ref.read(authSessionProvider).revision) return null;
      final token = asString(refreshResp.data?['access_token']);
      if (token == null) {
        clearAuth();
        return null;
      }
      final meResp = await dio.get<Map<String, dynamic>>(
        '/api/auth/me',
        options: Options(headers: {'Authorization': 'Bearer $token'}),
      );
      final authSession = ref.read(authSessionProvider);
      if (revision != authSession.revision) return null;
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
      if (revision != ref.read(authSessionProvider).revision) return null;
      final code = ApiError.fromDio(error).code;
      if (code == 'AUTH_MOBILE_SESSION_REPLACED' ||
          code == 'AUTH_MOBILE_LOGIN_REQUIRED') {
        invalidateMobileSession(code);
      } else {
        clearAuth();
      }
      return null;
    }
  }
}

final authProvider = NotifierProvider<AuthController, AuthState>(
  AuthController.new,
);
