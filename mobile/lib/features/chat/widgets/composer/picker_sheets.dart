import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../../shared/api/auth_store.dart';
import '../../../../shared/appearance/tokens.dart';
import '../../../../shared/appearance/type_scale.dart';
import '../../../../shared/i18n/i18n.dart';
import '../../../../shared/models/app_config.dart';
import '../../state/chat_session_controller.dart';
import '../../state/config_providers.dart';
import '../../utils/reasoning.dart';

/// Mobile model picker: models from `GET /api/agent/config`, followed by a
/// second sheet for the reasoning strengths declared by the chosen model.
/// Models without strengths are selected immediately.
Future<void> showModelPicker(
  BuildContext context,
  WidgetRef ref, {
  required String sessionKey,
  required String? currentModel,
  required String? currentVariant,
}) async {
  final t = context.tokens;
  final i18n = ref.read(i18nProvider);
  final config = ref.read(appConfigProvider).valueOrNull;
  final models = config?.models ?? const <ModelInfo>[];
  final active = ref.read(pickedModelProvider(sessionKey)) ??
      (currentModel?.isNotEmpty == true ? currentModel : config?.defaultModel);

  ReasoningChoice choiceFor(ModelInfo model) => resolveReasoning(
        model: model,
        sessionModel: currentModel,
        sessionVariant: currentVariant,
        pick: ref.read(
            pickedVariantProvider(reasoningKey(sessionKey, model.id))),
      );

  void choose(ModelInfo model, [String? level]) {
    if (model.variants.isNotEmpty) {
      ref
          .read(pickedVariantProvider(reasoningKey(sessionKey, model.id))
              .notifier)
          .state = Variant(level);
    }
    ref.read(pickedModelProvider(sessionKey).notifier).state = model.id;
  }

  Widget modelRow(BuildContext sheetContext, ModelInfo model) {
    final isActive = model.id == active;
    final choice = choiceFor(model);
    final hasReasoning = choice.variants.isNotEmpty;
    return ListTile(
      dense: true,
      title: Text(model.name,
          style: TextStyle(fontSize: FontSizes.base, color: t.ink)),
      subtitle: model.provider == null
          ? null
          : Text(model.provider!,
              style: TextStyle(fontSize: FontSizes.xs, color: t.n500)),
      trailing: isActive
          ? Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                if (hasReasoning) ...[
                  Text(
                    _reasoningChoiceLabel(i18n, choice),
                    style: TextStyle(fontSize: FontSizes.xs, color: t.n500),
                  ),
                  const SizedBox(width: 6),
                ],
                Icon(Icons.check, size: 18, color: t.a700),
                if (hasReasoning)
                  Icon(Icons.chevron_right, size: 18, color: t.n500),
              ],
            )
          : (hasReasoning
              ? Icon(Icons.chevron_right, size: 18, color: t.n500)
              : null),
      onTap: () async {
        Navigator.pop(sheetContext);
        if (!hasReasoning) {
          choose(model);
          return;
        }
        if (!context.mounted) return;
        await _showModelReasoningPicker(
          context,
          model: model,
          choice: choice,
          onPick: (level) => choose(model, level),
        );
      },
    );
  }

  await showModalBottomSheet<void>(
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
          for (final model in models) modelRow(sheetContext, model),
        ],
      ),
    ),
  );
}

