import 'package:cookie_jar/cookie_jar.dart';
import 'package:flutter/widgets.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:path_provider/path_provider.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'app/app.dart';
import 'shared/api/auth_store.dart';
import 'shared/api/providers.dart';
import 'shared/i18n/i18n.dart';

/// Boot sequence mirrors web `bootstrap/main.tsx`: load persistence + i18n,
/// then silent refresh BEFORE first paint, then mount the app.
Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();

  final prefs = await SharedPreferences.getInstance();
  final supportDir = await getApplicationSupportDirectory();
  final cookieJar = PersistCookieJar(
    storage: FileStorage('${supportDir.path}/cookies'),
  );
  final i18nBundle = await I18nBundle.load();

  final container = ProviderContainer(
    overrides: [
      prefsProvider.overrideWithValue(prefs),
      cookieJarProvider.overrideWithValue(cookieJar),
      i18nProvider.overrideWith(() => I18nController(i18nBundle, prefs)),
    ],
  );

  await container.read(authProvider.notifier).bootstrap();

  runApp(
    UncontrolledProviderScope(
      container: container,
      child: const BossipApp(),
    ),
  );
}
