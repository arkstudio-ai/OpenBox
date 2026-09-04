import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:logto_dart_sdk/logto_dart_sdk.dart';

import '../../../shared/api/auth_store.dart';
import '../../../shared/api/providers.dart';
import '../../../shared/appearance/appearance_store.dart';
import '../../../shared/config/env.dart';
import '../../../shared/models/auth_user.dart';
import '../../../shared/models/json.dart';
import '../api/logto.dart';

/// Login/register orchestration, mirroring frontend-v2
/// `features/auth/api/auth.ts` (`useLogin`/`useRegister`/`useCompleteAuth`).
/// Sign-out lives on the shared [AuthController].
class AuthFlow {
  AuthFlow(this._ref);

  final Ref _ref;

  Future<void> login(String username, String password) async {
    final resp = await _ref.read(apiDioProvider).post<Map<String, dynamic>>(
      '/api/auth/login',
      data: {'username': username, 'password': password},
    );
    await _completeAuth(resp.data ?? const {});
  }

  Future<void> register(String username, String password, {String? email}) async {
    final resp = await _ref.read(apiDioProvider).post<Map<String, dynamic>>(
      '/api/auth/register',
      data: {
        'username': username,
        'password': password,
        if (email != null && email.isNotEmpty) 'email': email,
      },
    );
    await _completeAuth(resp.data ?? const {});
  }

  /// Sign in through Logto's hosted pages and come back with a session.
  ///
  /// Logto's own SDK drives the PKCE flow in a system browser sheet. As a
  /// public native client it redeems the code on the device, so — unlike web,
  /// where the server completes the exchange — what reaches us is the ID
  /// token; the server verifies it against Logto's JWKS before it counts.
  Future<void> loginWithLogto(LogtoSso sso, {bool register = false}) async {
    final client = LogtoClient(
      config: LogtoConfig(endpoint: sso.endpoint, appId: sso.appId),
    );
    await client.signIn(
      Env.ssoRedirectUri,
      firstScreen: register ? FirstScreen.register : FirstScreen.signIn,
    );
    final idToken = await client.idToken;
    if (idToken == null) {
      throw StateError('Logto returned no id token');
    }
    final resp = await _ref.read(apiDioProvider).post<Map<String, dynamic>>(
      '/api/auth/logto/id-token',
      data: {'id_token': idToken},
    );
    await _completeAuth(resp.data ?? const {});
  }

  /// setAuth → best-effort prefs fetch → appearance hydrate (web
  /// `useCompleteAuth`). Navigation is the caller's concern.
  Future<void> _completeAuth(Map<String, dynamic> tokenResponse) async {
    final token = asString(tokenResponse['access_token']) ?? '';
    final user = AuthUser.fromJson(asMap(tokenResponse['user']));
    _ref.read(authProvider.notifier).setAuth(token, user);
    try {
      final prefs = await _ref
          .read(apiDioProvider)
          .get<Map<String, dynamic>>('/api/auth/me/preferences');
      _ref
          .read(appearanceProvider.notifier)
          .hydrateFromServer(prefs.data ?? const {});
    } catch (_) {
      // Best-effort, same as web.
    }
  }
}

final authFlowProvider = Provider<AuthFlow>(AuthFlow.new);
