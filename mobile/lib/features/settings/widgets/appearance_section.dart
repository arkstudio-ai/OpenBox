import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/appearance_store.dart';
import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';

/// Appearance settings (web `AppearancePage`): language (2-col), theme
/// (2-col cards with temp pill + swatches), color mode (3-col), font size
/// (4-col "Aa" previews).
class AppearanceSection extends ConsumerWidget {
  const AppearanceSection({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final appearance = ref.watch(appearanceProvider);
    final controller = ref.read(appearanceProvider.notifier);

    return ListView(
      padding: const EdgeInsets.all(16),
      children: [
        _sectionLabel(t, i18n.t('settings:appearance.language')),
        Row(
          children: [
            Expanded(
              child: _card(
                t,
                selected: i18n.language == 'zh-CN',
                onTap: () => controller.setLanguage('zh-CN'),
                child: _langLabel(t, i18n, 'settings:appearance.langZh',
                    i18n.language == 'zh-CN'),
              ),
            ),
            const SizedBox(width: 10),
            Expanded(
              child: _card(
                t,
                selected: i18n.language == 'en-US',
                onTap: () => controller.setLanguage('en-US'),
                child: _langLabel(t, i18n, 'settings:appearance.langEn',
                    i18n.language == 'en-US'),
              ),
            ),
          ],
        ),
        const SizedBox(height: 24),
        _sectionLabel(t, i18n.t('settings:appearance.theme')),
        GridView.count(
          crossAxisCount: 2,
          shrinkWrap: true,
          physics: const NeverScrollableScrollPhysics(),
          mainAxisSpacing: 10,
          crossAxisSpacing: 10,
          childAspectRatio: 2.1,
          children: [
            for (final theme in BossipThemeName.values)
              _themeCard(t, i18n, theme, appearance.theme == theme,
                  () => controller.setTheme(theme)),
          ],
        ),
        const SizedBox(height: 24),
        _sectionLabel(t, i18n.t('settings:appearance.colorMode')),
        Row(
          children: [
            for (final (mode, icon, key) in [
              (ColorMode.light, Icons.wb_sunny_outlined, 'light'),
              (ColorMode.system, Icons.desktop_windows_outlined, 'system'),
              (ColorMode.dark, Icons.dark_mode_outlined, 'dark'),
            ]) ...[
              Expanded(
                child: _card(
                  t,
                  selected: appearance.mode == mode,
                  onTap: () => controller.setMode(mode),
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      Icon(icon, size: 18, color: t.n700),
                      const SizedBox(height: 6),
                      Text(
                        i18n.t('settings:appearance.$key'),
                        style:
                            TextStyle(fontSize: FontSizes.sm, color: t.ink),
                      ),
                    ],
                  ),
                ),
              ),
              if (mode != ColorMode.dark) const SizedBox(width: 10),
            ],
          ],
        ),
        const SizedBox(height: 24),
        _sectionLabel(t, i18n.t('settings:appearance.fontSize')),
        Row(
          children: [
            for (final size in UiFontSize.values) ...[
              Expanded(
                child: _card(
                  t,
                  selected: appearance.fontSize == size,
                  selectedBorder: t.accent,
                  onTap: () => controller.setFontSize(size),
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      // Preview px per web AppearancePage {12.5,14,15.5,17.5}.
                      Text(
                        'Aa',
                        style: TextStyle(
                          fontSize: switch (size) {
                            UiFontSize.sm => 12.5,
                            UiFontSize.base => 14.0,
                            UiFontSize.md => 15.5,
                            UiFontSize.lg => 17.5,
                          },
                          color: t.ink,
                          fontWeight: FontWeight.w500,
                        ),
                      ),
                      const SizedBox(height: 4),
                      Text(
                        i18n.t('settings:appearance.fs.${size.wire}'),
                        style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
                      ),
                    ],
                  ),
                ),
              ),
              if (size != UiFontSize.lg) const SizedBox(width: 8),
            ],
          ],
        ),
      ],
    );
  }

  Widget _langLabel(
      BossipTokens t, I18nState i18n, String key, bool selected) {
    return Row(
      mainAxisAlignment: MainAxisAlignment.center,
      children: [
        Text(i18n.t(key),
            style: TextStyle(fontSize: FontSizes.base, color: t.ink)),
        if (selected) ...[
          const SizedBox(width: 6),
          Text(
            i18n.t('settings:appearance.current'),
            style: TextStyle(fontSize: FontSizes.xs, color: t.n500),
          ),
        ],
      ],
    );
  }

  Widget _sectionLabel(BossipTokens t, String text) => Padding(
        padding: const EdgeInsets.only(bottom: 10),
        child: Text(
          text,
          style: TextStyle(
            fontSize: FontSizes.sm,
            fontWeight: FontWeight.w600,
            color: t.n700,
          ),
        ),
      );

  Widget _card(
    BossipTokens t, {
    required bool selected,
    required VoidCallback onTap,
    required Widget child,
    Color? selectedBorder,
  }) {
    return Material(
      color: selected ? t.hairSoft : t.card,
      borderRadius: BorderRadius.circular(Radii.lg),
      child: InkWell(
        borderRadius: BorderRadius.circular(Radii.lg),
        onTap: onTap,
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 14),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(Radii.lg),
            border: Border.all(
              color: selected ? (selectedBorder ?? t.ink) : t.hair,
              width: selected ? 1.4 : 1,
            ),
          ),
          child: Center(child: child),
        ),
      ),
    );
  }

  Widget _themeCard(BossipTokens t, I18nState i18n, BossipThemeName theme,
      bool selected, VoidCallback onTap) {
    final (pill1, pill2) = themeSwatches[theme]!;
    final name = theme == BossipThemeName.default_
        ? i18n.t('settings:appearance.themeDefault')
        : theme.wire;
    return _card(
      t,
      selected: selected,
      onTap: onTap,
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              for (final color in [pill1, pill2, t.n300, t.hair])
                Container(
                  width: 16,
                  height: 8,
                  margin: const EdgeInsets.only(right: 4),
                  decoration: BoxDecoration(
                    color: color,
                    borderRadius: BorderRadius.circular(Radii.full),
                  ),
                ),
            ],
          ),
          const SizedBox(height: 8),
          Text(
            name,
            style: TextStyle(
              fontSize: FontSizes.sm,
              color: t.ink,
              fontWeight: FontWeight.w500,
            ),
          ),
        ],
      ),
    );
  }
}
