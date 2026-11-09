import 'dart:convert';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/providers.dart';
import '../utils/task_status.dart';

const _storageKey = 'openbox.taskCardFold';

/// A finished task updated within this long is "just done" and stays open by
/// default, so its result is seen once (web `stores/task-card-fold.ts`).
const freshWindow = Duration(minutes: 10);

/// Whether task cards are folded to one line. A display preference, not a
/// server fact: it lives on this device only (SharedPreferences), so
/// 不再跟进 stays the one server-side action and nothing syncs across devices.
///
/// Default: a finished card (done / failed / stopped / idle) folds itself once
/// it is no longer fresh; active and waiting cards stay open. A manual fold or
/// unfold wins over the default. [autoFold] false turns the default off (test
/// fixtures that read the full card).
class TaskCardFoldState {
  const TaskCardFoldState({
    this.overrides = const {},
    this.finishedGroupOpen = false,
    this.autoFold = true,
  });

  /// task id → folded (true) or opened (false) by hand.
  final Map<String, bool> overrides;

  /// Whether 我的任务's 最近完成 group shows its cards.
  final bool finishedGroupOpen;
  final bool autoFold;

  TaskCardFoldState copyWith({
    Map<String, bool>? overrides,
    bool? finishedGroupOpen,
    bool? autoFold,
  }) => TaskCardFoldState(
    overrides: overrides ?? this.overrides,
    finishedGroupOpen: finishedGroupOpen ?? this.finishedGroupOpen,
    autoFold: autoFold ?? this.autoFold,
  );

  bool isFolded(
    String taskId,
    TaskStatus status,
    DateTime? updatedAt, {
    DateTime? now,
  }) {
    final override = overrides[taskId];
    if (override != null) return override;
    return autoFold && defaultFolded(status, updatedAt, now: now);
  }
}

/// The default before any manual choice: finished and not fresh → folded.
bool defaultFolded(TaskStatus status, DateTime? updatedAt, {DateTime? now}) {
  if (isActiveTask(status)) return false;
  if (updatedAt != null &&
      (now ?? DateTime.now()).difference(updatedAt) < freshWindow) {
    return false;
  }
  return true;
}

class TaskCardFoldStore extends Notifier<TaskCardFoldState> {
  @override
  TaskCardFoldState build() {
    final raw = ref.read(prefsProvider).getString(_storageKey);
    if (raw == null) return const TaskCardFoldState();
    try {
      final json = jsonDecode(raw) as Map<String, dynamic>;
      final stored = json['overrides'];
      return TaskCardFoldState(
        overrides: {
          if (stored is Map)
            for (final entry in stored.entries)
              if (entry.value == 'folded' || entry.value == 'open')
                entry.key.toString(): entry.value == 'folded',
        },
        finishedGroupOpen: json['finishedGroupOpen'] == true,
      );
    } on FormatException {
      return const TaskCardFoldState();
    }
  }

  void setFold(String taskId, bool folded) {
    state = state.copyWith(overrides: {...state.overrides, taskId: folded});
    _save();
  }

  void setFinishedGroupOpen(bool open) {
    state = state.copyWith(finishedGroupOpen: open);
    _save();
  }

  void _save() {
    // Keep the newest entries only; a stale override for a long-gone task is harmless.
    final entries = state.overrides.entries.toList();
    final kept = entries.length > 400
        ? entries.sublist(entries.length - 400)
        : entries;
    ref
        .read(prefsProvider)
        .setString(
          _storageKey,
          jsonEncode({
            'overrides': {
              for (final e in kept) e.key: e.value ? 'folded' : 'open',
            },
            'finishedGroupOpen': state.finishedGroupOpen,
          }),
        );
  }
}

final taskCardFoldProvider =
    NotifierProvider<TaskCardFoldStore, TaskCardFoldState>(
      TaskCardFoldStore.new,
    );
