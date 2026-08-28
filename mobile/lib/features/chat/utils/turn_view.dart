import '../../../shared/models/message.dart';
import '../../../shared/models/message_part.dart';
import '../../../shared/models/todo.dart';
import '../../../shared/models/token_usage.dart';

/// Turn assembly, mirroring frontend-v2 `features/chat/lib/turn-view.ts`:
/// consecutive assistant messages merge into ONE turn; parts aggregate into
/// fixed regions (process / thinking / tools / body / artifacts / todo).
sealed class ChatRow {
  const ChatRow();
}

/// One task, with the calls made while it was the one in progress.
class TodoTask {
  const TodoTask({required this.item, required this.tools});

  final TodoItem item;
  final List<MessagePart> tools; // ToolPart | SubtaskPart
}

/// The task card's view (web `TodoView`): the card renders instead of the
/// flat tool chain; calls that fell outside any task stay in the chain.
class TodoView {
  const TodoView({
    required this.tasks,
    required this.before,
    required this.after,
    required this.activeForm,
    required this.done,
    required this.total,
    required this.current,
    required this.allDone,
  });

  final List<TodoTask> tasks;

  /// Calls made before any task started.
  final List<MessagePart> before;

  /// Calls made after the last task closed.
  final List<MessagePart> after;

  /// The heading: the running task's own wording, when it gave one.
  final String? activeForm;
  final int done;

  /// Steps that count towards the total — a cancelled task is not one.
  final int total;

  /// 1-based position of the running task, or `done` when nothing runs.
  final int current;
  final bool allDone;

  /// The tools a todo turn shows outside the card, in order.
  List<MessagePart> get looseTools => [...before, ...after];
}

/// Tools the card itself accounts for; the flat chain must not repeat them.
bool _isTodoTool(MessagePart part) =>
    part is ToolPart && (part.tool == 'todo_write' || part.tool == 'todo_read');

/// Group a turn's calls under the task that was running when each was made
/// (web `buildTodoView`): todo parts are snapshots in stream order, so the
/// task in progress at any point is the one the most recent snapshot had in
/// progress; the newest wins when several are marked.
TodoView? buildTodoView(List<MessagePart> parts) {
  final snapshots = parts.whereType<TodoPart>().toList();
  if (snapshots.isEmpty) return null;

  final buckets = <String, List<MessagePart>>{};
  final before = <MessagePart>[];
  final after = <MessagePart>[];
  String? running;
  var started = false;

  for (final part in parts) {
    if (part is TodoPart) {
      TodoItem? active;
      for (final item in part.items.reversed) {
        if (item.status == TodoStatus.inProgress) {
          active = item;
          break;
        }
      }
      running = active?.id;
      if (running != null) started = true;
      continue;
    }
    if (part is! ToolPart && part is! SubtaskPart) continue;
    if (_isTodoTool(part)) continue;
    if (running != null) {
      buckets.putIfAbsent(running, () => []).add(part);
    } else if (started) {
      after.add(part);
    } else {
      before.add(part);
    }
  }

  final items = snapshots.last.items;
  final tasks = [
    for (final item in items)
      TodoTask(item: item, tools: buckets[item.id] ?? const []),
  ];
  final counted =
      items.where((i) => i.status != TodoStatus.cancelled).toList();
  final done =
      counted.where((i) => i.status == TodoStatus.completed).length;
  final activeIndex =
      counted.indexWhere((i) => i.status == TodoStatus.inProgress);
  final active = activeIndex >= 0 ? counted[activeIndex] : null;
  final activeForm = active?.activeForm?.trim();

  return TodoView(
    tasks: tasks,
    before: before,
    after: after,
    activeForm: (activeForm != null && activeForm.isNotEmpty) ? activeForm : null,
    done: done,
    total: counted.length,
    current: activeIndex >= 0 ? activeIndex + 1 : done,
    allDone: counted.isNotEmpty && done == counted.length,
  );
}

class UserRowData extends ChatRow {
  const UserRowData(this.message);

  final ChatMessage message;
}

class AssistantTurnData extends ChatRow {
  const AssistantTurnData({
    required this.messages,
    required this.contextTokens,
    required this.durationSec,
    required this.stepCount,
    required this.thinkingText,
    required this.thinkingStreaming,
    required this.toolChain,
    required this.toolsStreaming,
    required this.bodyText,
    required this.patches,
    required this.files,
    required this.plans,
    required this.notices,
    required this.todo,
    required this.error,
    required this.tokens,
  });

