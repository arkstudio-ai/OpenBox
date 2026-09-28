import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../../shared/appearance/tokens.dart';
import '../../../../shared/appearance/type_scale.dart';
import '../../../../shared/i18n/i18n.dart';
import '../../../../shared/models/interaction.dart';

/// `detail.kind` of the persona summary card (docs/OPS_CASE_PLAN.md §4.3):
/// five editable memory summaries with 「确认」「稍后」 underneath.
const storePersonaBundleKind = 'store_persona_bundle';

const _laterLabels = {'稍后', 'Later'};

/// One proposed memory in the bundle.
class PersonaBundleItem {
  const PersonaBundleItem({
    required this.memoryId,
    required this.type,
    required this.label,
    required this.summary,
  });

  final String memoryId;
  final String type;
  final String label;
  final String summary;
}

/// The bundle a question carries, or null for any other question.
List<PersonaBundleItem>? readPersonaBundle(Map<String, dynamic>? detail) {
  if (detail == null || detail['kind'] != storePersonaBundleKind) return null;
  final raw = detail['items'];
  if (raw is! List) return const [];
  return [
    for (final item in raw)
      if (item is Map<String, dynamic>)
        PersonaBundleItem(
          memoryId: _text(item, 'memory_id'),
          type: _text(item, 'type'),
          label: _text(item, 'label'),
          summary: _text(item, 'summary'),
        ),
  ];
}

String _text(Map<String, dynamic> record, String key) {
  final value = record[key];
  return value is String ? value : '';
}

/// Edits per question request (`memory_id → text`). Outside the card so a
/// rebuild of the lazy transcript row does not lose what was typed.
final personaEditsProvider =
    StateProvider.family<Map<String, String>, String>((ref, _) => const {});

/// Which items differ from their proposed summary.
Map<String, String> personaChanges(
  List<PersonaBundleItem> items,
  Map<String, String> edits,
) => {
  for (final item in items)
    if (edits[item.memoryId] case final text?
        when text.trim() != item.summary.trim())
      item.memoryId: text.trim(),
};

/// The answer row to send for a persona question: 「稍后」 goes as picked;
/// 「确认」 with edits becomes one custom string carrying them; anything else
/// (「确认」 untouched) is null, meaning "send the picked label as is".
List<String>? personaAnswer({
  required List<String> picked,
  required List<PersonaBundleItem> items,
  required Map<String, String> edits,
}) {
  if (picked.isEmpty || picked.any(_laterLabels.contains)) return null;
  final changes = personaChanges(items, edits);
  if (changes.isEmpty) return null;
  return [
    jsonEncode({'confirm': true, 'items': changes}),
  ];
}

/// True for an answer string produced by [personaAnswer], so the record in
/// the transcript can read 「已确认（有修改）」 instead of raw JSON.
bool isPersonaEditAnswer(String answer) {
  if (!answer.startsWith('{')) return false;
  try {
    final decoded = jsonDecode(answer);
    return decoded is Map && decoded['confirm'] == true && decoded['items'] is Map;
  } catch (_) {
    return false;
  }
}

/// Editable summaries above the option chips of a persona question.
class StorePersonaDetail extends ConsumerStatefulWidget {
  const StorePersonaDetail({
    super.key,
    required this.item,
    required this.requestId,
    this.enabled = true,
  });

  final QuestionItem item;
  final String requestId;
  final bool enabled;

  @override
  ConsumerState<StorePersonaDetail> createState() => _StorePersonaDetailState();
}

class _StorePersonaDetailState extends ConsumerState<StorePersonaDetail> {
  late final List<PersonaBundleItem> _items =
      readPersonaBundle(widget.item.detail) ?? const [];
  late final Map<String, TextEditingController> _controllers;

  @override
  void initState() {
    super.initState();
    final edits = ref.read(personaEditsProvider(widget.requestId));
    _controllers = {
      for (final item in _items)
        item.memoryId: TextEditingController(
          text: edits[item.memoryId] ?? item.summary,
        ),
    };
  }

  @override
  void dispose() {
    for (final controller in _controllers.values) {
      controller.dispose();
    }
    super.dispose();
  }

  void _write(String memoryId, String text) {
    final edits = ref.read(personaEditsProvider(widget.requestId).notifier);
    edits.state = {...edits.state, memoryId: text};
  }

  @override
  Widget build(BuildContext context) {
    if (_items.isEmpty) return const SizedBox.shrink();
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final edits = ref.watch(personaEditsProvider(widget.requestId));
    final changed = personaChanges(_items, edits);
    return Container(
      margin: const EdgeInsets.only(top: 8),
      padding: const EdgeInsets.all(11),
      decoration: BoxDecoration(
        border: Border.all(color: t.hair),
        borderRadius: BorderRadius.circular(Radii.md),
        color: t.bg,
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            i18n.t('chat:question.persona.hint'),
            style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
          ),
          for (final item in _items) ...[
            const SizedBox(height: 10),
            Row(
              children: [
                Expanded(
                  child: Text(
                    item.label.isNotEmpty ? item.label : item.type,
                    style: TextStyle(
                      fontSize: FontSizes.sm,
                      fontWeight: FontWeight.w500,
                      color: t.ink,
                    ),
                  ),
                ),
                if (changed.containsKey(item.memoryId))
                  Text(
                    i18n.t('chat:question.persona.edited'),
                    style: TextStyle(fontSize: FontSizes.xs, color: t.a800),
                  ),
              ],
            ),
            const SizedBox(height: 4),
            TextField(
              key: Key('persona-${item.memoryId}'),
              controller: _controllers[item.memoryId],
              enabled: widget.enabled,
              minLines: 2,
              maxLines: 6,
              maxLength: 2000,
              onChanged: (text) => _write(item.memoryId, text),
              style: TextStyle(
                fontSize: FontSizes.sm,
                height: 1.5,
                color: t.ink,
              ),
              decoration: InputDecoration(
                counterText: '',
                isDense: true,
                filled: true,
                fillColor: t.card,
                contentPadding: const EdgeInsets.symmetric(
                  horizontal: 10,
                  vertical: 8,
                ),
                enabledBorder: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(Radii.md),
                  borderSide: BorderSide(color: t.hair),
                ),
                focusedBorder: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(Radii.md),
                  borderSide: BorderSide(color: t.accent),
                ),
                disabledBorder: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(Radii.md),
                  borderSide: BorderSide(color: t.hair),
                ),
              ),
            ),
          ],
        ],
      ),
    );
  }
}
