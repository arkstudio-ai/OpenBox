import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../../shared/appearance/tokens.dart';
import '../../../../shared/appearance/type_scale.dart';
import '../../../../shared/i18n/i18n.dart';
import '../../../../shared/models/app_config.dart';
import '../../state/chat_session_controller.dart';
import '../../state/config_providers.dart';
import '../../utils/reasoning.dart';

/// Model picker bottom sheet (web `ModelPicker`): checked list from
/// `GET /api/agent/config`.
Future<void> showModelPicker(
  BuildContext context,
  WidgetRef ref, {
  required String sessionKey,
  required String? currentModel,
}) {
  final t = context.tokens;
  final i18n = ref.read(i18nProvider);
  final config = ref.read(appConfigProvider).valueOrNull;
  final models = config?.models ?? const <ModelInfo>[];
  final active =
      ref.read(pickedModelProvider(sessionKey)) ??
      (currentModel?.isNotEmpty == true ? currentModel : config?.defaultModel);
  return showModalBottomSheet<void>(
    context: context,
    builder: (sheetContext) => SafeArea(
      child: ListView(
        shrinkWrap: true,
        padding: const EdgeInsets.symmetric(vertical: 8),
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(20, 8, 20, 8),
            child: Text(
              i18n.t('chat:model.pick'),
              style: TextStyle(
                fontSize: FontSizes.sm,
                fontWeight: FontWeight.w600,
                color: t.n600,
              ),
            ),
          ),
          for (final model in models)
            ListTile(
              dense: true,
              title: Text(
                model.name,
                style: TextStyle(fontSize: FontSizes.base, color: t.ink),
              ),
              subtitle: model.provider == null
                  ? null
                  : Text(
                      model.provider!,
                      style: TextStyle(fontSize: FontSizes.xs, color: t.n500),
                    ),
              trailing: model.id == active
                  ? Icon(Icons.check, size: 18, color: t.a700)
                  : null,
              onTap: () {
                ref.read(pickedModelProvider(sessionKey).notifier).state =
                    model.id;
                Navigator.pop(sheetContext);
              },
            ),
        ],
      ),
    ),
  );
}

/// Reasoning-strength picker (web `ReasoningPicker`): the levels the active
/// model declares, plus "default" which clears the conversation override.
/// Never opened for a model that declares none — the pill is hidden then.
Future<void> showReasoningPicker(
  BuildContext context,
  WidgetRef ref, {
  required String sessionKey,
  required String modelId,
  required ReasoningChoice choice,
}) {
  final t = context.tokens;
  final i18n = ref.read(i18nProvider);
  final defaultId = choice.defaultId;
  final key = reasoningKey(sessionKey, modelId);

  Widget row(BuildContext sheetContext, String? id, String label) => ListTile(
    dense: true,
    title: Text(
      label,
      style: TextStyle(fontSize: FontSizes.base, color: t.ink),
    ),
    trailing: id == choice.activeId
        ? Icon(Icons.check, size: 18, color: t.a700)
        : null,
    onTap: () {
      ref.read(pickedVariantProvider(key).notifier).state = Variant(id);
      Navigator.pop(sheetContext);
    },
  );

  return showModalBottomSheet<void>(
    context: context,
    builder: (sheetContext) => SafeArea(
      child: ListView(
        shrinkWrap: true,
        padding: const EdgeInsets.symmetric(vertical: 8),
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(20, 8, 20, 8),
            child: Text(
              i18n.t('chat:reasoning.pick'),
              style: TextStyle(
                fontSize: FontSizes.sm,
                fontWeight: FontWeight.w600,
                color: t.n600,
              ),
            ),
          ),
          row(
            sheetContext,
            null,
            defaultId == null
                ? i18n.t('chat:reasoning.default')
                : i18n.t(
                    'chat:reasoning.defaultWithLevel',
                    vars: {'level': reasoningLevelLabel(i18n, defaultId)},
                  ),
          ),
          for (final id in choice.variants)
            row(sheetContext, id, reasoningLevelLabel(i18n, id)),
        ],
      ),
    ),
  );
}

/// Known level ids get a translated label; anything else shows the raw id,
/// exactly as the web picker does.
String reasoningLevelLabel(I18nState i18n, String id) {
  const known = [
    'off',
    'none',
    'minimal',
    'low',
    'medium',
    'high',
    'xhigh',
    'max',
  ];
  return known.contains(id) ? i18n.t('chat:reasoning.level.$id') : id;
}

/// Mode/agent picker bottom sheet (web `ModePicker`): build vs plan.
Future<void> showModePicker(
  BuildContext context,
  WidgetRef ref, {
  required String sessionKey,
  required String? currentAgent,
}) {
  final t = context.tokens;
  final i18n = ref.read(i18nProvider);
  final agents =
      ref.read(chatAgentsProvider).valueOrNull ?? const <AgentInfo>[];
  final active = ref.read(pickedAgentProvider(sessionKey)) ?? currentAgent;
  return showModalBottomSheet<void>(
    context: context,
    builder: (sheetContext) => SafeArea(
      child: ListView(
        shrinkWrap: true,
        padding: const EdgeInsets.symmetric(vertical: 8),
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(20, 8, 20, 8),
            child: Text(
              i18n.t('chat:mode.label'),
              style: TextStyle(
                fontSize: FontSizes.sm,
                fontWeight: FontWeight.w600,
                color: t.n600,
              ),
            ),
          ),
          for (final agent in agents)
            ListTile(
              dense: true,
              title: Text(
                _agentLabel(i18n, agent.name),
                style: TextStyle(fontSize: FontSizes.base, color: t.ink),
              ),
              subtitle: Text(
                _agentDescription(i18n, agent),
                style: TextStyle(fontSize: FontSizes.xs, color: t.n500),
              ),
              trailing: agent.name == active
                  ? Icon(Icons.check, size: 18, color: t.a700)
                  : null,
              onTap: () {
                ref.read(pickedAgentProvider(sessionKey).notifier).state =
                    agent.name;
                Navigator.pop(sheetContext);
              },
            ),
        ],
      ),
    ),
  );
}

String _agentLabel(I18nState i18n, String name) {
  final key = 'chat:mode.$name';
  final label = i18n.t(key);
  return label == key ? name : label;
}

String _agentDescription(I18nState i18n, AgentInfo agent) {
  final key = 'chat:mode.${agent.name}Desc';
  final label = i18n.t(key);
  if (label != key) return label;
  return agent.description ?? '';
}
