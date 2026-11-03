import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/auth_store.dart';
import '../../../shared/api/first_video_entry.dart';
import '../../../shared/api/logto_session.dart';
import '../../../shared/api/providers.dart';
import '../../../shared/appearance/appearance_store.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/legal/legal_links.dart';
import '../../../shared/models/auth_user.dart';
import '../../../shared/models/first_video.dart';
import '../../../shared/models/json.dart';

/// Login/register orchestration, mirroring frontend-v2
/// `features/auth/api/auth.ts` (`useLogin`/`useRegister`/`useCompleteAuth`).
/// Sign-out lives on the shared [AuthController].
class AuthFlow {
  AuthFlow(this._ref);

  final Ref _ref;

  Future<void> login(
    String username,
    String password, {
    bool acceptedLegal = false,
  }) async {
    final resp = await _ref
        .read(apiDioProvider)
        .post<Map<String, dynamic>>(
          '/api/auth/login',
          data: {'username': username, 'password': password},
        );
    await _completeAuth(resp.data ?? const {}, acceptedLegal: acceptedLegal);
  }

  Future<void> register(
    String username,
    String password, {
    String? email,
    bool acceptedLegal = false,
  }) async {
    final resp = await _ref
        .read(apiDioProvider)
        .post<Map<String, dynamic>>(
          '/api/auth/register',
          data: {
            'username': username,
            'password': password,
            if (email != null && email.isNotEmpty) 'email': email,
          },
        );
    await _completeAuth(resp.data ?? const {}, acceptedLegal: acceptedLegal);
  }

  /// Sign in through Logto's hosted pages and come back with a session.
  ///
  /// Logto's own SDK drives the PKCE flow in a system browser sheet. As a
  /// public native client it redeems the code on the device, so — unlike web,
  /// where the server completes the exchange — what reaches us is the ID
  /// token; the server verifies it against Logto's JWKS before it counts.
  Future<void> loginWithLogto(
    LogtoSso sso, {
    bool register = false,
    bool acceptedLegal = false,
  }) async {
    final idToken = await _ref
        .read(logtoSessionProvider)
        .signIn(sso, register: register);
    final resp = await _ref
        .read(apiDioProvider)
        .post<Map<String, dynamic>>(
          '/api/auth/logto/id-token',
          data: {'id_token': idToken},
        );
    await _completeAuth(resp.data ?? const {}, acceptedLegal: acceptedLegal);
  }

  /// Best-effort prefs fetch → first-video entry → setAuth → appearance
  /// hydrate (web `useCompleteAuth`). The prefs come first because the
  /// router's auth guard fires on setAuth and reads the entry right then.
  /// Navigation is the caller's concern.
  Future<void> _completeAuth(
    Map<String, dynamic> tokenResponse, {
    required bool acceptedLegal,
  }) async {
    final token = asString(tokenResponse['access_token']) ?? '';
    final user = AuthUser.fromJson(asMap(tokenResponse['user']));
    if (token.trim().isEmpty || user.id.trim().isEmpty) {
      throw const FormatException('Incomplete authentication response');
    }
    if (acceptedLegal) {
      await _ref
          .read(apiDioProvider)
          .post<Map<String, dynamic>>(
            '/api/auth/me/legal-consent',
            data: {
              'version': legalVersion,
              'accepted': true,
              'language': _ref.read(i18nProvider).language,
              'channel': 'native',
            },
            options: Options(headers: {'Authorization': 'Bearer $token'}),
          );
    }
    Map<String, dynamic>? prefs;
    try {
      prefs =
          (await _ref
                  .read(apiDioProvider)
                  .get<Map<String, dynamic>>(
                    '/api/auth/me/preferences',
                    options: Options(
                      headers: {'Authorization': 'Bearer $token'},
                    ),
                  ))
              .data;
    } catch (_) {
      // Best-effort, same as web: an unreadable profile lands on the chat
      // home rather than in the guided flow (only a readable `offered`
      // record opens it).
    }
    _ref.read(firstVideoEntryProvider.notifier).state = prefs == null
        ? FirstVideoEntry.settled
        : firstVideoEntryFromPreferences(prefs);
    _ref
        .read(authProvider.notifier)
        .setAuth(
          token,
          user,
          mobileSessionId: asString(tokenResponse['mobile_session_id']),
        );
    if (prefs == null) return;
    try {
      _ref.read(appearanceProvider.notifier).hydrateFromServer(prefs);
    } catch (_) {
      // Best-effort, same as web.
    }
  }
}

final authFlowProvider = Provider<AuthFlow>(AuthFlow.new);
