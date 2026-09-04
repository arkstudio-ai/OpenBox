import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/providers.dart';
import '../../../shared/models/json.dart';

/// The half of the server's Logto settings a phone can act on
/// (web `features/auth/lib/logto.ts` + `useLogtoConfig`).
class LogtoSso {
  const LogtoSso({required this.endpoint, required this.appId});

  final String endpoint;

  /// The *native* application. Web and mobile are separate Logto applications
  /// because a phone cannot keep a client secret; both resolve to the same
  /// Logto identity, so signing in through either lands on one account.
  final String appId;
}

/// Null when the deployment has no Logto, or has no native application
/// registered yet — the account/password form is then the only way in, exactly
/// as on a deployment that never configured SSO.
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
    // An unreachable server is not a reason to hide the password form.
    return null;
  }
});