  final List<ChatMessage> messages;
  final int contextTokens;
  final double durationSec;
  final int stepCount;
  final String thinkingText;
  final bool thinkingStreaming;
  final List<MessagePart> toolChain; // ToolPart | SubtaskPart, stream order
  final bool toolsStreaming;
  final String bodyText;
  final List<PatchPart> patches;
  final List<FilePart> files;
  final List<PlanPart> plans;
  final List<MessagePart> notices; // CompactionPart | RetryPart | AgentPart

  /// Present when the turn kept a todo list; the card renders instead of
  /// the flat tool chain (which then holds only the loose calls).
  final TodoView? todo;
  final Map<String, dynamic>? error;
  final TokenUsage? tokens;

  String get lastMessageId => messages.last.id;

  /// The newest message's finish reason (web `AssistantTurnMeta.finish`).
  String? get finish => messages.last.finish;

  bool get hasBody => bodyText.trim().isNotEmpty;

  bool get hasTools => toolChain.isNotEmpty;

  bool get hasThinking => thinkingText.trim().isNotEmpty;

  bool get hasProcess => stepCount > 0 || durationSec > 0;
}

/// Internal continuation/plan/compaction prompts belong to the model
/// protocol, not to the user's transcript (web `isSyntheticOnlyUserMessage`).
bool isSyntheticOnlyUserMessage(ChatMessage message) =>
    message.parts.isNotEmpty &&
    message.parts.every((part) => part is TextPart && part.synthetic);

/// Merge consecutive assistant messages into turns, then aggregate.
List<ChatRow> buildChatRows(List<ChatMessage> messages) {
  final rows = <ChatRow>[];
  var group = <ChatMessage>[];

  void flush() {
    if (group.isNotEmpty) {
      rows.add(_buildTurn(group));
      group = [];
    }
  }

  for (final message in messages) {
    if (message.isUser) {
      // Skipping the synthetic turn also lets its following assistant
      // message stay in the same visible turn as the preceding real request.
      if (isSyntheticOnlyUserMessage(message)) continue;
      flush();
      rows.add(UserRowData(message));
    } else if (message.isAssistant) {
      group.add(message);
    }
    // system messages are not rendered (web parity)
  }
  flush();
  return rows;
}

AssistantTurnData _buildTurn(List<ChatMessage> messages) {
  var contextTokens = 0;
  var duration = 0.0;
  var stepCount = 0;
  final thinking = <String>[];
  final tools = <MessagePart>[];
  final body = <String>[];
  final patches = <PatchPart>[];
  final files = <FilePart>[];
  final plans = <PlanPart>[];
  final notices = <MessagePart>[];
  final allParts = <MessagePart>[];
  MessagePart? lastPart;
  Map<String, dynamic>? error;
  TokenUsage? tokens;

  for (final message in messages) {
    error = message.error ?? error;
    tokens = message.tokens ?? tokens;
    for (final part in message.parts) {
      lastPart = part;
      allParts.add(part);
      switch (part) {
        case TextPart(:final text):
          if (text.isNotEmpty) body.add(text);
        case ReasoningPart(:final text):
          if (text.isNotEmpty) thinking.add(text);
        case ToolPart():
          tools.add(part);
          final metaDuration = part.metadata['duration'];
          duration += part.duration ?? (metaDuration is num ? metaDuration.toDouble() : 0);
        case SubtaskPart():
          tools.add(part);
        case StepStartPart():
          stepCount += 1;
        case StepFinishPart():
          if (part.inputTokens > contextTokens) contextTokens = part.inputTokens;
          duration += part.duration;
        case CompactionPart() || RetryPart() || AgentPart():
          notices.add(part);
        case PatchPart():
          patches.add(part);
        case FilePart():
          files.add(part);
        case PlanPart():
          plans.add(part);
        case TodoPart() || SkillJobPart() || UnknownPart():
          break;
      }
    }
  }

  bool isLive(MessagePart p) =>
      p is ToolPart &&
      (p.status == ToolStatus.running || p.status == ToolStatus.pending);

  // The card accounts for every call it filed under a task; the flat chain
  // keeps only what fell outside (web parity).
  final todo = buildTodoView(allParts);
  final chain = todo != null ? todo.looseTools : tools;
  final toolsStreaming = chain.any(isLive);

  return AssistantTurnData(
    messages: messages,
    contextTokens: contextTokens,
    durationSec: duration,
    stepCount: stepCount,
    thinkingText: thinking.join('\n\n'),
    thinkingStreaming: lastPart is ReasoningPart,
    toolChain: chain,
    toolsStreaming: toolsStreaming,
    bodyText: body.join('\n\n'),
    patches: patches,
    files: files,
    plans: plans,
    notices: notices,
    todo: todo,
    error: error,
    tokens: tokens,
  );
}
