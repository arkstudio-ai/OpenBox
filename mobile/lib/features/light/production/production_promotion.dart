import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/i18n/i18n.dart';
import 'production_voice.dart' show VoiceCard;
import 'production_widgets.dart';

const _briefLimits = {
  'audience': 120,
  'offer': 160,
  'call_to_action': 120,
  'details': 400,
};

String _text(I18nState i18n, String key) =>
    i18n.t('light:production.promotion.$key');

class ProductionPromotion extends ConsumerWidget {
  const ProductionPromotion({
    super.key,
    required this.brief,
    required this.enabled,
    required this.onChanged,
  });

  final Map<String, String> brief;
  final bool enabled;
  final ValueChanged<Map<String, String>> onChanged;

  Future<void> _open(BuildContext context, I18nState i18n) async {
    final edited = await showModalBottomSheet<Map<String, String>>(
      context: context,
      backgroundColor: context.tokens.bg,
      isScrollControlled: true,
      useSafeArea: true,
      showDragHandle: true,
      builder: (_) => _PromotionSheet(brief: brief, i18n: i18n),
    );
    if (context.mounted && edited != null && !mapEquals(edited, brief)) {
      onChanged(edited);
    }
  }

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final i18n = ref.watch(i18nProvider);
    final primarySummary = [
      for (final key in ['audience', 'offer', 'call_to_action'])
        if (brief[key]?.isNotEmpty == true) brief[key]!,
    ].join(' · ');
    final summary = primarySummary.isEmpty
        ? brief['details'] ?? ''
        : primarySummary;
    return VoiceCard(
      child: ProductionSettingRow(
        key: const Key('production-promotion'),
        icon: Icons.outlined_flag_rounded,
        label: _text(i18n, 'title'),
        detail: summary.isEmpty ? _text(i18n, 'empty') : summary,
        detailMaxLines: 2,
        minTileHeight: 68,
        value: summary.isEmpty ? _text(i18n, 'optional') : null,
        onTap: enabled ? () => _open(context, i18n) : null,
      ),
    );
  }
}

class _PromotionSheet extends StatefulWidget {
  const _PromotionSheet({required this.brief, required this.i18n});

  final Map<String, String> brief;
  final I18nState i18n;

  @override
  State<_PromotionSheet> createState() => _PromotionSheetState();
}

class _PromotionSheetState extends State<_PromotionSheet> {
  late final _fields = {
    for (final key in _briefLimits.keys)
      key: TextEditingController(text: widget.brief[key]),
  };

  @override
  void dispose() {
    for (final field in _fields.values) {
      field.dispose();
    }
    super.dispose();
  }

  void _finish() {
    Navigator.pop(context, {
      for (final entry in _fields.entries)
        if (entry.value.text.trim().isNotEmpty)
          entry.key: entry.value.text.trim(),
    });
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = widget.i18n;
    final border = OutlineInputBorder(
      borderRadius: BorderRadius.circular(12),
      borderSide: BorderSide(color: t.hair),
    );

    Widget field(String key) {
      final multiline = key == 'details';
      final label = _text(i18n, key);
      return Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        spacing: 8,
        children: [
          ExcludeSemantics(
            child: Text(
              label,
              style: TextStyle(
                color: t.ink,
                fontSize: 15,
                fontWeight: FontWeight.w600,
              ),
            ),
          ),
          Semantics(
            label: label,
            child: TextField(
              key: Key('production-promotion-$key'),
              controller: _fields[key],
              maxLength: _briefLimits[key],
              minLines: multiline ? 2 : 1,
              maxLines: multiline ? 4 : 1,
              textInputAction: multiline
                  ? TextInputAction.newline
                  : TextInputAction.next,
              style: TextStyle(color: t.ink, fontSize: 16, height: 1.4),
              decoration: InputDecoration(
                hintText: multiline ? _text(i18n, 'detailsHint') : null,
                contentPadding: const EdgeInsets.symmetric(
                  horizontal: 14,
                  vertical: 15,
                ),
                constraints: const BoxConstraints(minHeight: 52),
                filled: true,
                fillColor: t.card,
                border: border,
                enabledBorder: border,
                focusedBorder: border.copyWith(
                  borderSide: BorderSide(color: t.a700),
                ),
                counterText: '',
              ),
            ),
          ),
        ],
      );
    }

    return Padding(
      padding: EdgeInsets.only(bottom: MediaQuery.viewInsetsOf(context).bottom),
      child: SafeArea(
        top: false,
        minimum: const EdgeInsets.fromLTRB(16, 0, 16, 8),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Row(
              children: [
                Expanded(child: ProductionHeading(_text(i18n, 'title'))),
                TextButton(
                  key: const Key('production-promotion-close'),
                  onPressed: () => Navigator.pop(context),
                  style: TextButton.styleFrom(foregroundColor: t.ink),
                  child: Text(_text(i18n, 'close')),
                ),
              ],
            ),
            Flexible(
              child: SingleChildScrollView(
                padding: const EdgeInsets.only(top: 16, bottom: 20),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  spacing: 16,
                  children: [for (final key in _briefLimits.keys) field(key)],
                ),
              ),
            ),
            ProductionPrimary(
              key: const Key('production-promotion-done'),
              label: _text(i18n, 'done'),
              onPressed: _finish,
            ),
          ],
        ),
      ),
    );
  }
}
