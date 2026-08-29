import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../../shared/appearance/tokens.dart';
import '../../../../shared/appearance/type_scale.dart';
import '../../../../shared/i18n/i18n.dart';
import '../../../../shared/models/message_part.dart';
import '../../../../shared/models/todo.dart';
import '../../../../shared/utils/format.dart';
import '../../../../shared/widgets/fold.dart';
import '../../../../shared/widgets/shimmer_text.dart';
import '../../api/chat_api.dart';
import '../../utils/todo_progress.dart';
import '../../utils/tool_map.dart';
import '../../utils/turn_view.dart';
import '../traces/subagent_line.dart';
import '../traces/tool_chain_trace.dart' show ToolDetailBox;

/// The task card (web `TodoCard.tsx`): the model's todo list with each
/// task's own calls folded underneath it. Replaces the flat tool chain for
/// turns that kept a list; loose calls still render as a chain outside.
class TodoCard extends ConsumerStatefulWidget {
  const TodoCard({
    super.key,
    required this.todo,
    required this.sessionId,
    required this.streaming,
    this.onStop,
    this.editable = true,
  });

  final TodoView todo;
  final String sessionId;

  /// The turn is live — the card offers to stop it and keeps its bar moving.
  final bool streaming;
  final VoidCallback? onStop;

  /// Only the conversation's newest card takes edits.
  final bool editable;

  @override
  ConsumerState<TodoCard> createState() => _TodoCardState();
}

class _TodoCardState extends ConsumerState<TodoCard> {
  Timer? _tick;
  DateTime _now = DateTime.now();

  // Once every task is done the card is a record, not a control: it folds
  // to its heading (latched, so it doesn't snap under the user).
  late bool _open = !widget.todo.allDone;
  late bool _wasDone = widget.todo.allDone;

  String? _adding; // afterId, or 'end' for the tail
  final _draft = TextEditingController();

  @override
  void initState() {
    super.initState();
    _syncTick();
  }

  @override
  void didUpdateWidget(TodoCard oldWidget) {
    super.didUpdateWidget(oldWidget);
    _syncTick();
    if (_wasDone != widget.todo.allDone) {
      _wasDone = widget.todo.allDone;
      _open = !widget.todo.allDone;
    }
  }

  void _syncTick() {
    final active =
        todoDisposition(widget.todo, widget.streaming).isLive &&
            !widget.todo.allDone;
    if (active && _tick == null) {
      _tick = Timer.periodic(const Duration(seconds: 1), (_) {
        setState(() => _now = DateTime.now());
      });
    } else if (!active && _tick != null) {
      _tick?.cancel();
      _tick = null;
    }
  }

  @override
  void dispose() {
    _tick?.cancel();
    _draft.dispose();
    super.dispose();
  }

