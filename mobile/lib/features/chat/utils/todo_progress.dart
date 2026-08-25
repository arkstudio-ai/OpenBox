/// The running task's bar — a 1:1 port of web `lib/todo-progress.ts`.
///
/// There is no honest number here: the bar is a fiction, but a DETERMINISTIC
/// one, computed from when the task started and how many calls it finished —
/// never from a timer inside the widget — so a reload picks it up exactly
/// where it was.
library;

import 'dart:math' as math;

/// Never reaches the top: a full bar on an unfinished task reads as a stall.
const _ceiling = 0.9;

/// Where the bar sits the moment a task starts, so it is visible at once.
const _floor = 0.06;

/// Seconds for the time-based half to run its course.
const _pace = 90.0;

/// Each finished call is worth this much, up to [_stepMax].
const _stepWeight = 0.08;
const _stepMax = 0.45;

/// Fraction filled, 0…1. A completed task is 1; nothing else ever is.
double taskProgress({
  required DateTime? startedAt,
  required int steps,
  required DateTime now,
}) {
  if (startedAt == null) return 0;
  final elapsed =
      math.max(0, now.difference(startedAt).inMilliseconds / 1000.0);

  // Asymptotic in elapsed time: quick at first, then slower, never arriving.
  // Work done pushes it along faster than waiting does.
  final byTime = 1 - math.exp(-elapsed / _pace);
  final byWork = math.min(_stepMax, steps * _stepWeight);
  final combined =
      _floor + (_ceiling - _floor) * (byTime * 0.6 + byWork / _stepMax * 0.4);
  return math.min(_ceiling, combined);
}

int progressPercent(double value) => (value * 100).round();
