import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../models/auth_user.dart';
import '../models/json.dart';
import '../ws/ws_client.dart';
import 'providers.dart';

const _singleUserAccessToken = 'openbox-single-user';

/// Global auth store, mirroring frontend-v2 `shared/api/auth-store.ts`:
/// access token in memory only (on [AuthSession]); refresh token rides the
/// cookie jar; `isLoading` is true until the boot refresh settles.
class AuthState {
  const AuthState({this.user, this.isLoading = true});

  final AuthUser? user;
  final bool isLoading;

  bool get isAuthenticated => user != null;

  String get userId => user?.id ?? 'anonymous';
}

class AuthController extends Notifier<AuthState> {
  @override
  AuthState build() {
    ref.read(authSessionProvider).refreshFn = _doRefresh;
    return const AuthState();
  }

  /// Boot-time silent sign-in (web `refreshAccessToken()` before first
  /// paint): tries the refresh cookie; settles `isLoading`.
  Future<void> bootstrap() async {
    await ref.read(authSessionProvider).refresh();
    if (state.isLoading) state = AuthState(user: state.user, isLoading: false);
  }

  void setAuth(String accessToken, AuthUser user) {
    ref.read(authSessionProvider).accessToken = accessToken;
    state = AuthState(user: user, isLoading: false);
  }

  void clearAuth() {
    ref.read(authSessionProvider).accessToken = null;
    state = const AuthState(isLoading: false);
  }

  /// Sign out: server logout (best-effort), WS teardown, local clear
  /// (web `UserRow` sign-out flow).
  Future<void> signOut() async {
    try {
      await ref.read(apiDioProvider).post<dynamic>('/api/auth/logout');
    } catch (_) {
      // Sign out locally regardless.
    }
    ref.read(wsClientProvider).disconnect();
    clearAuth();
  }

  /// POST /api/auth/refresh (cookie) → GET /api/auth/me. Returns the new
  /// access token or null (→ signed out). When credential routes are absent,
  /// the public bootstrap contract may authorize the trusted single-user mode.
  /// Wired into [AuthSession] so concurrent 401s share one round-trip.
  Future<String?> _doRefresh() async {
    final dio = ref.read(refreshDioProvider);
    try {
      final refreshResp = await dio.post<Map<String, dynamic>>(
        '/api/auth/refresh',
      );
      final token = asString(refreshResp.data?['access_token']);
      if (token == null) {
        return _bootstrapSingleUser(dio);
      }
      final meResp = await dio.get<Map<String, dynamic>>(
        '/api/auth/me',
        options: Options(headers: {'Authorization': 'Bearer $token'}),
      );
      ref.read(authSessionProvider).accessToken = token;
      state = AuthState(
        user: AuthUser.fromJson(meResp.data ?? const {}),
        isLoading: false,
      );
      return token;
    } on DioException {
      return _bootstrapSingleUser(dio);
    }
  }

  Future<String?> _bootstrapSingleUser(Dio dio) async {
    try {
      final response = await dio.get<Map<String, dynamic>>(
        '/api/auth/bootstrap',
      );
      final payload = response.data;
      final rawUser = payload?['user'];
      if (asString(payload?['mode']) != 'single_user' ||
          rawUser is! Map<String, dynamic>) {
        clearAuth();
        return null;
      }

      final user = AuthUser.fromJson(rawUser);
      if (user.id.isEmpty || user.username.isEmpty) {
        clearAuth();
        return null;
      }

      setAuth(_singleUserAccessToken, user);
      return _singleUserAccessToken;
    } on DioException {
      clearAuth();
      return null;
    }
  }
}

final authProvider = NotifierProvider<AuthController, AuthState>(
  AuthController.new,
);