/// Second step of the mobile model picker. The model is not committed until a
/// strength (or its default) is chosen, so dismissing this sheet changes
/// neither half of the pair.
Future<void> _showModelReasoningPicker(
  BuildContext context, {
  required ModelInfo model,
  required ReasoningChoice choice,
  required void Function(String?) onPick,
}) {
  final t = context.tokens;
  final container = ProviderScope.containerOf(context, listen: false);
  final i18n = container.read(i18nProvider);
  final defaultId = choice.defaultId;

  Widget row(BuildContext sheetContext, String? id, String label) => ListTile(
        dense: true,
        title: Text(label,
            style: TextStyle(fontSize: FontSizes.base, color: t.ink)),
        trailing: id == choice.activeId
            ? Icon(Icons.check, size: 18, color: t.a700)
            : null,
        onTap: () {
          onPick(id);
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
              model.name,
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
                : i18n.t('chat:reasoning.defaultWithLevel', vars: {
                    'level': reasoningLevelLabel(i18n, defaultId),
                  }),
          ),
          for (final id in choice.variants)
            row(sheetContext, id, reasoningLevelLabel(i18n, id)),
        ],
      ),
    ),
  );
}

String _reasoningChoiceLabel(I18nState i18n, ReasoningChoice choice) {
  final activeId = choice.activeId;
  if (activeId != null) return reasoningLevelLabel(i18n, activeId);
  final defaultId = choice.defaultId;
  return defaultId == null
      ? i18n.t('chat:reasoning.default')
      : i18n.t('chat:reasoning.defaultWithLevel', vars: {
          'level': reasoningLevelLabel(i18n, defaultId),
        });
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
  final agents = ref.read(chatAgentsProvider).valueOrNull ?? const <AgentInfo>[];
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

/// Video-model picker (web `VideoModelPicker`): each model with the resolution
/// tiers it publishes.
///
/// Two sheets rather than the web's inline chips. On a phone the tiers do not
/// fit beside a model name without shrinking both past readability, and a
/// second sheet is the gesture people already know from every other picker
/// here. A model with one tier skips the second sheet — there is nothing to
/// choose — and is selected outright.
Future<void> showVideoModelPicker(
  BuildContext context,
  WidgetRef ref, {
  required String sessionKey,
  required String? currentModel,
  required String? currentResolution,
}) async {
  final t = context.tokens;
  final i18n = ref.read(i18nProvider);
  final config = ref.read(appConfigProvider).valueOrNull;
  final models = config?.videoModels ?? const <VideoModelInfo>[];
  if (models.isEmpty) return;

  final picked = ref.read(pickedVideoProvider(sessionKey));
  final activeId = picked?.modelId ??
      (currentModel?.isNotEmpty == true ? currentModel : config?.defaultVideoModel);
  final activeTier = picked?.resolution ??
      (currentResolution?.isNotEmpty == true
          ? currentResolution
          : config?.defaultVideoResolution);

  void choose(String modelId, String resolution) {
    ref.read(pickedVideoProvider(sessionKey).notifier).state =
        VideoPick(modelId, resolution);
  }

  await showModalBottomSheet<void>(
    context: context,
    builder: (sheetContext) => SafeArea(
      child: ListView(
        shrinkWrap: true,
        padding: const EdgeInsets.symmetric(vertical: 8),
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(20, 8, 20, 8),
            child: Text(
              i18n.t('chat:videoModel.pick'),
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
              title: Text(model.name,
                  style: TextStyle(fontSize: FontSizes.base, color: t.ink)),
              subtitle: Text(
                [
                  ?model.tier,
                  model.resolutions.join(' / '),
                ].join(' · '),
                style: TextStyle(fontSize: FontSizes.xs, color: t.n500),
              ),
              // The chevron says "there is a second step here", so the row
              // you are already on needs it too — its tiers are exactly the
              // ones you are most likely to want to change. Showing only the
              // check made the current model read as the one model whose
              // resolution was fixed, when tapping it opens the tiers like
              // any other.
              trailing: model.id == activeId
                  ? Row(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        if (activeTier != null)
                          Text(activeTier,
                              style: TextStyle(
                                  fontSize: FontSizes.xs, color: t.n500)),
                        const SizedBox(width: 6),
                        Icon(Icons.check, size: 18, color: t.a700),
                        if (model.resolutions.length > 1)
                          Icon(Icons.chevron_right, size: 18, color: t.n500),
                      ],
                    )
                  : (model.resolutions.length > 1
                      ? Icon(Icons.chevron_right, size: 18, color: t.n500)
                      : null),
              onTap: () async {
                Navigator.pop(sheetContext);
                final tiers = model.resolutions;
                if (tiers.length <= 1) {
                  choose(model.id, tiers.isEmpty ? '' : tiers.first);
                  return;
                }
                if (!context.mounted) return;
                await _showResolutionPicker(
                  context,
                  model: model,
                  current: model.id == activeId ? activeTier : null,
                  onPick: (tier) => choose(model.id, tier),
                );
              },
            ),
        ],
      ),
    ),
  );
}

