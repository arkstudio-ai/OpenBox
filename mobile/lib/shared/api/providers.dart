import 'package:cookie_jar/cookie_jar.dart';
import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'auth_session.dart';
import 'http_client.dart';

/// Platform/transport singletons. `prefs` and `cookieJar` are created
/// asynchronously and overridden in main() before runApp.
final prefsProvider = Provider<SharedPreferences>(
  (ref) => throw UnimplementedError('overridden in main()'),
);

final cookieJarProvider = Provider<CookieJar>(
  (ref) => throw UnimplementedError('overridden in main()'),
);

final authSessionProvider = Provider<AuthSession>((ref) => AuthSession());

final apiDioProvider = Provider<Dio>(
  (ref) => buildApiDio(
    auth: ref.watch(authSessionProvider),
    cookieJar: ref.watch(cookieJarProvider),
  ),
);

final refreshDioProvider = Provider<Dio>(
  (ref) => buildRefreshDio(cookieJar: ref.watch(cookieJarProvider)),
);
