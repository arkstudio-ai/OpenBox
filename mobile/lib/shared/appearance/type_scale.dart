import 'package:flutter/widgets.dart';

/// Type scale — 1:1 with frontend-v2 `tokens.css` @theme text sizes
/// (px at root=16). Line heights are the CSS unitless values.
abstract final class FontSizes {
  static const double xs2 = 10.56;
  static const double xs = 11.52;
  static const double sm = 12.48;
  static const double md = 13.52;
  static const double base = 14.4;
  static const double lg = 15.52;
  static const double xl = 16.96;
  static const double xl2 = 19.04;
  static const double xl3 = 24.8;
  static const double xl4 = 28.0;
  static const double hero = 34.0;
}

/// Text style helper carrying the paired CSS line-height.
TextStyle ts(double size, double lineHeight, {FontWeight? weight, Color? color}) =>
    TextStyle(fontSize: size, height: lineHeight, fontWeight: weight, color: color);

/// Border radii — `tokens.css` @theme radius overrides.
abstract final class Radii {
  static const double sm = 6;
  static const double md = 10;
  static const double lg = 14;
  static const double xl = 18;
  static const double xl2 = 22;
  static const double xl3 = 24; // composer shell (Tailwind rounded-3xl)
  static const double full = 999;
}

/// The 4 UI font-size levels (`html[data-fs]` root scaling percentages).
enum UiFontSize { sm, base, md, lg }

extension UiFontSizeX on UiFontSize {
  String get wire => name;

  double get scale => switch (this) {
        UiFontSize.sm => 0.92,
        UiFontSize.base => 1.0,
        UiFontSize.md => 1.09,
        UiFontSize.lg => 1.20,
      };

  static UiFontSize parse(String? value) => switch (value) {
        'sm' => UiFontSize.sm,
        'md' => UiFontSize.md,
        'lg' => UiFontSize.lg,
        _ => UiFontSize.base,
      };
}

/// Color mode preference (`light` | `system` | `dark`).
enum ColorMode { light, system, dark }

extension ColorModeX on ColorMode {
  String get wire => name;

  static ColorMode parse(String? value) => switch (value) {
        'light' => ColorMode.light,
        'dark' => ColorMode.dark,
        _ => ColorMode.system,
      };
}