  Future<void> _submitAdd() async {
    final subject = _draft.text.trim();
    final afterId = _adding;
    setState(() {
      _adding = null;
      _draft.clear();
    });
    if (subject.isEmpty || afterId == null) return;
    await ref.read(chatApiProvider).addTodoItem(
          widget.sessionId,
          subject,
          afterId: afterId == 'end' ? null : afterId,
        );
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final todo = widget.todo;
    // `streaming` is "the turn that wrote this card is still running" — never
    // "the session is busy". An unrelated later turn makes the session busy
    // again, and keying off that relit every old card in the conversation.
    final disposition = todoDisposition(todo, widget.streaming);
    final live = disposition.isLive;
    // A settled list takes no edits: adding a task to a plan nothing is
    // working through only splits the stored list from this snapshot.
    final editable =
        widget.editable && live && (!todo.allDone || _adding != null);
    final heading = live
        ? (todo.activeForm ?? i18n.t('chat:todo.working'))
        : i18n.t('chat:todo.title');

    return Container(
      margin: const EdgeInsets.only(bottom: 8),
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: t.card,
        borderRadius: BorderRadius.circular(Radii.xl),
        border: Border.all(color: t.hair),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            children: [
              Expanded(
                child: InkWell(
                  onTap: () => setState(() => _open = !_open),
                  child: Row(
                    children: [
                      Flexible(
                        child: live && !todo.allDone
                            ? ShimmerText(
                                heading,
                                style: const TextStyle(
                                  fontSize: FontSizes.base,
                                  fontWeight: FontWeight.w500,
                                ),
                              )
                            : Text(
                                heading,
                                maxLines: 1,
                                overflow: TextOverflow.ellipsis,
                                style: TextStyle(
                                  fontSize: FontSizes.base,
                                  fontWeight: FontWeight.w500,
                                  color: t.ink,
                                ),
                              ),
                      ),
                      if (todo.total > 0) ...[
                        const SizedBox(width: 10),
                        Text(
                          switch (disposition.kind) {
                            TodoDispositionKind.done => i18n.t(
                                'chat:todo.allDone',
                                vars: {'total': todo.total}),
                            TodoDispositionKind.interrupted => i18n.t(
                                'chat:todo.interruptedAt', vars: {
                                'done': todo.done,
                                'total': todo.total,
                                'at': disposition.at,
                              }),
                            TodoDispositionKind.unfinished => i18n.t(
                                'chat:todo.unfinished', vars: {
                                'done': todo.done,
                                'total': todo.total,
                              }),
                            TodoDispositionKind.live =>
                              i18n.t('chat:plan.stepCounter', vars: {
                                  'current':
                                      todo.current < 1 ? 1 : todo.current,
                                  'total': todo.total,
                                }),
                          },
                          style: TextStyle(
                              fontSize: FontSizes.sm, color: t.n600),
                        ),
                      ],
                      const SizedBox(width: 6),
                      AnimatedRotation(
                        turns: _open ? 0.5 : 0,
                        duration: const Duration(milliseconds: 200),
                        child:
                            Icon(Icons.expand_more, size: 15, color: t.n500),
                      ),
                    ],
                  ),
                ),
              ),
              if (live && widget.onStop != null)
                GestureDetector(
                  onTap: widget.onStop,
                  child: Padding(
                    padding: const EdgeInsets.only(left: 10),
                    child: Text(
                      i18n.t('chat:plan.stop'),
                      style:
                          TextStyle(fontSize: FontSizes.sm, color: t.a700),
                    ),
                  ),
                ),
            ],
          ),
          Fold(
            open: _open,
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                const SizedBox(height: 8),
                for (final task in todo.tasks)
                  _TaskRow(
                    task: task,
                    now: _now,
                    live: live,
                    editable: editable,
                    onAdd: (id) => setState(() {
                      _adding = id;
                      _draft.clear();
                    }),
                    onRemove: (id) => ref
                        .read(chatApiProvider)
                        .removeTodoItem(widget.sessionId, id),
                  ),
                if (editable)
                  Padding(
                    padding: const EdgeInsets.only(top: 4),
                    child: _adding != null
                        ? TextField(
                            controller: _draft,
                            autofocus: true,
                            onSubmitted: (_) => _submitAdd(),
                            onTapOutside: (_) => _submitAdd(),
                            style: TextStyle(
                                fontSize: FontSizes.base, color: t.ink),
                            decoration: InputDecoration(
                              hintText: i18n.t('chat:todo.addPlaceholder'),
                              hintStyle: TextStyle(
                                  fontSize: FontSizes.base, color: t.n500),
                              isDense: true,
                              filled: true,
                              fillColor: t.bg,
                              contentPadding: const EdgeInsets.symmetric(
                                  horizontal: 10, vertical: 8),
                              enabledBorder: OutlineInputBorder(
                                borderRadius:
                                    BorderRadius.circular(Radii.md),
                                borderSide: BorderSide(color: t.hair),
                              ),
                              focusedBorder: OutlineInputBorder(
                                borderRadius:
                                    BorderRadius.circular(Radii.md),
                                borderSide: BorderSide(color: t.accent),
                              ),
                            ),
                          )
                        : Align(
                            alignment: Alignment.centerLeft,
                            child: GestureDetector(
                              onTap: () => setState(() {
                                _adding = 'end';
                                _draft.clear();
                              }),
                              child: Text(
                                i18n.t('chat:plan.addStep'),
                                style: TextStyle(
                                    fontSize: FontSizes.sm, color: t.a700),
                              ),
                            ),
                          ),
                  ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

/// Status circle (web `StatusMark`): filled sage check when done, thick
/// accent ring while running, muted ring otherwise.
class _StatusMark extends StatelessWidget {
  const _StatusMark({required this.item, required this.live});

  final TodoItem item;

  /// The turn is still running. On a settled card the in_progress flag only
  /// records where work stopped, and must not wear the running ring.
  final bool live;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final done = item.status == TodoStatus.completed;
    final running = live && item.status == TodoStatus.inProgress;
    final cancelled = item.status == TodoStatus.cancelled;
    return Opacity(
      opacity: cancelled ? 0.6 : 1,
      child: Container(
        width: 18,
        height: 18,
        decoration: BoxDecoration(
          shape: BoxShape.circle,
          color: done ? t.s600 : null,
          border: done
              ? null
              : Border.all(color: running ? t.accent : t.n400, width: 2.5),
        ),
        child: done
            ? Icon(Icons.check, size: 12, color: t.bg)
            : null,
      ),
    );
  }
}

class _TaskRow extends ConsumerStatefulWidget {
  const _TaskRow({
    required this.task,
    required this.now,
    required this.live,
    required this.editable,
    required this.onAdd,
    required this.onRemove,
  });

  final TodoTask task;
  final DateTime now;

  /// See _StatusMark.live — the turn that wrote this row is still running.
  final bool live;
  final bool editable;
  final void Function(String afterId) onAdd;
  final void Function(String id) onRemove;

  @override
  ConsumerState<_TaskRow> createState() => _TaskRowState();
}

class _TaskRowState extends ConsumerState<_TaskRow> {
  // A finished task folds its work away; the running one stays open so the
  // calls stream where they happen. Latched after that.
  late bool _open =
      widget.live && widget.task.item.status == TodoStatus.inProgress;
  late bool _wasRunning =
      widget.live && widget.task.item.status == TodoStatus.inProgress;

  @override
  void didUpdateWidget(_TaskRow oldWidget) {
    super.didUpdateWidget(oldWidget);
    final running =
        widget.live && widget.task.item.status == TodoStatus.inProgress;
    if (_wasRunning != running) {
      _wasRunning = running;
      if (running) _open = true;
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final item = widget.task.item;
    final tools = widget.task.tools;
    final running = item.status == TodoStatus.inProgress;
    final done = item.status == TodoStatus.completed;
    final cancelled = item.status == TodoStatus.cancelled;

    final finishedCalls = tools
        .where((p) =>
            p is! ToolPart ||
            (p.status != ToolStatus.running && p.status != ToolStatus.pending))
        .length;
    final percent = done
        ? 100
        : running
            ? progressPercent(taskProgress(
                startedAt: item.startedAt,
                steps: finishedCalls,
                now: widget.now,
              ))
            : 0;

    final title = running &&
            item.activeForm != null &&
            item.activeForm!.trim().isNotEmpty
        ? item.activeForm!
        : item.subject;

    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        InkWell(
          onTap: tools.isEmpty ? null : () => setState(() => _open = !_open),
          child: Padding(
            padding: const EdgeInsets.symmetric(vertical: 5),
            child: Row(
              children: [
                _StatusMark(item: item, live: widget.live),
                const SizedBox(width: 11),
                Expanded(
                  child: Text(
                    title,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(
                      fontSize: FontSizes.base,
                      fontWeight: running ? FontWeight.w500 : FontWeight.w400,
                      color: running
                          ? t.ink
                          : done
                              ? t.n700
                              : t.n500,
                      decoration:
                          cancelled ? TextDecoration.lineThrough : null,
                    ),
                  ),
                ),
                if (tools.isNotEmpty)
                  AnimatedRotation(
                    turns: _open ? 0.5 : 0,
                    duration: const Duration(milliseconds: 200),
                    child: Icon(Icons.expand_more, size: 14, color: t.n500),
                  ),
                if (running) ...[
                  const SizedBox(width: 6),
                  Text(
                    '$percent%',
                    style: TextStyle(fontSize: FontSizes.sm, color: t.n600),
                  ),
                ],
                if (widget.editable && !running && !done) ...[
                  const SizedBox(width: 4),
                  InkWell(
                    onTap: () => widget.onAdd(item.id),
                    child: Padding(
                      padding: const EdgeInsets.all(4),
                      child: Icon(Icons.add, size: 15, color: t.n500),
                    ),
                  ),
                  if (!cancelled)
                    InkWell(
                      onTap: () => widget.onRemove(item.id),
                      child: Padding(
                        padding: const EdgeInsets.all(4),
                        child: Icon(Icons.close, size: 15, color: t.n500),
                      ),
                    ),
                ],
              ],
            ),
          ),
        ),
        if (running)
          Padding(
            padding: const EdgeInsets.only(top: 2, bottom: 6),
            child: ClipRRect(
              borderRadius: BorderRadius.circular(Radii.full),
              child: SizedBox(
                height: 4,
                child: Stack(
                  children: [
                    ColoredBox(
                        color: t.n300,
                        child: const SizedBox.expand()),
                    AnimatedFractionallySizedBox(
                      duration: const Duration(milliseconds: 1000),
                      curve: Curves.linear,
                      alignment: Alignment.centerLeft,
                      widthFactor: percent / 100,
                      child: ColoredBox(
                          color: t.accent, child: const SizedBox.expand()),
                    ),
                  ],
                ),
              ),
            ),
          ),
        if (tools.isNotEmpty)
          Fold(
            open: _open,
            child: Container(
              margin: const EdgeInsets.only(left: 8, bottom: 4),
              padding: const EdgeInsets.only(left: 14),
              decoration: BoxDecoration(
                border: Border(left: BorderSide(color: t.hair)),
              ),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  for (final part in tools) _TaskToolRow(part: part),
                ],
              ),
            ),
          ),
      ],
    );
  }
}

