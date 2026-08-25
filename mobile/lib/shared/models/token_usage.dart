import 'json.dart';

/// Mirrors `TokenUsage` (frontend-v2 `shared/types/api.ts:7-15`).
class TokenUsage {
  const TokenUsage({
    this.input = 0,
    this.output = 0,
    this.cache = 0,
    this.total = 0,
    this.limit = 0,
    this.cost = 0,
    this.context = 0,
  });

  factory TokenUsage.fromJson(Map<String, dynamic> json) => TokenUsage(
        input: asInt(json['input']) ?? 0,
        output: asInt(json['output']) ?? 0,
        cache: asInt(json['cache']) ?? 0,
        total: asInt(json['total']) ?? 0,
        limit: asInt(json['limit']) ?? 0,
        cost: asDouble(json['cost']) ?? 0,
        context: asInt(json['context']) ?? 0,
      );

  final int input;
  final int output;
  final int cache;
  final int total;
  final int limit;
  final double cost;
  final int context;
}
