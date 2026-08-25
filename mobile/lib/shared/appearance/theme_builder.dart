import 'package:flutter/material.dart';
import 'package:google_fonts/google_fonts.dart';

import 'tokens.dart';
import 'type_scale.dart';

/// Builds the Flutter ThemeData for a bossip theme+brightness.
/// Body font is Figtree (web `--font-body`); CJK falls through to the
/// platform default (PingFang SC on iOS), same as the web font stack.
ThemeData buildBossipTheme(BossipThemeName name, Brightness brightness) {
  final t = BossipTokens.resolve(name, brightness);

  final baseText = GoogleFonts.figtreeTextTheme().apply(
    bodyColor: t.ink,
    displayColor: t.ink,
  );

  final textTheme = baseText.copyWith(
    // Chat prose default is text-lg / relaxed leading (web ChatFlow).
    bodyLarge: baseText.bodyLarge?.copyWith(fontSize: FontSizes.lg, height: 1.78),
    bodyMedium: baseText.bodyMedium?.copyWith(fontSize: FontSizes.base, height: 1.65),
    bodySmall: baseText.bodySmall?.copyWith(fontSize: FontSizes.sm, height: 1.55),
    titleLarge: baseText.titleLarge?.copyWith(
      fontSize: FontSizes.xl2,
      height: 1.35,
      fontWeight: FontWeight.w500,
      letterSpacing: -0.2,
    ),
    titleMedium: baseText.titleMedium?.copyWith(
      fontSize: FontSizes.xl,
      height: 1.4,
      fontWeight: FontWeight.w500,
    ),
    titleSmall: baseText.titleSmall?.copyWith(
      fontSize: FontSizes.lg,
      height: 1.5,
      fontWeight: FontWeight.w500,
    ),
    labelLarge: baseText.labelLarge?.copyWith(fontSize: FontSizes.sm, height: 1.2),
    labelMedium: baseText.labelMedium?.copyWith(fontSize: FontSizes.xs, height: 1.2),
  );

  final scheme = ColorScheme(
    brightness: brightness,
    primary: t.ink,
    onPrimary: t.bg,
    secondary: t.accent,
    onSecondary: Colors.white,
    error: t.danger,
    onError: Colors.white,
    surface: t.card,
    onSurface: t.ink,
    outline: t.hair,
    surfaceContainerHighest: t.surface,
    onSurfaceVariant: t.n700,
  );

  return ThemeData(
    useMaterial3: true,
    brightness: brightness,
    colorScheme: scheme,
    scaffoldBackgroundColor: t.bg,
    canvasColor: t.bg,
    cardColor: t.card,
    dividerColor: t.hair,
    splashFactory: InkSparkle.splashFactory,
    textTheme: textTheme,
    extensions: [t],
    appBarTheme: AppBarTheme(
      backgroundColor: t.bg,
      foregroundColor: t.ink,
      elevation: 0,
      scrolledUnderElevation: 0,
      centerTitle: false,
    ),
    drawerTheme: DrawerThemeData(
      backgroundColor: t.rail,
      surfaceTintColor: Colors.transparent,
    ),
    bottomSheetTheme: BottomSheetThemeData(
      backgroundColor: t.card,
      surfaceTintColor: Colors.transparent,
      shape: const RoundedRectangleBorder(
        borderRadius: BorderRadius.vertical(top: Radius.circular(Radii.xl2)),
      ),
    ),
    dialogTheme: DialogThemeData(
      backgroundColor: t.card,
      surfaceTintColor: Colors.transparent,
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(Radii.xl)),
    ),
    textSelectionTheme: TextSelectionThemeData(
      cursorColor: t.ink,
      selectionColor: t.accent.withValues(alpha: 0.30),
      selectionHandleColor: t.accent,
    ),
    progressIndicatorTheme: ProgressIndicatorThemeData(color: t.a700),
    scrollbarTheme: ScrollbarThemeData(
      thumbColor: WidgetStatePropertyAll(t.n400),
      radius: const Radius.circular(Radii.full),
      thickness: const WidgetStatePropertyAll(4),
    ),
  );
}
