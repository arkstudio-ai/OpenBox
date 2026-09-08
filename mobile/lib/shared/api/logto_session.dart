import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:logto_dart_sdk/logto_dart_sdk.dart';

import '../config/env.dart';
import '../models/json.dart';
import 'providers.dart';

/// Public Logto application settings a native client can safely receive.
class LogtoSso {
  const LogtoSso({required this.endpoint, required this.appId});

  final String endpoint;
  final String appId;
}

/// Null when Logto or its separate Native application is not configured.
final logtoSsoProvider = FutureProvider<LogtoSso?>((ref) async {
  try {
    final resp = await ref
        .read(apiDioProvider)
        .get<Map<String, dynamic>>('/api/auth/logto/config');
    final data = asMap(resp.data);
    if (asBool(data['enabled']) != true) return null;
    final endpoint = asString(data['endpoint']) ?? '';
    final appId = asString(data['native_app_id']) ?? '';
    if (endpoint.isEmpty || appId.isEmpty) return null;
    return LogtoSso(endpoint: endpoint, appId: appId);
  } catch (_) {
    return null;
  }
});

/// Injectable boundary around Logto's native SDK.
abstract interface class LogtoSession {
  Future<String> signIn(LogtoSso config, {required bool register});

  /// Ends the centralized SSO session and always clears SDK tokens on-device.
  Future<void> signOut(LogtoSso? config);
}

class SdkLogtoSession implements LogtoSession {
  LogtoClient? _client;
  String? _clientKey;

  LogtoClient _clientFor(LogtoSso config) {
    final key = '${config.endpoint}\u0000${config.appId}';
    if (_client == null || _clientKey != key) {
      _client = LogtoClient(
        config: LogtoConfig(endpoint: config.endpoint, appId: config.appId),
      );
      _clientKey = key;
    }
    return _client!;
  }

  @override
  Future<String> signIn(LogtoSso config, {required bool register}) async {
    final client = _clientFor(config);
    await client.signIn(
      Env.ssoRedirectUri,
      firstScreen: register ? FirstScreen.register : FirstScreen.signIn,
      // flutter_web_auth_2 5.x gives this flow an ephemeral browser session,
      // while signOut below explicitly ends the centralized session. Forcing
      // `login` against the production Logto sends a freshly authenticated
      // interaction through /oidc/session/end/confirm and strands Android on
      // its blank "Submitting Callback" page. Keep `consent` because the SDK
      // requests offline_access for refresh tokens.
      extraParams: const {'prompt': 'consent'},
    );
    final idToken = await client.idToken;
    if (idToken == null) throw StateError('Logto returned no id token');
    return idToken;
  }

  @override
  Future<void> signOut(LogtoSso? config) async {
    try {
      if (config == null) return;
      final client = _clientFor(config);
      if (await client.isAuthenticated) {
        await client.signOut(Env.ssoPostLogoutRedirectUri);
      }
    } finally {
      // The SDK normally clears these before opening Logto's end-session URL.
      // Keep the same guarantee when discovery/revocation/browser setup fails,
      // or when the public server config is temporarily unavailable.
      final storage = SecureStorageStrategy();
      await Future.wait<void>([
        storage.delete(key: 'logto_access_token'),
        storage.delete(key: 'logto_refresh_token'),
        storage.delete(key: 'logto_id_token'),
      ]);
    }
  }
}

final logtoSessionProvider = Provider<LogtoSession>((ref) => SdkLogtoSession());
