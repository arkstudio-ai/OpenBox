import '../../../shared/utils/format.dart';

/// One plain-words status for a task the personal assistant follows, the way
/// a secretary would put it: is it waiting on you, still going, or done
/// (web `lib/task-status.ts`). The detailed task/run/delivery states stay on
/// the server; the page shows this.
enum TaskStatus {
  waiting,
  running,
  queued,
  paused,
  done,
  failed,
  stopped,
  idle,
}

/// How a status reads at a glance (web `StatusTone`).
enum TaskTone { ok, warn, danger, muted, accent }

const _live = {'busy', 'finalizing', 'retry', 'compacting'};
const _trouble = {'error', 'resume_blocked', 'effect_unknown'};

/// States that win over whatever the run is doing: stopped, waiting on you,
/// paused.
TaskStatus? _held({
  String? sessionStatus,
  String? observedState,
  String? desiredState,
  int pendingQuestions = 0,
}) {
  if (desiredState == 'canceled' ||
      observedState == 'canceled' ||
      observedState == 'canceling') {
    return TaskStatus.stopped;
  }
  if (pendingQuestions > 0 ||
      sessionStatus == 'waiting_input' ||
      observedState == 'waiting_input') {
    return TaskStatus.waiting;
  }
  if (desiredState == 'paused' ||
      observedState == 'paused' ||
      observedState == 'pausing') {
    return TaskStatus.paused;
  }
  return null;
}

/// A live run or queue position, read from the conversation and the task
/// record.
TaskStatus? _inMotion({String? sessionStatus, String? observedState}) {
  if (sessionStatus != null && _live.contains(sessionStatus)) {
    return TaskStatus.running;
  }
  if (observedState == 'running' || observedState == 'resuming') {
    return TaskStatus.running;
  }
  if (sessionStatus == 'queued' || observedState == 'queued') {
    return TaskStatus.queued;
  }
  return null;
}

TaskStatus taskStatus({
  String? sessionStatus,
  String? observedState,
  String? desiredState,
  int pendingQuestions = 0,
  String? outcome,
}) {
  final held =
      _held(
        sessionStatus: sessionStatus,
        observedState: observedState,
        desiredState: desiredState,
        pendingQuestions: pendingQuestions,
      ) ??
      _inMotion(sessionStatus: sessionStatus, observedState: observedState);
  if (held != null) return held;
  if (sessionStatus == 'error' ||
      (observedState != null && _trouble.contains(observedState))) {
    return TaskStatus.failed;
  }
  if (observedState == 'aborted') return TaskStatus.stopped;
  if (observedState == 'completed') {
    return outcome == 'error' ? TaskStatus.failed : TaskStatus.done;
  }
  return switch (outcome) {
    'succeeded' => TaskStatus.done,
    'error' => TaskStatus.failed,
    'aborted' => TaskStatus.stopped,
    _ => TaskStatus.idle,
  };
}

/// Still something to wait for or act on.
bool isActiveTask(TaskStatus status) =>
    status == TaskStatus.waiting ||
    status == TaskStatus.running ||
    status == TaskStatus.queued ||
    status == TaskStatus.paused;

TaskTone taskTone(TaskStatus status) => switch (status) {
  TaskStatus.waiting => TaskTone.warn,
  TaskStatus.running => TaskTone.accent,
  TaskStatus.done => TaskTone.ok,
  TaskStatus.failed => TaskTone.danger,
  TaskStatus.queued ||
  TaskStatus.paused ||
  TaskStatus.stopped ||
  TaskStatus.idle => TaskTone.muted,
};

const _monthsEn = [
  'Jan',
  'Feb',
  'Mar',
  'Apr',
  'May',
  'Jun',
  'Jul',
  'Aug',
  'Sep',
  'Oct',
  'Nov',
  'Dec',
];

/// A short date in the active language: "10月7日" / "Oct 7", with the year
/// when asked for. Spelled out rather than through `intl` locale data, which
/// the app does not load for every language.
String shortDate(DateTime at, String language, {bool year = false}) {
  if (language.startsWith('zh')) {
    return year
        ? '${at.year}年${at.month}月${at.day}日'
        : '${at.month}月${at.day}日';
  }
  final day = '${_monthsEn[at.month - 1]} ${at.day}';
  return year ? '$day, ${at.year}' : day;
}

/// "3 小时前" within a week, then a short date (web `sinceLabel`).
String sinceLabel(DateTime? when, String language, {DateTime? now}) {
  if (when == null) return '';
  final at = when.toLocal();
  final current = now ?? DateTime.now();
  if (at.difference(current).abs() < const Duration(days: 7)) {
    return formatRelative(at, language, now: current);
  }
  return shortDate(at, language, year: at.year != current.year);
}

/// A task's latest reply as plain words for a card preview: markdown marks
/// (emphasis, code, links, headings, quotes, tables) dropped, text kept
/// (web `plainSummary`).
String plainSummary(String text) {
  String all(String value, String pattern, String Function(Match) to) =>
      value.replaceAllMapped(RegExp(pattern, multiLine: true), to);
  var value = text;
  value = all(value, r'```[^\n]*\n?', (_) => '');
  value = all(value, r'!\[([^\]]*)\]\([^)]*\)', (m) => m[1]!);
  value = all(value, r'\[([^\]]+)\]\([^)]*\)', (m) => m[1]!);
  value = all(value, r'`([^`\n]+)`', (m) => m[1]!);
  value = all(value, r'(\*\*|__)(.+?)\1', (m) => m[2]!);
  value = all(
    value,
    r'(^|[\s(（])\*(?!\s)([^*\n]+?)\*(?=[\s).,;:!?，。；：！？）]|$)',
    (m) => '${m[1]}${m[2]}',
  );
  value = all(
    value,
    r'(^|[\s(（])_(?!\s)([^_\n]+?)_(?=[\s).,;:!?，。；：！？）]|$)',
    (m) => '${m[1]}${m[2]}',
  );
  value = all(value, r'^[ \t]{0,3}#{1,6}[ \t]+', (_) => '');
  value = all(value, r'^[ \t]{0,3}>[ \t]?', (_) => '');
  value = all(
    value,
    r'^[ \t]*\|?[ \t]*:?-{3,}:?[ \t]*(\|[ \t]*:?-{3,}:?[ \t]*)*\|?[ \t]*$',
    (_) => '',
  );
  value = all(
    value,
    r'^[ \t]*\|(.*)\|[ \t]*$',
    (m) => m[1]!
        .split('|')
        .map((cell) => cell.trim())
        .where((cell) => cell.isNotEmpty)
        .join(' · '),
  );
  value = all(value, r'^[ \t]*[-*+][ \t]+', (_) => '• ');
  value = all(value, r'\n{3,}', (_) => '\n\n');
  return value.trim();
}
