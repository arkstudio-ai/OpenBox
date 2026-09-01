import 'package:bossip_mobile/features/cron/utils/schedule.dart';
import 'package:bossip_mobile/shared/models/cron.dart';
import 'package:flutter_test/flutter_test.dart';

CronSchedule _roundTrip(Map<String, dynamic> wire, {String localTz = 'UTC'}) {
  final schedule = CronSchedule.fromJson(wire);
  final form = scheduleToForm(schedule);
  final rebuilt = buildSchedule(form, localTz);
  expect(rebuilt, isNotNull);
  return rebuilt!;
}

void main() {
  test('interval edit preserves its durable anchor', () {
    const wire = {
      'kind': 'every',
      'every_ms': 3600000,
      'anchor_ms': 1788134400000,
    };

    expect(_roundTrip(wire).toJson(), wire);
  });

  test(
    'cron edit preserves the stored timezone instead of device timezone',
    () {
      const wire = {
        'kind': 'cron',
        'expr': '30 9 * * *',
        'tz': 'America/New_York',
      };

      expect(_roundTrip(wire, localTz: 'Asia/Shanghai').toJson(), wire);
    },
  );

  test('one-shot edit remains a one-shot schedule', () {
    const wire = {'kind': 'at', 'at': '2026-09-01T01:02:03Z'};

    expect(_roundTrip(wire).toJson(), wire);
  });
}
