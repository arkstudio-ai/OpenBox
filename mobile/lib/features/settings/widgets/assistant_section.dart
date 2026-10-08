import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/assistant_profile.dart';
import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/widgets/toast.dart';

/// 个人助理 (web `AssistantPage`): its name, what it calls the person and how
/// it talks, followed by the web, this app, its own replies and calls. Below,
/// what it learned on its own, each removable.
class AssistantSection extends ConsumerStatefulWidget {
  const AssistantSection({super.key});

  @override
  ConsumerState<AssistantSection> createState() => _AssistantSectionState();
}

class _AssistantSectionState extends ConsumerState<AssistantSection> {
  final _name = TextEditingController();
  final _address = TextEditingController();
  final _persona = TextEditingController();

  // The stored profile the fields were last filled from; a newer one (a
  // rename in chat, another device) refills them unless the person has
  // started changing this page.
  AssistantProfile? _filledFrom;

  /// Choices made here and not saved yet.
  String? _tone;
  String? _length;
  bool? _emoji;
  bool _saving = false;

  @override
  void dispose() {
    _name.dispose();
    _address.dispose();
    _persona.dispose();
    super.dispose();
  }

  void _fill(AssistantProfile stored) {
    _name.text = stored.name;
    _address.text = stored.address;
    _persona.text = stored.persona;
    _tone = _length = null;
    _emoji = null;
    _filledFrom = stored;
  }

  /// What would change: names and the description as the server keeps them
  /// (trimmed), the choices as picked.
  Map<String, Object> _patch(AssistantProfile stored) => {
    if (_name.text.trim() != stored.name) 'name': _name.text.trim(),
    if (_address.text.trim() != stored.address) 'address': _address.text.trim(),
    if (_persona.text.trim() != stored.persona) 'persona': _persona.text.trim(),
    if (_tone != null && _tone != stored.tone) 'tone': _tone!,
    if (_length != null && _length != stored.length) 'length': _length!,
    if (_emoji != null && _emoji != stored.emoji) 'emoji': _emoji!,
  };