Future<void> _showResolutionPicker(
  BuildContext context, {
  required VideoModelInfo model,
  required String? current,
  required void Function(String) onPick,
}) {
  final t = context.tokens;
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
              model.name,
              style: TextStyle(
                fontSize: FontSizes.sm,
                fontWeight: FontWeight.w600,
                color: t.n600,
              ),
            ),
          ),
          for (final tier in model.resolutions)
            ListTile(
              dense: true,
              title: Text(tier,
                  style: TextStyle(fontSize: FontSizes.base, color: t.ink)),
              trailing: tier == current
                  ? Icon(Icons.check, size: 18, color: t.a700)
                  : null,
              onTap: () {
                onPick(tier);
                Navigator.pop(sheetContext);
              },
            ),
        ],
      ),
    ),
  );
}


/// Which chat tier the composer is on, if any: matched on the model, so an
/// admin retuning the strength is still on that tier.
String? activeChatTier(AppConfig config, String modelId) {
  for (final row in config.modelTiers.chat) {
    if (row.model == modelId) return row.tier;
  }
  return null;
}

/// Which video tier the composer is on, if any: the model must match and the
/// resolution must be one the tier offers, since a tier is a model plus the
/// resolutions it lets you pick.
String? activeVideoTier(AppConfig config, String modelId, String resolution) {
  for (final row in config.modelTiers.video) {
    if (row.model == modelId &&
        (row.resolutions.isEmpty || row.resolutions.contains(resolution))) {
      return row.tier;
    }
  }
  return null;
}

/// The tier's own wording when the deployment gave one, else the UI's.
String tierLabel(I18nState i18n, String kind, String? tier) =>
    i18n.t('chat:tier.$kind.$tier');

String videoTierLabel(I18nState i18n, VideoTierRow row) =>
    row.label.isNotEmpty ? row.label : tierLabel(i18n, 'video', row.tier);

/// "720p · 0.60 积分/秒", or just the resolution when it is unpriced.
String resolutionWithPrice(I18nState i18n, VideoTierRow row, String resolution) {
  final price = row.prices[resolution];
  if (price == null) return resolution;
  return '$resolution · ${i18n.t('chat:tier.video.perSecond', vars: {'price': price})}';
}

Widget _tierHeading(BossipTokens t, String text) => Padding(
      padding: const EdgeInsets.fromLTRB(20, 8, 20, 8),
      child: Text(
        text,
        style: TextStyle(
          fontSize: FontSizes.sm,
          fontWeight: FontWeight.w600,
          color: t.n600,
        ),
      ),
    );

Widget _tierRow(
  BossipTokens t, {
  required String label,
  required String hint,
  required bool selected,
  required VoidCallback onTap,
}) =>
    ListTile(
      dense: true,
      title: Text(label,
          style: TextStyle(
              fontSize: FontSizes.base,
              fontWeight: FontWeight.w500,
              color: t.ink)),
      // What the tier resolves to, in small print: a price signal with
      // nothing behind it is not one.
      subtitle: Text(hint, style: TextStyle(fontSize: FontSizes.xs, color: t.n500)),
      trailing: selected ? Icon(Icons.check, size: 18, color: t.a700) : null,
      onTap: onTap,
    );

