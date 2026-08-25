import 'package:flutter/material.dart';

/// The 8 bossip themes. Mirrors frontend-v2 `src/shared/appearance/store.ts`
/// (`THEMES`) and the `--t-*` palettes in `src/styles/tokens.css`.
enum BossipThemeName { default_, azure, cobalt, graphite, lagoon, ink, ochre, sepia }

extension BossipThemeNameWire on BossipThemeName {
  /// Wire/persistence value (matches web `data-theme` / prefs `theme`).
  String get wire => this == BossipThemeName.default_ ? 'default' : name;

  static BossipThemeName parse(String? value) {
    if (value == null) return BossipThemeName.default_;
    return BossipThemeName.values.firstWhere(
      (t) => t.wire == value,
      orElse: () => BossipThemeName.default_,
    );
  }
}

/// Swatch pill colors for the theme picker cards (web `THEME_META`).
const Map<BossipThemeName, (Color, Color)> themeSwatches = {
  BossipThemeName.default_: (Color(0xFFC67139), Color(0xFF2C2B28)),
  BossipThemeName.azure: (Color(0xFF3BA0FF), Color(0xFF1C1F22)),
  BossipThemeName.cobalt: (Color(0xFF1A48D0), Color(0xFF191C22)),
  BossipThemeName.graphite: (Color(0xFF111111), Color(0xFF3D3D3D)),
  BossipThemeName.lagoon: (Color(0xFF12B39A), Color(0xFF17332E)),
  BossipThemeName.ink: (Color(0xFF101215), Color(0xFF2C3138)),
  BossipThemeName.ochre: (Color(0xFFF07C0A), Color(0xFF28221A)),
  BossipThemeName.sepia: (Color(0xFF8A5F52), Color(0xFF2C2320)),
};

/// Runtime design tokens — a 1:1 port of the `--t-*` CSS variables in
/// frontend-v2 `src/styles/tokens.css`. Widgets read colors ONLY from here
/// (via `context.tokens`), never hardcoded, matching the web rule that
/// components only use token-mapped utilities.
@immutable
class BossipTokens extends ThemeExtension<BossipTokens> {
  const BossipTokens({
    required this.bg,
    required this.rail,
    required this.card,
    required this.hair,
    required this.hairSoft,
    required this.ink,
    required this.surface,
    required this.n100,
    required this.n200,
    required this.n300,
    required this.n400,
    required this.n500,
    required this.n600,
    required this.n700,
    required this.n800,
    required this.n900,
    required this.accent,
    required this.a100,
    required this.a200,
    required this.a300,
    required this.a700,
    required this.a800,
    required this.sage,
    required this.s100,
    required this.s300,
    required this.s400,
    required this.s600,
    required this.s700,
    required this.s800,
    required this.danger,
    required this.dangerInk,
    required this.dangerSoft,
    required this.diffAdd,
    required this.diffDel,
    required this.term,
    required this.termInk,
    required this.shine,
  });

  final Color bg; // --t-bg      page background
  final Color rail; // --t-rail    sidebar background
  final Color card; // --t-card    card / composer background
  final Color hair; // --t-hair    hairline border
  final Color hairSoft; // --t-hair-soft
  final Color ink; // --t-ink     primary foreground
  final Color surface; // --t-surface
  final Color n100;
  final Color n200;
  final Color n300;
  final Color n400;
  final Color n500;
  final Color n600;
  final Color n700;
  final Color n800;
  final Color n900;
  final Color accent; // --t-accent  vivid brand hue
  final Color a100;
  final Color a200;
  final Color a300;
  final Color a700; // toned accent (== accent hue in colored themes)
  final Color a800;
  final Color sage; // success family
  final Color s100;
  final Color s300;
  final Color s400;
  final Color s600;
  final Color s700;
  final Color s800;
  final Color danger;
  final Color dangerInk;
  final Color dangerSoft;
  final Color diffAdd;
  final Color diffDel;
  final Color term; // terminal background
  final Color termInk; // terminal foreground
  final Color shine; // wordmark shine

  /// Resolve the palette for a theme + brightness, reproducing the CSS
  /// cascade: `:root` (default light) → `[data-theme]` → `[data-mode=dark]`
  /// → `[data-mode=dark][data-theme]`.
  static BossipTokens resolve(BossipThemeName theme, Brightness brightness) {
    if (brightness == Brightness.light) {
      return _lightOverrides[theme] ?? _defaultLight;
    }
    final (a700, accent) = _darkAccents[theme]!;
    return _darkGround.copyWith(a700: a700, accent: accent);
  }

