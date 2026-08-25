import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../shared/appearance/tokens.dart';
import '../../shared/appearance/type_scale.dart';
import '../../shared/i18n/i18n.dart';
import 'widgets/account_section.dart';
import 'widgets/appearance_section.dart';
import 'widgets/models_section.dart';

/// Settings (web `SettingsRoute`), mobile: segmented tabs
/// 账号 / 外观 / 模型. Usage/tools/browser pages are desktop-scope.
class SettingsScreen extends ConsumerStatefulWidget {
  const SettingsScreen({super.key, this.initialTab = 'appearance'});

  final String initialTab;

  @override
  ConsumerState<SettingsScreen> createState() => _SettingsScreenState();
}

class _SettingsScreenState extends ConsumerState<SettingsScreen> {
  static const _tabs = ['account', 'appearance', 'models'];

  late String _tab =
      _tabs.contains(widget.initialTab) ? widget.initialTab : 'appearance';

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return Scaffold(
      backgroundColor: t.bg,
      appBar: AppBar(
        title: Text(
          i18n.t('settings:title'),
          style: TextStyle(
            fontSize: FontSizes.lg,
            fontWeight: FontWeight.w500,
            color: t.ink,
          ),
        ),
        bottom: PreferredSize(
          preferredSize: const Size.fromHeight(46),
          child: Padding(
            padding: const EdgeInsets.fromLTRB(14, 0, 14, 10),
            child: Row(
              children: [
                for (final tab in _tabs)
                  Padding(
                    padding: const EdgeInsets.only(right: 8),
                    child: ChoiceChip(
                      label: Text(
                        i18n.t('settings:nav.$tab'),
                        style: const TextStyle(fontSize: FontSizes.sm),
                      ),
                      selected: _tab == tab,
                      showCheckmark: false,
                      selectedColor: t.a200,
                      backgroundColor: t.bg,
                      labelStyle: TextStyle(color: t.ink),
                      side: BorderSide(color: _tab == tab ? t.a700 : t.hair),
                      onSelected: (_) => setState(() => _tab = tab),
                    ),
                  ),
              ],
            ),
          ),
        ),
      ),
      body: switch (_tab) {
        'account' => const AccountSection(),
        'models' => const ModelsSection(),
        _ => const AppearanceSection(),
      },
    );
  }
}