/// The catalogue behind the tiers is an admin's view. Everyone else reads
/// the three tiers and nothing about routing.
Widget? _catalogueRow(
  BossipTokens t,
  I18nState i18n,
  WidgetRef ref, {
  required VoidCallback onTap,
}) {
  if (ref.read(authProvider).user?.role != 'admin') return null;
  return ListTile(
    dense: true,
    leading: Icon(Icons.tune, size: 18, color: t.n600),
    title: Text(i18n.t('chat:tier.more'),
        style: TextStyle(fontSize: FontSizes.sm, color: t.n600)),
    onTap: onTap,
  );
}

/// Chat tier picker (web `TierPicker` for the chat model): three tiers, each
/// resolving to a model and a strength in one tap. The strength is stored
/// against the tier's model, exactly as the two-step picker would have.
Future<void> showChatTierPicker(
  BuildContext context,
  WidgetRef ref, {
  required String sessionKey,
  required String? currentModel,
  required String? currentVariant,
}) async {
  final t = context.tokens;
  final i18n = ref.read(i18nProvider);
  final config = ref.read(appConfigProvider).valueOrNull;
  if (config == null || config.modelTiers.chat.isEmpty) return;
  final activeId = activeModelId(
    picked: ref.read(pickedModelProvider(sessionKey)),
    sessionModel: currentModel,
    defaultModel: config.defaultModel,
  );
  final active = activeChatTier(config, activeId);

  void choose(ChatTierRow row) {
    final target = config.byId(row.model);
    if (target != null && target.variants.isNotEmpty) {
      final level = row.variant != null && target.variants.contains(row.variant)
          ? row.variant
          : null;
      ref
          .read(pickedVariantProvider(reasoningKey(sessionKey, row.model))
              .notifier)
          .state = Variant(level);
    }
    ref.read(pickedModelProvider(sessionKey).notifier).state = row.model;
  }

  await showModalBottomSheet<void>(
    context: context,
    builder: (sheetContext) => SafeArea(
      child: ListView(
        shrinkWrap: true,
        padding: const EdgeInsets.symmetric(vertical: 8),
        children: [
          _tierHeading(t, i18n.t('chat:tier.chat.pick')),
          for (final row in config.modelTiers.chat)
            _tierRow(
              t,
              label: tierLabel(i18n, 'chat', row.tier),
              hint: config.byId(row.model)?.name ?? row.model,
              selected: row.tier == active,
              onTap: () {
                choose(row);
                Navigator.pop(sheetContext);
              },
            ),
          ?_catalogueRow(
            t,
            i18n,
            ref,
            onTap: () {
              Navigator.pop(sheetContext);
              if (!context.mounted) return;
              showModelPicker(
                context,
                ref,
                sessionKey: sessionKey,
                currentModel: currentModel,
                currentVariant: currentVariant,
              );
            },
          ),
        ],
      ),
    ),
  );
}

