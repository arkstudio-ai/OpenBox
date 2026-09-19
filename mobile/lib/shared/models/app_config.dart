import 'json.dart';

/// Mirrors `ModelInfo` / `AppConfig` (`GET /api/agent/config`,
/// frontend-v2 `shared/types/api.ts:228-248`).
class ModelInfo {
  const ModelInfo({
    required this.id,
    required this.name,
    this.provider,
    this.maxTokens,
    this.contextLimit,
    this.vision = false,
    this.variants = const [],
    this.defaultVariant,
    this.compaction,
  });

  factory ModelInfo.fromJson(Map<String, dynamic> json) => ModelInfo(
    id: asString(json['id']) ?? '',
    name: asString(json['name']) ?? asString(json['id']) ?? '',
    provider: asString(json['provider']),
    maxTokens: asInt(json['max_tokens']),
    contextLimit: asInt(json['context_limit']),
    vision: asBool(json['vision']) ?? false,
    variants: [for (final v in asList(json['variants'])) ?asString(v)],
    defaultVariant: asString(json['default_variant']),
    compaction: json['compaction'] is Map<String, dynamic>
        ? ContextCompaction.fromJson(json['compaction'] as Map<String, dynamic>)
        : null,
  );

  final String id;
  final String name;
  final String? provider;
  final int? maxTokens;
  final int? contextLimit;
  final bool vision;

  /// Reasoning strengths this model accepts, in display order. Empty means
  /// the route owns the effort and the picker stays hidden.
  final List<String> variants;

  /// Effective strength when the conversation does not override it.
  final String? defaultVariant;
  final ContextCompaction? compaction;
}

/// Server-resolved ceilings use the same output/reasoning reserve as the loop.
class ContextCompaction {
  const ContextCompaction({
    required this.enabled,
    this.threshold,
    this.variants = const {},
  });

  factory ContextCompaction.fromJson(Map<String, dynamic> json) =>
      ContextCompaction(
        enabled: asBool(json['enabled']) ?? false,
        threshold: asInt(json['threshold']),
        variants: {
          for (final entry in asMap(json['variants']).entries)
            if (asInt(entry.value) case final int value) entry.key: value,
        },
      );

  final bool enabled;
  final int? threshold;
  final Map<String, int> variants;

  int? thresholdFor(String? variant) {
    if (!enabled) return null;
    final value = variants[variant] ?? threshold;
    return value != null && value > 0 ? value : null;
  }
}

/// A video model the composer can generate with (web `VideoModelInfo`).
///
/// The tiers are per model and not interchangeable: one renders 480p only,
/// another names its tiers 768P and 2K. The backend refuses a mismatch, so
/// the picker offers each model only what that model published.
class VideoModelInfo {
  const VideoModelInfo({
    required this.id,
    required this.name,
    this.tier,
    this.resolutions = const [],
  });

  factory VideoModelInfo.fromJson(Map<String, dynamic> json) => VideoModelInfo(
        id: asString(json['id']) ?? '',
        name: asString(json['name']) ?? asString(json['id']) ?? '',
        tier: asString(json['tier']),
        resolutions: [
          for (final r in asList(json['resolutions'])) ?asString(r),
        ],
      );

  final String id;
  final String name;

  /// Free-text price tier, so an expensive switch is visible before it happens.
  final String? tier;

  /// Resolution tiers this model offers, in display order.
  final List<String> resolutions;
}

/// One chat tier (web `ChatTierRow`): the deployment resolves it to a model
/// and a reasoning strength, and the picker shows only the tier.
class ChatTierRow {
  const ChatTierRow({required this.tier, required this.model, this.variant});

  factory ChatTierRow.fromJson(Map<String, dynamic> json) => ChatTierRow(
        tier: asString(json['tier']) ?? '',
        model: asString(json['model']) ?? '',
        variant: asString(json['variant']),
      );

  /// `high` | `medium` | `low`.
  final String tier;
  final String model;

  /// Strength sent with the tier; null keeps the model default.
  final String? variant;
}

/// One video tier (web `VideoTierRow`): the (model, resolution) pair that
/// decides the price.
class VideoTierRow {
  const VideoTierRow({
    required this.tier,
    required this.model,
    required this.resolution,
  });

  factory VideoTierRow.fromJson(Map<String, dynamic> json) => VideoTierRow(
        tier: asString(json['tier']) ?? '',
        model: asString(json['model']) ?? '',
        resolution: asString(json['resolution']) ?? '',
      );

  final String tier;
  final String model;
  final String resolution;
}

/// Tier presets (web `ModelTiers`). Empty lists mean the deployment shows
/// the full pickers.
class ModelTiers {
  const ModelTiers({this.chat = const [], this.video = const []});

  factory ModelTiers.fromJson(Map<String, dynamic> json) => ModelTiers(
        chat: asList(json['chat'])
            .whereType<Map<String, dynamic>>()
            .map(ChatTierRow.fromJson)
            .where((row) => row.tier.isNotEmpty && row.model.isNotEmpty)
            .toList(),
        video: asList(json['video'])
            .whereType<Map<String, dynamic>>()
            .map(VideoTierRow.fromJson)
            .where((row) => row.tier.isNotEmpty && row.model.isNotEmpty)
            .toList(),
      );

  final List<ChatTierRow> chat;
  final List<VideoTierRow> video;
}

class AppConfig {
  const AppConfig({
    required this.models,
    this.videoModels = const [],
    this.defaultModel = '',
    this.defaultVideoModel = '',
    this.defaultVideoResolution = '',
    this.defaultAgent = 'build',
    this.modelTiers = const ModelTiers(),
  });

  factory AppConfig.fromJson(Map<String, dynamic> json) => AppConfig(
        models: asList(json['models'])
            .whereType<Map<String, dynamic>>()
            .map(ModelInfo.fromJson)
            .toList(),
        videoModels: asList(json['video_models'])
            .whereType<Map<String, dynamic>>()
            .map(VideoModelInfo.fromJson)
            .toList(),
        defaultModel: asString(json['default_model']) ?? '',
        defaultVideoModel: asString(json['default_video_model']) ?? '',
        defaultVideoResolution: asString(json['default_video_resolution']) ?? '',
        defaultAgent: asString(json['default_agent']) ?? 'build',
        modelTiers: json['model_tiers'] is Map<String, dynamic>
            ? ModelTiers.fromJson(json['model_tiers'] as Map<String, dynamic>)
            : const ModelTiers(),
      );

  final List<ModelInfo> models;
  final List<VideoModelInfo> videoModels;
  final String defaultModel;
  final String defaultVideoModel;
  final String defaultVideoResolution;
  final String defaultAgent;
  final ModelTiers modelTiers;

  VideoModelInfo? videoById(String id) {
    for (final m in videoModels) {
      if (m.id == id) return m;
    }
    return null;
  }

  ModelInfo? byId(String id) {
    for (final m in models) {
      if (m.id == id) return m;
    }
    return null;
  }
}

/// Mirrors `AgentInfo` (`GET /api/agent/agent`).
class AgentInfo {
  const AgentInfo({
    required this.name,
    this.description,
    this.model,
    this.mode,
    this.color,
  });

  factory AgentInfo.fromJson(Map<String, dynamic> json) => AgentInfo(
        name: asString(json['name']) ?? '',
        description: asString(json['description']),
        model: asString(json['model']),
        mode: asString(json['mode']),
        color: asString(json['color']),
      );

  final String name;
  final String? description;
  final String? model;
  final String? mode;
  final String? color;
}
