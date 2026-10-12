import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/app_config.dart';
import '../../../shared/models/json.dart';
import '../api/settings_api.dart';

/// Model defaults (web `ModelsPage`): default model + default agent pickers,
/// persisted via `PUT /api/auth/me/preferences`.
class ModelsSection extends ConsumerWidget {
  const ModelsSection({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final prefs = ref.watch(preferencesProvider).valueOrNull ?? const {};
    final config = ref.watch(settingsConfigProvider).valueOrNull;
    final agents = ref.watch(settingsAgentsProvider).valueOrNull ?? const [];

    final defaultModel =
        asString(prefs['default_model']) ?? config?.defaultModel ?? '';
    final defaultAgent =
        asString(prefs['default_agent']) ?? config?.defaultAgent ?? 'build';
    final imageProvider =
        asString(prefs['image_generation_provider']) ?? 'default';
    final imageOptions =
        config?.imageProviders ?? const <ImageProviderOption>[];
    final imageLabel =
        imageOptions
            .where((option) => option.id == imageProvider)
            .firstOrNull
            ?.name ??
        imageProvider;

    return ListView(
      padding: const EdgeInsets.all(16),
      children: [
        _pickerRow(
          context,
          ref,
          t,
          title: i18n.t('settings:models.imageProvider'),
          hint: i18n.t('settings:models.imageProviderHint'),
          value: imageLabel,
          options: [
            for (final option in imageOptions)
              if (option.available) (option.id, option.name),
          ],
          selected: imageProvider,
          onPick: (id) =>
              _update(context, ref, {'image_generation_provider': id}),
        ),
        const SizedBox(height: 10),
        _pickerRow(
          context,
          ref,
          t,
          title: i18n.t('settings:models.defaultModel'),
          hint: i18n.t('settings:models.defaultModelHint'),
          value: config?.byId(defaultModel)?.name ?? defaultModel,
          options: [
            for (final m in config?.models ?? const <ModelInfo>[])
              (m.id, m.name),
          ],
          selected: defaultModel,
          onPick: (id) => _update(context, ref, {'default_model': id}),
        ),
        const SizedBox(height: 10),
        _pickerRow(
          context,
          ref,
          t,
          title: i18n.t('settings:models.defaultAgent'),
          hint: i18n.t('settings:models.defaultAgentHint'),
          value: defaultAgent,
          options: [for (final a in agents) (a.name, a.name)],
          selected: defaultAgent,
          onPick: (name) => _update(context, ref, {'default_agent': name}),
        ),
      ],
    );
  }

  Future<void> _update(
    BuildContext context,
    WidgetRef ref,
    Map<String, dynamic> patch,
  ) async {
    final message = ref.read(i18nProvider).t('settings:models.saveFailed');
    try {
      await ref.read(settingsApiProvider).updatePreferences(patch);
      ref.invalidate(preferencesProvider);
    } catch (_) {
      if (context.mounted) {
        ScaffoldMessenger.of(
          context,
        ).showSnackBar(SnackBar(content: Text(message)));
      }
    }
  }

  Widget _pickerRow(
    BuildContext context,
    WidgetRef ref,
    BossipTokens t, {
    required String title,
    required String hint,
    required String value,
    required List<(String, String)> options,
    required String selected,
    required void Function(String) onPick,
  }) {
    return Material(
      color: t.card,
      borderRadius: BorderRadius.circular(Radii.xl),
      child: InkWell(
        borderRadius: BorderRadius.circular(Radii.xl),
        onTap: () => _showOptions(context, t, options, selected, onPick),
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
          decoration: BoxDecoration(
            border: Border.all(color: t.hair),
            borderRadius: BorderRadius.circular(Radii.xl),
          ),
          child: Row(
            children: [
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      title,
                      style: TextStyle(
                        fontSize: FontSizes.base,
                        color: t.ink,
                        fontWeight: FontWeight.w500,
                      ),
                    ),
                    const SizedBox(height: 2),
                    Text(
                      hint,
                      style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
                    ),
                  ],
                ),
              ),
              Text(
                value,
                style: TextStyle(fontSize: FontSizes.sm, color: t.n700),
              ),
              const SizedBox(width: 4),
              Icon(Icons.expand_more, size: 16, color: t.n500),
            ],
          ),
        ),
      ),
    );
  }

  void _showOptions(
    BuildContext context,
    BossipTokens t,
    List<(String, String)> options,
    String selected,
    void Function(String) onPick,
  ) {
    showModalBottomSheet<void>(
      context: context,
      builder: (sheetContext) => SafeArea(
        child: ListView(
          shrinkWrap: true,
          children: [
            for (final (id, label) in options)
              ListTile(
                dense: true,
                title: Text(
                  label,
                  style: TextStyle(fontSize: FontSizes.base, color: t.ink),
                ),
                trailing: id == selected
                    ? Icon(Icons.check, size: 18, color: t.a700)
                    : null,
                onTap: () {
                  Navigator.pop(sheetContext);
                  onPick(id);
                },
              ),
          ],
        ),
      ),
    );
  }
}
