import 'package:flutter/material.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/i18n/i18n.dart';

class ProductionFilmSetting extends StatelessWidget {
  const ProductionFilmSetting({
    super.key,
    required this.enabled,
    required this.i18n,
    required this.onChanged,
  });
  final bool enabled;
  final I18nState i18n;
  final ValueChanged<bool>? onChanged;

  @override
  Widget build(BuildContext context) {
    String t(String key) => i18n.t('light:production.renderOptions.$key');
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Text(t('title'), style: const TextStyle(fontWeight: FontWeight.w700)),
        Card(
          child: SwitchListTile.adaptive(
            key: const Key('production-perfect-film'),
            title: Text(
              t('perfect'),
              style: const TextStyle(fontWeight: FontWeight.w700),
            ),
            subtitle: Text(t(enabled ? 'on' : 'off')),
            value: enabled,
            onChanged: onChanged == null
                ? null
                : (next) async {
                    if (!next) {
                      onChanged!(false);
                      return;
                    }
                    final accepted = await showDialog<bool>(
                      context: context,
                      builder: (dialog) => AlertDialog(
                        title: Text(t('confirmTitle')),
                        content: Column(
                          mainAxisSize: MainAxisSize.min,
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Text(t('confirmBody')),
                            const SizedBox(height: 12),
                            Text(
                              t('impact'),
                              style: const TextStyle(
                                fontWeight: FontWeight.w600,
                              ),
                            ),
                          ],
                        ),
                        actions: [
                          TextButton(
                            onPressed: () => Navigator.pop(dialog, false),
                            child: Text(t('cancelEnable')),
                          ),
                          TextButton(
                            key: const Key('production-perfect-confirm'),
                            onPressed: () => Navigator.pop(dialog, true),
                            child: Text(t('enable')),
                          ),
                        ],
                      ),
                    );
                    if (context.mounted && accepted == true) onChanged!(true);
                  },
          ),
        ),
        const SizedBox(height: 4),
      ],
    );
  }
}

class ProductionLanguageButton extends StatelessWidget {
  const ProductionLanguageButton({
    super.key,
    required this.language,
    required this.i18n,
    required this.onChanged,
  });
  final String language;
  final I18nState i18n;
  final ValueChanged<String>? onChanged;
  String t(String key) => i18n.t('light:production.renderOptions.$key');

  @override
  Widget build(BuildContext context) {
    final label =
        "${t('language')} · ${language.isEmpty ? t('auto') : language}";
    return OutlinedButton(
      key: const Key('production-output-language'),
      onPressed: onChanged == null
          ? null
          : () async {
              final value = await showDialog<String>(
                context: context,
                builder: (ctx) =>
                    _LanguageDialog(language: language, i18n: i18n),
              );
              if (context.mounted && value != null && value != language) {
                onChanged!(value);
              }
            },
      style: OutlinedButton.styleFrom(
        minimumSize: const Size(0, 32),
        padding: const EdgeInsets.symmetric(horizontal: 10),
        side: BorderSide(color: context.tokens.hair),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Flexible(
            child: Text(label, maxLines: 1, overflow: TextOverflow.ellipsis),
          ),
          const Icon(Icons.expand_more, size: 15),
        ],
      ),
    );
  }
}

class _LanguageDialog extends StatefulWidget {
  const _LanguageDialog({required this.language, required this.i18n});
  final String language;
  final I18nState i18n;
  @override
  State<_LanguageDialog> createState() => _LanguageDialogState();
}

class _LanguageDialogState extends State<_LanguageDialog> {
  late bool custom = widget.language.isNotEmpty;
  late final controller = TextEditingController(text: widget.language);
  String t(String key) => widget.i18n.t('light:production.renderOptions.$key');
  @override
  void dispose() {
    controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final locale = widget.i18n.locale;
    final name = switch (locale.languageCode) {
      'zh' => '简体中文',
      'vi' => 'Tiếng Việt',
      'en' => 'English',
      _ => locale.toLanguageTag(),
    };
    final canSave = !custom || controller.text.trim().isNotEmpty;
    void save() => Navigator.pop(context, custom ? controller.text.trim() : '');
    return AlertDialog(
      title: Text(t('language')),
      scrollable: true,
      content: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Text(t('scope')),
          const SizedBox(height: 16),
          SegmentedButton<bool>(
            showSelectedIcon: false,
            segments: [
              ButtonSegment(value: false, label: Text(t('follow'))),
              ButtonSegment(
                value: true,
                label: Text(
                  t('custom'),
                  key: const Key('production-language-manual'),
                ),
              ),
            ],
            selected: {custom},
            onSelectionChanged: (v) => setState(() => custom = v.single),
          ),
          const SizedBox(height: 16),
          if (custom)
            TextField(
              key: const Key('production-language-input'),
              autofocus: true,
              controller: controller,
              maxLength: 80,
              decoration: InputDecoration(
                labelText: t('field'),
                hintText: t('hint'),
                counterText: '',
              ),
              textInputAction: TextInputAction.done,
              onSubmitted: (_) {
                if (canSave) save();
              },
              onChanged: (_) => setState(() {}),
            )
          else
            Text("${t('current')} · $name"),
        ],
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.pop(context),
          child: Text(widget.i18n.t('common:action.cancel')),
        ),
        TextButton(
          key: const Key('production-language-save'),
          onPressed: canSave ? save : null,
          child: Text(t('save')),
        ),
      ],
    );
  }
}