/// Video tier picker (web `TierPicker` for video): each tier is a
/// (model, resolution) pair, picked in one tap.
Future<void> showVideoTierPicker(
  BuildContext context,
  WidgetRef ref, {
  required String sessionKey,
  required String? currentModel,
  required String? currentResolution,
}) async {
  final t = context.tokens;
  final i18n = ref.read(i18nProvider);
  final config = ref.read(appConfigProvider).valueOrNull;
  if (config == null || config.modelTiers.video.isEmpty) return;
  final picked = ref.read(pickedVideoProvider(sessionKey));
  final activeId = picked?.modelId ??
      (currentModel?.isNotEmpty == true ? currentModel! : config.defaultVideoModel);
  final activeResolution = picked?.resolution ??
      (currentResolution?.isNotEmpty == true
          ? currentResolution!
          : config.defaultVideoResolution);
  final active = activeVideoTier(config, activeId, activeResolution);

  void choose(String modelId, String resolution) {
    ref.read(pickedVideoProvider(sessionKey).notifier).state =
        VideoPick(modelId, resolution);
  }

  await showModalBottomSheet<void>(
    context: context,
    builder: (sheetContext) => SafeArea(
      child: ListView(
        shrinkWrap: true,
        padding: const EdgeInsets.symmetric(vertical: 8),
        children: [
          _tierHeading(t, i18n.t('chat:tier.video.pick')),
          for (final row in config.modelTiers.video)
            _videoTierRow(
              t,
              i18n,
              row,
              modelName: config.videoById(row.model)?.name ?? row.model,
              selected: row.tier == active,
              current: row.tier == active ? activeResolution : null,
              onTap: () async {
                Navigator.pop(sheetContext);
                // One resolution: nothing to choose, the tier is the pair.
                // Several: a second sheet with the price beside each, the
                // gesture people already know from the other pickers here.
                if (row.resolutions.length <= 1) {
                  choose(row.model, row.resolution);
                  return;
                }
                if (!context.mounted) return;
                await _showTierResolutionPicker(
                  context,
                  i18n: i18n,
                  row: row,
                  current: row.tier == active ? activeResolution : null,
                  onPick: (resolution) => choose(row.model, resolution),
                );
              },
            ),
          ?_catalogueRow(
            t,
            i18n,
            ref,
            onTap: () {
              Navigator.pop(sheetContext);
              if (!context.mounted) return;
              showVideoModelPicker(
                context,
                ref,
                sessionKey: sessionKey,
                currentModel: currentModel,
                currentResolution: currentResolution,
              );
            },
          ),
        ],
      ),
    ),
  );
}

Widget _videoTierRow(
  BossipTokens t,
  I18nState i18n,
  VideoTierRow row, {
  required String modelName,
  required bool selected,
  required String? current,
  required VoidCallback onTap,
}) {
  // Label, then what it is for, then what it resolves to — the reader
  // decides on the first two and can check the third.
  final detail = [
    if (row.description.isNotEmpty) row.description,
    modelName,
  ].join(' · ');
  return ListTile(
    dense: true,
    title: Text(videoTierLabel(i18n, row),
        style: TextStyle(
            fontSize: FontSizes.base,
            fontWeight: FontWeight.w500,
            color: t.ink)),
    subtitle: Text(detail, style: TextStyle(fontSize: FontSizes.xs, color: t.n500)),
    trailing: Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        if (selected && current != null)
          Text(current, style: TextStyle(fontSize: FontSizes.xs, color: t.n500)),
        if (selected) ...[
          const SizedBox(width: 6),
          Icon(Icons.check, size: 18, color: t.a700),
        ],
        if (row.resolutions.length > 1)
          Icon(Icons.chevron_right, size: 18, color: t.n500),
      ],
    ),
    onTap: onTap,
  );
}

/// Second step of the video tier picker: the tier's resolutions, each with
/// its per-second price from the same table the estimate bills against.
Future<void> _showTierResolutionPicker(
  BuildContext context, {
  required I18nState i18n,
  required VideoTierRow row,
  required String? current,
  required void Function(String) onPick,
}) {
  final t = context.tokens;
  return showModalBottomSheet<void>(
    context: context,
    builder: (sheetContext) => SafeArea(
      child: ListView(
        shrinkWrap: true,
        padding: const EdgeInsets.symmetric(vertical: 8),
        children: [
          _tierHeading(t, videoTierLabel(i18n, row)),
          for (final resolution in row.resolutions)
            ListTile(
              dense: true,
              title: Text(resolutionWithPrice(i18n, row, resolution),
                  style: TextStyle(fontSize: FontSizes.base, color: t.ink)),
              trailing: resolution == current
                  ? Icon(Icons.check, size: 18, color: t.a700)
                  : null,
              onTap: () {
                onPick(resolution);
                Navigator.pop(sheetContext);
              },
            ),
        ],
      ),
    ),
  );
}