  Future<void> _save(AssistantProfile stored) async {
    final i18n = ref.read(i18nProvider);
    final toast = ref.read(toastProvider.notifier);
    setState(() => _saving = true);
    try {
      final kept = await ref
          .read(assistantProfileProvider.notifier)
          .save(_patch(stored));
      if (!mounted) return;
      setState(() => _fill(kept));
      toast.success(i18n.t('settings:assistant.saved'));
    } catch (error) {
      if (!mounted) return;
      final invalid =
          error is DioException && error.response?.statusCode == 422;
      toast.error(
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
    final stored =
        ref.watch(assistantProfileProvider).valueOrNull ??
        const AssistantProfile();
    final filled = _filledFrom;
    final untouched =
        filled == null ||
        (_name.text == filled.name &&
            _address.text == filled.address &&
            _persona.text == filled.persona &&
            _tone == null &&
            _length == null &&
            _emoji == null);
    if (!identical(filled, stored) && untouched) _fill(stored);
    final changed = _patch(stored).isNotEmpty;
    final tone = _tone ?? stored.tone;
    final length = _length ?? stored.length;
    final emoji = _emoji ?? stored.emoji;

    Widget label(String key) => Padding(
      padding: const EdgeInsets.only(bottom: 8),
      child: Text(
        i18n.t(key),
        style: TextStyle(fontSize: FontSizes.sm, color: t.n700),
      ),
    );

    return ListView(
      padding: const EdgeInsets.all(16),
      children: [
        label('settings:assistant.nameLabel'),
        _Field(
          fieldKey: const ValueKey('assistant-name'),
          controller: _name,
          maxLength: 20,
          hint: i18n.t('settings:assistant.placeholder'),
          onChanged: () => setState(() {}),
        ),
        const SizedBox(height: 18),
        label('settings:assistant.addressLabel'),
        _Field(
          fieldKey: const ValueKey('assistant-address'),
          controller: _address,
          maxLength: 20,
          hint: i18n.t('settings:assistant.addressPlaceholder'),
          onChanged: () => setState(() {}),
        ),
        const SizedBox(height: 18),
        label('settings:assistant.toneLabel'),
        _Choices(
          prefix: 'tone',
          options: AssistantProfile.tones,
          value: tone,
          text: (option) => i18n.t('settings:assistant.tone.$option'),
          onPick: (option) => setState(() => _tone = option),
        ),
        const SizedBox(height: 18),
        label('settings:assistant.lengthLabel'),
        _Choices(
          prefix: 'length',
          options: AssistantProfile.lengths,
          value: length,
          text: (option) => i18n.t('settings:assistant.length.$option'),
          onPick: (option) => setState(() => _length = option),
        ),
        const SizedBox(height: 10),
        SwitchListTile.adaptive(
          key: const ValueKey('assistant-emoji'),
          contentPadding: EdgeInsets.zero,
          value: emoji,
          onChanged: (value) => setState(() => _emoji = value),
          title: Text(
            i18n.t('settings:assistant.emoji'),
            style: TextStyle(fontSize: FontSizes.base, color: t.ink),
          ),
          subtitle: Text(
            i18n.t('settings:assistant.emojiHint'),
            style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
          ),
        ),
        const SizedBox(height: 10),
        label('settings:assistant.personaLabel'),
        _Field(
          fieldKey: const ValueKey('assistant-persona'),
          controller: _persona,
          maxLength: 300,
          maxLines: 4,
          hint: i18n.t('settings:assistant.personaPlaceholder'),
          onChanged: () => setState(() {}),
        ),
        const SizedBox(height: 6),
        Text(
          i18n.t('settings:assistant.personaHint'),
          style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
        ),
        const SizedBox(height: 16),
        Align(
          alignment: Alignment.centerLeft,
          child: FilledButton(
            key: const ValueKey('assistant-save'),
            onPressed: _saving || !changed ? null : () => _save(stored),
            child: Text(i18n.t('settings:assistant.save')),
          ),
        ),
        const SizedBox(height: 14),
        Text(
          i18n.t('settings:assistant.note'),
          style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
        ),
        const SizedBox(height: 20),
        const _Learned(),
      ],
    );
  }
}

class _Field extends ConsumerWidget {
  const _Field({
    required this.fieldKey,
    required this.controller,
    required this.maxLength,
    required this.hint,
    required this.onChanged,
    this.maxLines = 1,
  });

  final Key fieldKey;
  final TextEditingController controller;
  final int maxLength;
  final int maxLines;
  final String hint;
  final VoidCallback onChanged;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final border = OutlineInputBorder(
      borderRadius: BorderRadius.circular(Radii.lg),
      borderSide: BorderSide(color: t.hair),
    );
    return TextField(
      key: fieldKey,
      controller: controller,
      maxLength: maxLength,
      maxLines: maxLines,
      minLines: 1,
      onChanged: (_) => onChanged(),
      style: TextStyle(fontSize: FontSizes.base, color: t.ink),
      decoration: InputDecoration(
        hintText: hint,
        hintStyle: TextStyle(fontSize: FontSizes.sm, color: t.n600),
        counterText: '',
        filled: true,
        fillColor: t.card,
        border: border,
        enabledBorder: border,
      ),
    );
  }
}

/// One of a few choices; the chosen one has the ink border.
class _Choices extends StatelessWidget {
  const _Choices({
    required this.prefix,
    required this.options,
    required this.value,
    required this.text,
    required this.onPick,
  });

  final String prefix;
  final List<String> options;
  final String value;
  final String Function(String option) text;
  final ValueChanged<String>? onPick;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Wrap(
      spacing: 8,
      runSpacing: 8,
      children: [
        for (final option in options)
          Semantics(
            button: true,
            selected: option == value,
            child: InkWell(
              key: ValueKey('$prefix-$option'),
              onTap: onPick == null ? null : () => onPick!(option),
              borderRadius: BorderRadius.circular(Radii.md),
              child: Container(
                constraints: const BoxConstraints(minWidth: 72, minHeight: 38),
                alignment: Alignment.center,
                padding: const EdgeInsets.symmetric(horizontal: 14),
                decoration: BoxDecoration(
                  color: t.card,
                  borderRadius: BorderRadius.circular(Radii.md),
                  border: Border.all(color: option == value ? t.ink : t.hair),
                ),
                child: Text(
                  text(option),
                  style: TextStyle(fontSize: FontSizes.sm, color: t.ink),
                ),
              ),
            ),
          ),
      ],
    );
  }
}