  @override
  BossipTokens copyWith({
    Color? bg,
    Color? rail,
    Color? card,
    Color? hair,
    Color? hairSoft,
    Color? ink,
    Color? surface,
    Color? n100,
    Color? n200,
    Color? n300,
    Color? n400,
    Color? n500,
    Color? n600,
    Color? n700,
    Color? n800,
    Color? n900,
    Color? accent,
    Color? a100,
    Color? a200,
    Color? a300,
    Color? a700,
    Color? a800,
    Color? sage,
    Color? s100,
    Color? s300,
    Color? s400,
    Color? s600,
    Color? s700,
    Color? s800,
    Color? danger,
    Color? dangerInk,
    Color? dangerSoft,
    Color? diffAdd,
    Color? diffDel,
    Color? term,
    Color? termInk,
    Color? shine,
  }) {
    return BossipTokens(
      bg: bg ?? this.bg,
      rail: rail ?? this.rail,
      card: card ?? this.card,
      hair: hair ?? this.hair,
      hairSoft: hairSoft ?? this.hairSoft,
      ink: ink ?? this.ink,
      surface: surface ?? this.surface,
      n100: n100 ?? this.n100,
      n200: n200 ?? this.n200,
      n300: n300 ?? this.n300,
      n400: n400 ?? this.n400,
      n500: n500 ?? this.n500,
      n600: n600 ?? this.n600,
      n700: n700 ?? this.n700,
      n800: n800 ?? this.n800,
      n900: n900 ?? this.n900,
      accent: accent ?? this.accent,
      a100: a100 ?? this.a100,
      a200: a200 ?? this.a200,
      a300: a300 ?? this.a300,
      a700: a700 ?? this.a700,
      a800: a800 ?? this.a800,
      sage: sage ?? this.sage,
      s100: s100 ?? this.s100,
      s300: s300 ?? this.s300,
      s400: s400 ?? this.s400,
      s600: s600 ?? this.s600,
      s700: s700 ?? this.s700,
      s800: s800 ?? this.s800,
      danger: danger ?? this.danger,
      dangerInk: dangerInk ?? this.dangerInk,
      dangerSoft: dangerSoft ?? this.dangerSoft,
      diffAdd: diffAdd ?? this.diffAdd,
      diffDel: diffDel ?? this.diffDel,
      term: term ?? this.term,
      termInk: termInk ?? this.termInk,
      shine: shine ?? this.shine,
    );
  }

  @override
  BossipTokens lerp(BossipTokens? other, double t) {
    if (other == null) return this;
    Color mix(Color a, Color b) => Color.lerp(a, b, t)!;
    return BossipTokens(
      bg: mix(bg, other.bg),
      rail: mix(rail, other.rail),
      card: mix(card, other.card),
      hair: mix(hair, other.hair),
      hairSoft: mix(hairSoft, other.hairSoft),
      ink: mix(ink, other.ink),
      surface: mix(surface, other.surface),
      n100: mix(n100, other.n100),
      n200: mix(n200, other.n200),
      n300: mix(n300, other.n300),
      n400: mix(n400, other.n400),
      n500: mix(n500, other.n500),
      n600: mix(n600, other.n600),
      n700: mix(n700, other.n700),
      n800: mix(n800, other.n800),
      n900: mix(n900, other.n900),
      accent: mix(accent, other.accent),
      a100: mix(a100, other.a100),
      a200: mix(a200, other.a200),
      a300: mix(a300, other.a300),
      a700: mix(a700, other.a700),
      a800: mix(a800, other.a800),
      sage: mix(sage, other.sage),
      s100: mix(s100, other.s100),
      s300: mix(s300, other.s300),
      s400: mix(s400, other.s400),
      s600: mix(s600, other.s600),
      s700: mix(s700, other.s700),
      s800: mix(s800, other.s800),
      danger: mix(danger, other.danger),
      dangerInk: mix(dangerInk, other.dangerInk),
      dangerSoft: mix(dangerSoft, other.dangerSoft),
      diffAdd: mix(diffAdd, other.diffAdd),
      diffDel: mix(diffDel, other.diffDel),
      term: mix(term, other.term),
      termInk: mix(termInk, other.termInk),
      shine: mix(shine, other.shine),
    );
  }
}