/// One call under a task (web `TaskToolRows`): kind, target, and how it
/// went; opens to the same structured detail the flat chain shows.
class _TaskToolRow extends ConsumerStatefulWidget {
  const _TaskToolRow({required this.part});

  final MessagePart part;

  @override
  ConsumerState<_TaskToolRow> createState() => _TaskToolRowState();
}

class _TaskToolRowState extends ConsumerState<_TaskToolRow> {
  bool _open = false;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final part = widget.part;

    final (label, target, running, failed, seconds) = switch (part) {
      ToolPart() => (
          i18n.t('chat:kind.${toolKindKey(part.tool)}'),
          toolDetail(part),
          part.status == ToolStatus.running ||
              part.status == ToolStatus.pending,
          part.status == ToolStatus.error,
          toolDuration(part),
        ),
      SubtaskPart() => (
          i18n.t('chat:kind.task'),
          part.description,
          false,
          part.status == 'error',
          null,
        ),
      _ => ('', '', false, false, null),
    };

    final meta = running
        ? i18n.t('chat:toolMeta.running')
        : [
            if (seconds != null) formatDuration(seconds),
            if (failed) i18n.t('chat:toolMeta.failed'),
          ].join(' · ');

    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        InkWell(
          onTap: part is ToolPart
              ? () => setState(() => _open = !_open)
              : null,
          child: Padding(
            padding: const EdgeInsets.symmetric(vertical: 5),
            child: Row(
              children: [
                Text(
                  label,
                  style: TextStyle(
                    fontSize: FontSizes.sm,
                    fontWeight: FontWeight.w500,
                    color: t.n700,
                  ),
                ),
                const SizedBox(width: 10),
                Expanded(
                  child: Text(
                    target,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(
                      fontSize: FontSizes.sm,
                      color: failed ? t.danger : t.n800,
                      fontFamily: 'Menlo',
                      fontFamilyFallback: const ['monospace'],
                    ),
                  ),
                ),
                if (meta.isNotEmpty) ...[
                  const SizedBox(width: 8),
                  running
                      ? ShimmerText(
                          meta,
                          style: const TextStyle(fontSize: FontSizes.xs),
                        )
                      : Text(
                          meta,
                          style: TextStyle(
                              fontSize: FontSizes.xs, color: t.n600),
                        ),
                ],
                if (part is ToolPart) ...[
                  const SizedBox(width: 4),
                  AnimatedRotation(
                    turns: _open ? 0.25 : 0,
                    duration: const Duration(milliseconds: 200),
                    child:
                        Icon(Icons.chevron_right, size: 14, color: t.n500),
                  ),
                ],
              ],
            ),
          ),
        ),
        if (part is ToolPart && part.tool == 'task') SubagentLine(part: part),
        if (_open) ToolDetailBox(part: part),
      ],
    );
  }
}