/// What the assistant learned on its own about how the person likes to be
/// helped, and the reasons picked with thumbs-downs this month.
class _Learned extends ConsumerStatefulWidget {
  const _Learned();

  @override
  ConsumerState<_Learned> createState() => _LearnedState();
}

class _LearnedState extends ConsumerState<_Learned> {
  final _removing = <String>{};

  Future<void> _remove(LearnedItem item) async {
    final i18n = ref.read(i18nProvider);
    final toast = ref.read(toastProvider.notifier);
    setState(() => _removing.add(item.id));
    try {
      await ref
          .read(assistantProfileApiProvider)
          .forget(
            item,
            requestId:
                'settings-learned:${item.id}:${item.revision}:${DateTime.now().microsecondsSinceEpoch}',
          );
      ref.invalidate(learnedStyleProvider);
      toast.success(i18n.t('settings:assistant.learned.removed'));
    } catch (_) {
      toast.error(i18n.t('settings:assistant.learned.removeFailed'));
    } finally {
      if (mounted) setState(() => _removing.remove(item.id));
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final data = ref.watch(learnedStyleProvider).valueOrNull;
    if (data == null) return const SizedBox.shrink();
    return Column(
      key: const ValueKey('assistant-learned'),
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Divider(color: t.hair, height: 1),
        const SizedBox(height: 18),
        Text(
          i18n.t('settings:assistant.learned.title'),
          style: TextStyle(
            fontSize: FontSizes.sm,
            fontWeight: FontWeight.w500,
            color: t.ink,
          ),
        ),
        const SizedBox(height: 4),
        Text(
          i18n.t('settings:assistant.learned.hint'),
          style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
        ),
        const SizedBox(height: 10),
        if (data.learned.isEmpty)
          Text(
            i18n.t('settings:assistant.learned.empty'),
            style: TextStyle(fontSize: FontSizes.sm, color: t.n600),
          )
        else
          Container(
            decoration: BoxDecoration(
              color: t.card,
              border: Border.all(color: t.hair),
              borderRadius: BorderRadius.circular(Radii.md),
            ),
            child: Column(
              children: [
                for (final (index, item) in data.learned.indexed) ...[
                  if (index > 0) Divider(color: t.hair, height: 1),
                  Padding(
                    padding: const EdgeInsets.fromLTRB(14, 8, 6, 8),
                    child: Row(
                      children: [
                        Expanded(
                          child: Text(
                            item.summary,
                            style: TextStyle(
                              fontSize: FontSizes.sm,
                              height: 1.5,
                              color: t.ink,
                            ),
                          ),
                        ),
                        TextButton(
                          key: ValueKey('learned-remove-${item.id}'),
                          onPressed: _removing.contains(item.id)
                              ? null
                              : () => _remove(item),
                          child: Text(
                            i18n.t('settings:assistant.learned.remove'),
                            style: TextStyle(
                              fontSize: FontSizes.xs,
                              color: t.n700,
                            ),
                          ),
                        ),
                      ],
                    ),
                  ),
                ],
              ],
            ),
          ),
        if (data.reactions.isNotEmpty) ...[
          const SizedBox(height: 14),
          Text(
            i18n.t('settings:assistant.learned.reactions'),
            style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
          ),
          const SizedBox(height: 8),
          Wrap(
            spacing: 6,
            runSpacing: 6,
            children: [
              for (final (reason, count) in data.reactions)
                Container(
                  padding: const EdgeInsets.symmetric(
                    horizontal: 10,
                    vertical: 4,
                  ),
                  decoration: BoxDecoration(
                    color: t.hairSoft,
                    borderRadius: BorderRadius.circular(Radii.full),
                  ),
                  child: Text(
                    i18n.t(
                      'settings:assistant.learned.reaction',
                      vars: {
                        'reason': i18n.t('chat:meta.reason.$reason'),
                        'count': count,
                      },
                    ),
                    style: TextStyle(fontSize: FontSizes.xs, color: t.n700),
                  ),
                ),
            ],
          ),
        ],
      ],
    );
  }
}
