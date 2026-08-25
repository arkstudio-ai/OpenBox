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
  });

  factory ModelInfo.fromJson(Map<String, dynamic> json) => ModelInfo(
        id: asString(json['id']) ?? '',
        name: asString(json['name']) ?? asString(json['id']) ?? '',
        provider: asString(json['provider']),
        maxTokens: asInt(json['max_tokens']),
        contextLimit: asInt(json['context_limit']),
        vision: asBool(json['vision']) ?? false,
      );

  final String id;
  final String name;
  final String? provider;
  final int? maxTokens;
  final int? contextLimit;
  final bool vision;
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
