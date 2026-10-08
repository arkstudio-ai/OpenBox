import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/assistant_name.dart';
import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/widgets/toast.dart';

/// 个人助理 (web `AssistantPage`): the name the person calls their assistant,
/// used on the web, in this app, in the assistant's replies and on calls.
class AssistantSection extends ConsumerStatefulWidget {
  const AssistantSection({super.key});

  @override
  ConsumerState<AssistantSection> createState() => _AssistantSectionState();
}

class _AssistantSectionState extends ConsumerState<AssistantSection> {
  final _field = TextEditingController();
  // The stored name the field was last filled from; a newer one refills it
  // unless the person is in the middle of typing.
  String? _filledFrom;
  bool _saving = false;

  @override
  void dispose() {
    _field.dispose();
    super.dispose();
  }

  Future<void> _save(String name) async {
    final i18n = ref.read(i18nProvider);
    setState(() => _saving = true);
    try {
      final kept = await ref.read(assistantNameProvider.notifier).rename(name);
      if (!mounted) return;
      _field.text = kept;
      _filledFrom = kept;
      ref
          .read(toastProvider.notifier)
          .success(
            kept.isEmpty
                ? i18n.t('settings:assistant.resetDone')
                : i18n.t('settings:assistant.saved', vars: {'name': kept}),
          );
    } catch (error) {
      if (!mounted) return;
      final invalid =
          error is DioException && error.response?.statusCode == 422;
      ref
          .read(toastProvider.notifier)
          .error(
            i18n.t(
              invalid
                  ? 'settings:assistant.invalid'
                  : 'settings:assistant.saveFailed',
            ),
          );
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final current = ref.watch(assistantNameProvider).valueOrNull ?? '';
    if (_filledFrom != current && _field.text == (_filledFrom ?? '')) {
      _field.text = current;
      _filledFrom = current;
    }
    return ListView(
      padding: const EdgeInsets.all(16),
      children: [
        Text(
          i18n.t('settings:assistant.nameLabel'),
          style: TextStyle(fontSize: FontSizes.sm, color: t.n700),
        ),
        const SizedBox(height: 8),
        TextField(
          key: const ValueKey('assistant-name'),
          controller: _field,
          maxLength: 20,
          onChanged: (_) => setState(() {}),
          onSubmitted: (value) {
            if (value.trim() != current && !_saving) _save(value);
          },
          style: TextStyle(fontSize: FontSizes.base, color: t.ink),
          decoration: InputDecoration(
            hintText: i18n.t('settings:assistant.placeholder'),
            hintStyle: TextStyle(fontSize: FontSizes.sm, color: t.n600),
            counterText: '',
            filled: true,
            fillColor: t.card,
            border: OutlineInputBorder(
              borderRadius: BorderRadius.circular(Radii.lg),
              borderSide: BorderSide(color: t.hair),
            ),
            enabledBorder: OutlineInputBorder(
              borderRadius: BorderRadius.circular(Radii.lg),
              borderSide: BorderSide(color: t.hair),
            ),
          ),
        ),
        const SizedBox(height: 12),
        Row(
          children: [
            FilledButton(
              key: const ValueKey('assistant-save'),
              onPressed: _saving || _field.text.trim() == current
                  ? null
                  : () => _save(_field.text),
              child: Text(i18n.t('settings:assistant.save')),
            ),
            if (current.isNotEmpty) ...[
              const SizedBox(width: 10),
              TextButton(
                key: const ValueKey('assistant-reset'),
                onPressed: _saving ? null : () => _save(''),
                child: Text(i18n.t('settings:assistant.reset')),
              ),
            ],
          ],
        ),
        const SizedBox(height: 14),
        Text(
          i18n.t('settings:assistant.note'),
          style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
        ),
      ],
    );
  }
}
