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

class AppConfig {
  const AppConfig({
    required this.models,
    this.defaultModel = '',
    this.defaultAgent = 'build',
  });

  factory AppConfig.fromJson(Map<String, dynamic> json) => AppConfig(
        models: asList(json['models'])
            .whereType<Map<String, dynamic>>()
            .map(ModelInfo.fromJson)
            .toList(),
        defaultModel: asString(json['default_model']) ?? '',
        defaultAgent: asString(json['default_agent']) ?? 'build',
      );

  final List<ModelInfo> models;
  final String defaultModel;
  final String defaultAgent;

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
