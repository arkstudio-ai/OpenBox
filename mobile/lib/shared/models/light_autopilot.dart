import 'json.dart';

/// One 省心托管 job (backend `light/autopilot.py`, `GET /api/light/autopilot`):
/// a Light result that keeps being made on a schedule.
class LightAutopilot {
  const LightAutopilot({
    required this.id,
    required this.task,
    required this.enabled,
    this.mode = 'make_only',
    this.sessionId,
    this.sourceAssetId,
    this.entryId,
    this.repeat = 'daily',
    this.time = '10:00',
    this.creditsCap = '10',
    this.shopName = '',
    this.nextRunAt,
    this.lastRunAt,
    this.lastStatus,
    this.totalRuns = 0,
    this.running = false,
  });

  static LightAutopilot? fromJson(dynamic json) {
    if (json is! Map<String, dynamic>) return null;
    final id = asString(json['id']);
    if (id == null || id.isEmpty) return null;
    return LightAutopilot(
      id: id,
      task: asString(json['task']) ?? 'video',
      enabled: json['enabled'] == true,
      mode: asString(json['mode']) ?? 'make_only',
      sessionId: asString(json['session_id']),
      sourceAssetId: asString(json['source_asset_id']),
      entryId: asString(json['entry_id']),
      repeat: asString(json['repeat']) ?? 'daily',
      time: asString(json['time']) ?? '10:00',
      creditsCap: asString(json['credits_cap_per_run']) ?? '10',
      shopName: asString(json['shop_name']) ?? '',
      nextRunAt: DateTime.tryParse(
        asString(json['next_run_at']) ?? '',
      )?.toLocal(),
      lastRunAt: DateTime.tryParse(
        asString(json['last_run_at']) ?? '',
      )?.toLocal(),
      lastStatus: asString(json['last_status']),
      totalRuns: (json['total_runs'] as num?)?.toInt() ?? 0,
      running: json['running'] == true,
    );
  }

  final String id;

  /// video | image | listing | reviews | report
  final String task;
  final bool enabled;

  /// make_only | make_and_publish
  final String mode;
  final String? sessionId;
  final String? sourceAssetId;
  final String? entryId;

  /// daily | weekdays | weekly
  final String repeat;

  /// HH:MM, Beijing time.
  final String time;
  final String creditsCap;
  final String shopName;
  final DateTime? nextRunAt;
  final DateTime? lastRunAt;
  final String? lastStatus;
  final int totalRuns;
  final bool running;
}

/// The three answers behind the switch (plan §4.3: 什么时候 / 怎么做 / 每次最多).
/// Defaults are the 2026-10-31 decision: every day 10:00, make only, ≤ 10.
class LightAutopilotChoice {
  const LightAutopilotChoice({
    this.repeat = 'daily',
    this.time = '10:00',
    this.mode = 'make_only',
    this.creditsCap = '10',
  });

  final String repeat;
  final String time;
  final String mode;
  final String creditsCap;

  LightAutopilotChoice copyWith({
    String? repeat,
    String? time,
    String? mode,
    String? creditsCap,
  }) => LightAutopilotChoice(
    repeat: repeat ?? this.repeat,
    time: time ?? this.time,
    mode: mode ?? this.mode,
    creditsCap: creditsCap ?? this.creditsCap,
  );
}
