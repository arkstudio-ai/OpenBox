/// Tolerant JSON accessors for wire payloads (backend is snake_case REST,
/// camelCase WS — see frontend-v2 `shared/types/api.ts`).
library;

int? asInt(dynamic v) => v is num ? v.toInt() : null;

double? asDouble(dynamic v) => v is num ? v.toDouble() : null;

String? asString(dynamic v) => v is String ? v : null;

bool? asBool(dynamic v) => v is bool ? v : null;

Map<String, dynamic> asMap(dynamic v) =>
    v is Map<String, dynamic> ? v : const <String, dynamic>{};

List<dynamic> asList(dynamic v) => v is List ? v : const <dynamic>[];

DateTime? asDate(dynamic v) => v is String ? DateTime.tryParse(v) : null;
