/// Pure schedule helpers — a 1:1 port of frontend-v2
/// `features/cron/utils/schedule.ts` + `constants/index.ts`: build wire
/// schedules from the form model, parse them back for editing, and describe
/// them for display. No Flutter, no I/O — callers pass `t`.
library;

import '../../../shared/models/cron.dart';

typedef Translate = String Function(String key,
    {Map<String, Object?>? vars, int? count});

const scheduleModes = ['daily', 'weekly', 'interval', 'custom'];

const scheduleModeKeys = {
  'daily': 'cron:form.mode.daily',
  'weekly': 'cron:form.mode.weekly',
  'interval': 'cron:form.mode.interval',
  'custom': 'cron:form.mode.custom',
};

const intervalUnits = ['minutes', 'hours'];

const intervalUnitKeys = {
  'minutes': 'cron:form.unitOptions.minutes',
  'hours': 'cron:form.unitOptions.hours',
};

/// 0 = Sunday, matching cron day-of-week numbering.
const weekdayKeys = {
  0: 'cron:weekday.sun',
  1: 'cron:weekday.mon',
  2: 'cron:weekday.tue',
  3: 'cron:weekday.wed',
  4: 'cron:weekday.thu',
  5: 'cron:weekday.fri',
  6: 'cron:weekday.sat',
};

const runStatusKeys = {
  'ok': 'cron:run.status.ok',
  'error': 'cron:run.status.error',
  'skipped': 'cron:run.status.skipped',
  'running': 'cron:run.status.running',
};

/// Backend's silence sentinel: such runs are recorded but never injected.
const silentSentinel = 'NO_REPLY';

/// Mirrors backend cron_min_interval_seconds (5 minutes).
const minIntervalMinutes = 5;

class ScheduleForm {
  const ScheduleForm({
    this.mode = 'daily',
    this.time = '09:00',
    this.weekday = 1,
    this.every = 30,
    this.unit = 'minutes',
    this.expr = '0 9 * * *',
  });

  final String mode;

  /// "HH:mm" for daily/weekly.
  final String time;

  /// 0-6, Sunday=0, for weekly.
  final int weekday;
  final int every;
  final String unit;
  final String expr;

  ScheduleForm copyWith({
    String? mode,
    String? time,
    int? weekday,
    int? every,
    String? unit,
    String? expr,
  }) =>
      ScheduleForm(
        mode: mode ?? this.mode,
        time: time ?? this.time,
        weekday: weekday ?? this.weekday,
        every: every ?? this.every,
        unit: unit ?? this.unit,
        expr: expr ?? this.expr,
      );
}

int intervalMs(int every, String unit) =>
    unit == 'hours' ? every * 3600000 : every * 60000;

({int h, int m})? _parseTime(String time) {
  final match = RegExp(r'^(\d{1,2}):(\d{2})$').firstMatch(time);
  if (match == null) return null;
  final h = int.parse(match.group(1)!);
  final m = int.parse(match.group(2)!);
  if (h < 0 || h > 23 || m < 0 || m > 59) return null;
  return (h: h, m: m);
}

/// null means the form is invalid (empty expr, bad time, sub-min interval).
CronSchedule? buildSchedule(ScheduleForm form, String tz) {
  if (form.mode == 'interval') {
    if (form.every <= 0) return null;
    if (intervalMs(form.every, form.unit) < minIntervalMinutes * 60000) {
      return null;
    }
    return CronScheduleEvery(everyMs: intervalMs(form.every, form.unit));
  }
  if (form.mode == 'custom') {
    final expr = form.expr.trim();
    if (expr.split(RegExp(r'\s+')).length < 5) return null;
    return CronScheduleCron(expr: expr, tz: tz);
  }
  final time = _parseTime(form.time);
  if (time == null) return null;
  if (form.mode == 'weekly') {
    return CronScheduleCron(
        expr: '${time.m} ${time.h} * * ${form.weekday}', tz: tz);
  }
  return CronScheduleCron(expr: '${time.m} ${time.h} * * *', tz: tz);
}

final _dailyRe = RegExp(r'^(\d{1,2}) (\d{1,2}) \* \* \*$');
final _weeklyRe = RegExp(r'^(\d{1,2}) (\d{1,2}) \* \* ([0-6])$');

String _pad(int n) => n.toString().padLeft(2, '0');

/// Reconstruct the form model from a stored schedule (for the edit dialog).
ScheduleForm scheduleToForm(CronSchedule schedule) {
  switch (schedule) {
    case CronScheduleEvery(:final everyMs):
      if (everyMs % 3600000 == 0) {
        return ScheduleForm(
            mode: 'interval', every: everyMs ~/ 3600000, unit: 'hours');
      }
      return ScheduleForm(
          mode: 'interval',
          every: (everyMs / 60000).round(),
          unit: 'minutes');
    case CronScheduleCron(:final expr):
      final daily = _dailyRe.firstMatch(expr);
      if (daily != null) {
        return ScheduleForm(
          mode: 'daily',
          time:
              '${_pad(int.parse(daily.group(2)!))}:${_pad(int.parse(daily.group(1)!))}',
        );
      }
      final weekly = _weeklyRe.firstMatch(expr);
      if (weekly != null) {
        return ScheduleForm(
          mode: 'weekly',
          time:
              '${_pad(int.parse(weekly.group(2)!))}:${_pad(int.parse(weekly.group(1)!))}',
          weekday: int.parse(weekly.group(3)!),
        );
      }
      return ScheduleForm(mode: 'custom', expr: expr);
    case CronScheduleAt():
      return const ScheduleForm();
  }
}

/// Human-readable schedule line, localized through the caller's `t`.
String describeSchedule(CronSchedule schedule, Translate t) {
  switch (schedule) {
    case CronScheduleAt(:final at):
      return t('cron:describe.once', vars: {'time': at});
    case CronScheduleEvery(:final everyMs):
      if (everyMs % 3600000 == 0) {
        return t('cron:describe.everyHours', count: everyMs ~/ 3600000);
      }
      return t('cron:describe.everyMinutes', count: (everyMs / 60000).round());
    case CronScheduleCron(:final expr):
      final daily = _dailyRe.firstMatch(expr);
      if (daily != null) {
        return t('cron:describe.daily', vars: {
          'time':
              '${_pad(int.parse(daily.group(2)!))}:${_pad(int.parse(daily.group(1)!))}',
        });
      }
      final weekly = _weeklyRe.firstMatch(expr);
      if (weekly != null) {
        return t('cron:describe.weekly', vars: {
          'weekday': t(weekdayKeys[int.parse(weekly.group(3)!)]!),
          'time':
              '${_pad(int.parse(weekly.group(2)!))}:${_pad(int.parse(weekly.group(1)!))}',
        });
      }
      return t('cron:describe.cron', vars: {'expr': expr});
  }
}

/// A run whose result is the sentinel (or empty) was deliberately silent.
bool isSilentResult(String? summary) {
  if (summary == null) return true;
  final stripped = summary.trim();
  if (stripped.isEmpty) return true;
  final head = stripped
      .split('\n')
      .first
      .trim()
      .replaceAll(RegExp(r'^[*_`]+'), '')
      .replaceAll(RegExp(r'[*_`.。!!\s]+$'), '');
  return head == silentSentinel &&
      stripped.length <= silentSentinel.length + 8;
}