/// `:root` — the full default light palette (tokens.css:11-53).
const BossipTokens _defaultLight = BossipTokens(
  bg: Color(0xFFF8F8F6),
  rail: Color(0xFFF7F7F5),
  card: Color(0xFFFFFFFF),
  hair: Color(0xFFEEECE9),
  hairSoft: Color(0xFFF4F2EC),
  ink: Color(0xFF3E3929),
  surface: Color(0xFFF2F0EA),
  n100: Color(0xFFF8F8F6),
  n200: Color(0xFFF2F0EA),
  n300: Color(0xFFECEAE2),
  n400: Color(0xFFDCDAD3),
  n500: Color(0xFFB7B5AE),
  n600: Color(0xFF8B8A84),
  n700: Color(0xFF75746E),
  n800: Color(0xFF55534D),
  n900: Color(0xFF33322E),
  accent: Color(0xFFB06A3F),
  a100: Color(0xFFF4F2EC),
  a200: Color(0xFFECEAE2),
  a300: Color(0xFFDEDBD1),
  a700: Color(0xFF3E3929),
  a800: Color(0xFF2C2820),
  sage: Color(0xFF7D8A76),
  s100: Color(0xFFEFF4EC),
  s300: Color(0xFFD3DDCE),
  s400: Color(0xFFB3C2AD),
  s600: Color(0xFF5A8560),
  s700: Color(0xFF4C7553),
  s800: Color(0xFF3F6446),
  danger: Color(0xFF9C3A22),
  dangerInk: Color(0xFFA4553F),
  dangerSoft: Color(0xFFFBEEEA),
  diffAdd: Color(0xFFF1F7EE),
  diffDel: Color(0xFFFDF1EE),
  term: Color(0xFF26251F),
  termInk: Color(0xFFE6E3DC),
  shine: Color(0xFFB9B2A1),
);

/// `[data-mode=dark]` — the shared warm-dark ground (tokens.css:93-103).
/// Tokens not listed there inherit the `:root` light values.
final BossipTokens _darkGround = _defaultLight.copyWith(
  bg: const Color(0xFF1B1A18),
  rail: const Color(0xFF1F1E1C),
  card: const Color(0xFF232220),
  hair: const Color(0xFF302E2B),
  hairSoft: const Color(0xFF272624),
  ink: const Color(0xFFECE9E3),
  surface: const Color(0xFF272624),
  n100: const Color(0xFF1B1A18),
  n200: const Color(0xFF272624),
  n300: const Color(0xFF302E2B),
  n400: const Color(0xFF45433F),
  n500: const Color(0xFF6F6D67),
  n600: const Color(0xFF9A988F),
  n700: const Color(0xFFB6B3AB),
  n800: const Color(0xFFD8D5CE),
  n900: const Color(0xFFF0EDE7),
  a100: const Color(0xFF272624),
  a200: const Color(0xFF302E2B),
  a300: const Color(0xFF3D3B37),
  a700: const Color(0xFFC67139),
  dangerSoft: const Color(0xFF3A2A24),
  diffAdd: const Color(0xFF26302A),
  diffDel: const Color(0xFF362824),
  shine: const Color(0xFF6F6D67),
);

