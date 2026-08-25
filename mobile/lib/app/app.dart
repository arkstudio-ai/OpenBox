import 'package:flutter/material.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../shared/appearance/appearance_store.dart';
import '../shared/appearance/theme_builder.dart';
import '../shared/appearance/type_scale.dart';
import '../shared/i18n/i18n.dart';
import '../shared/widgets/toast.dart';
import 'router.dart';

/// Root widget: wires appearance (theme × mode × font-scale) and i18n into
/// MaterialApp.router, and floats the toast host above every screen.
class BossipApp extends ConsumerWidget {
  const BossipApp({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final appearance = ref.watch(appearanceProvider);
    final i18n = ref.watch(i18nProvider);
    final router = ref.watch(routerProvider);

    return MaterialApp.router(
      title: 'bossip',
      debugShowCheckedModeBanner: false,
      routerConfig: router,
      theme: buildBossipTheme(appearance.theme, Brightness.light),
      darkTheme: buildBossipTheme(appearance.theme, Brightness.dark),
      themeMode: switch (appearance.mode) {
        ColorMode.light => ThemeMode.light,
        ColorMode.dark => ThemeMode.dark,
        ColorMode.system => ThemeMode.system,
      },
      locale: i18n.locale,
      supportedLocales: const [Locale('zh', 'CN'), Locale('en', 'US')],
      localizationsDelegates: const [
        GlobalMaterialLocalizations.delegate,
        GlobalWidgetsLocalizations.delegate,
        GlobalCupertinoLocalizations.delegate,
      ],
      builder: (context, child) {
        // Global UI scale (web html[data-fs] root-font percentages).
        final media = MediaQuery.of(context);
        return MediaQuery(
          data: media.copyWith(
            textScaler: TextScaler.linear(appearance.fontSize.scale),
          ),
          child: Stack(
            children: [
              ?child,
              const ToastHost(),
            ],
          ),
        );
      },
    );
  }
}
