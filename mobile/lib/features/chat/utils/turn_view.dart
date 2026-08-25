import '../../../shared/models/message.dart';
import '../../../shared/models/message_part.dart';
import '../../../shared/models/token_usage.dart';

/// Turn assembly, mirroring frontend-v2 `features/chat/lib/turn-view.ts`:
/// consecutive assistant messages merge into ONE turn; parts aggregate into
/// fixed regions (process / thinking / tools / body / artifacts / todo).
sealed class ChatRow {
  const ChatRow();
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
    required this.todos,
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
  final List<TodoPart> todos;
  final Map<String, dynamic>? error;
  final TokenUsage? tokens;

  String get lastMessageId => messages.last.id;

  bool get hasBody => bodyText.trim().isNotEmpty;

  bool get hasTools => toolChain.isNotEmpty;

  bool get hasThinking => thinkingText.trim().isNotEmpty;

  bool get hasProcess => stepCount > 0 || durationSec > 0;
}

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
  final todos = <TodoPart>[];
  MessagePart? lastPart;
  Map<String, dynamic>? error;
  TokenUsage? tokens;

  for (final message in messages) {
    error = message.error ?? error;
    tokens = message.tokens ?? tokens;
    for (final part in message.parts) {
      lastPart = part;
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
        case TodoPart():
          todos.add(part);
        case UnknownPart():
          break;
      }
    }
  }

  final toolsStreaming = tools.any(
    (p) =>
        p is ToolPart &&
        (p.status == ToolStatus.running || p.status == ToolStatus.pending),
  );

  return AssistantTurnData(
    messages: messages,
    contextTokens: contextTokens,
    durationSec: duration,
    stepCount: stepCount,
    thinkingText: thinking.join('\n\n'),
    thinkingStreaming: lastPart is ReasoningPart,
    toolChain: tools,
    toolsStreaming: toolsStreaming,
    bodyText: body.join('\n\n'),
    patches: patches,
    files: files,
    plans: plans,
    notices: notices,
    todos: todos,
    error: error,
    tokens: tokens,
  );
}
