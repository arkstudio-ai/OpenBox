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
  });

  factory ModelInfo.fromJson(Map<String, dynamic> json) => ModelInfo(
        id: asString(json['id']) ?? '',
        name: asString(json['name']) ?? asString(json['id']) ?? '',
        provider: asString(json['provider']),
        maxTokens: asInt(json['max_tokens']),
        contextLimit: asInt(json['context_limit']),
        vision: asBool(json['vision']) ?? false,
        variants: [
          for (final v in asList(json['variants'])) ?asString(v),
        ],
        defaultVariant: asString(json['default_variant']),
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

class AppConfig {
  const AppConfig({
    required this.models,
    this.videoModels = const [],
    this.defaultModel = '',
    this.defaultVideoModel = '',
    this.defaultVideoResolution = '',
    this.defaultAgent = 'build',
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
      );

  final List<ModelInfo> models;
  final List<VideoModelInfo> videoModels;
  final String defaultModel;
  final String defaultVideoModel;
  final String defaultVideoResolution;
  final String defaultAgent;

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