/// `[data-theme=…]` light overrides (tokens.css:56-90). Only the listed
/// subset changes; everything else inherits the default light palette.
final Map<BossipThemeName, BossipTokens> _lightOverrides = {
  BossipThemeName.default_: _defaultLight,
  BossipThemeName.azure: _defaultLight.copyWith(
    bg: const Color(0xFFF7F9FB),
    rail: const Color(0xFFF5F8FA),
    hair: const Color(0xFFE9EDF1),
    hairSoft: const Color(0xFFF0F4F8),
    n100: const Color(0xFFF7F9FB),
    n200: const Color(0xFFEEF2F7),
    n300: const Color(0xFFE5EBF2),
    ink: const Color(0xFF23272B),
    surface: const Color(0xFFEEF2F7),
    accent: const Color(0xFF1F8FFF),
    a100: const Color(0xFFF0F4F8),
    a200: const Color(0xFFE5EBF2),
    a700: const Color(0xFF1F8FFF),
  ),
  BossipThemeName.cobalt: _defaultLight.copyWith(
    bg: const Color(0xFFF7F8FB),
    rail: const Color(0xFFF5F7FA),
    hair: const Color(0xFFE8EBF2),
    hairSoft: const Color(0xFFEFF2F8),
    n100: const Color(0xFFF7F8FB),
    n200: const Color(0xFFEDF0F7),
    n300: const Color(0xFFE3E8F2),
    ink: const Color(0xFF20242C),
    surface: const Color(0xFFEDF0F7),
    accent: const Color(0xFF1A48D0),
    a100: const Color(0xFFEFF2F8),
    a200: const Color(0xFFE3E8F2),
    a700: const Color(0xFF1A48D0),
  ),
  BossipThemeName.graphite: _defaultLight.copyWith(
    bg: const Color(0xFFF8F8F8),
    rail: const Color(0xFFF6F6F6),
    hair: const Color(0xFFECECEC),
    hairSoft: const Color(0xFFF2F2F2),
    n100: const Color(0xFFF8F8F8),
    n200: const Color(0xFFF1F1F1),
    n300: const Color(0xFFE8E8E8),
    ink: const Color(0xFF232323),
    surface: const Color(0xFFF1F1F1),
    accent: const Color(0xFF2B2B2B),
    a100: const Color(0xFFF2F2F2),
    a200: const Color(0xFFE8E8E8),
    a700: const Color(0xFF2B2B2B),
  ),
  BossipThemeName.lagoon: _defaultLight.copyWith(
    bg: const Color(0xFFF6FAF9),
    rail: const Color(0xFFF4F9F7),
    hair: const Color(0xFFE6EFEC),
    hairSoft: const Color(0xFFEEF5F3),
    n100: const Color(0xFFF6FAF9),
    n200: const Color(0xFFEBF4F1),
    n300: const Color(0xFFE0EDE9),
    ink: const Color(0xFF1F2B28),
    surface: const Color(0xFFEBF4F1),
    accent: const Color(0xFF0F9D86),
    a100: const Color(0xFFEEF5F3),
    a200: const Color(0xFFE0EDE9),
    a700: const Color(0xFF0F9D86),
  ),
  BossipThemeName.ink: _defaultLight.copyWith(
    bg: const Color(0xFFF5F8FB),
    rail: const Color(0xFFF2F6FA),
    hair: const Color(0xFFE5EBF1),
    hairSoft: const Color(0xFFEDF2F7),
    n100: const Color(0xFFF5F8FB),
    n200: const Color(0xFFEAF0F6),
    n300: const Color(0xFFDFE8F1),
    ink: const Color(0xFF191D22),
    surface: const Color(0xFFEAF0F6),
    accent: const Color(0xFF1A1A1A),
    a100: const Color(0xFFEDF2F7),
    a200: const Color(0xFFDFE8F1),
    a700: const Color(0xFF1A1A1A),
  ),
  BossipThemeName.ochre: _defaultLight.copyWith(
    bg: const Color(0xFFFAF7F2),
    rail: const Color(0xFFF8F5EF),
    hair: const Color(0xFFF0EAE0),
    hairSoft: const Color(0xFFF6F1E8),
    n100: const Color(0xFFFAF7F2),
    n200: const Color(0xFFF4EFE6),
    n300: const Color(0xFFEDE6D9),
    ink: const Color(0xFF3A3227),
    surface: const Color(0xFFF4EFE6),
    accent: const Color(0xFFD9760F),
    a100: const Color(0xFFF6F1E8),
    a200: const Color(0xFFEDE6D9),
    a700: const Color(0xFFD9760F),
  ),
  BossipThemeName.sepia: _defaultLight.copyWith(
    bg: const Color(0xFFFAF7F4),
    rail: const Color(0xFFF8F4F1),
    hair: const Color(0xFFEFE8E3),
    hairSoft: const Color(0xFFF5EFEB),
    n100: const Color(0xFFFAF7F4),
    n200: const Color(0xFFF3EDE8),
    n300: const Color(0xFFE9E0D9),
    ink: const Color(0xFF382D28),
    surface: const Color(0xFFF3EDE8),
    accent: const Color(0xFF7A5348),
    a100: const Color(0xFFF5EFEB),
    a200: const Color(0xFFE9E0D9),
    a700: const Color(0xFF7A5348),
  ),
};

/// `[data-mode=dark][data-theme=…]` accent overrides (tokens.css:104-110):
/// (a700, accent) per theme on top of the shared dark ground.
const Map<BossipThemeName, (Color, Color)> _darkAccents = {
  BossipThemeName.default_: (Color(0xFFC67139), Color(0xFFB06A3F)),
  BossipThemeName.azure: (Color(0xFF3BA0FF), Color(0xFF1F8FFF)),
  BossipThemeName.cobalt: (Color(0xFF5D7EE0), Color(0xFF486BD8)),
  BossipThemeName.graphite: (Color(0xFFC9C9C9), Color(0xFFA8A8A8)),
  BossipThemeName.lagoon: (Color(0xFF12B39A), Color(0xFF0F9D86)),
  BossipThemeName.ink: (Color(0xFFA8B6C6), Color(0xFF8A97A6)),
  BossipThemeName.ochre: (Color(0xFFF07C0A), Color(0xFFD9760F)),
  BossipThemeName.sepia: (Color(0xFFB58A7C), Color(0xFF8A5F52)),
};

extension BossipTokensContext on BuildContext {
  BossipTokens get tokens => Theme.of(this).extension<BossipTokens>()!;
}
