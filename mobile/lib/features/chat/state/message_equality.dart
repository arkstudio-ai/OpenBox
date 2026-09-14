import '../../../shared/models/message.dart';
import '../../../shared/models/message_part.dart';
import '../../../shared/models/todo.dart';
import '../../../shared/models/token_usage.dart';

/// Whether two copies of a message carry the same data, parts aside.
///
/// A history read decodes every message afresh, so identity says nothing
/// about change. These comparisons let the stream store keep the instance it
/// already holds when a refresh changed nothing, which is what lets widgets
/// skip rebuilding. The models keep identity equality on purpose —
/// `subagentProgressProvider` is keyed by part — so this lives beside the
/// store rather than on them.
///
/// Every field is compared: one missed here would hide a real update.
bool sameMessageFields(ChatMessage a, ChatMessage b) =>
    a.id == b.id &&
    a.sessionId == b.sessionId &&
    a.role == b.role &&
    a.createdAt == b.createdAt &&
    a.clientMessageId == b.clientMessageId &&
    a.agent == b.agent &&
    a.model == b.model &&
    a.parentId == b.parentId &&
    a.finish == b.finish &&
    a.summary == b.summary &&
    _sameTokens(a.tokens, b.tokens) &&
    sameJson(a.error, b.error) &&
    a.reaction == b.reaction;

/// Whether two copies of a part carry the same data (see [sameMessageFields]).
bool samePart(MessagePart a, MessagePart b) {
  if (identical(a, b)) return true;
  if (a.id != b.id) return false;
  return switch ((a, b)) {
    (final TextPart x, final TextPart y) =>
      x.text == y.text && x.channel == y.channel && x.synthetic == y.synthetic,
    (final ReasoningPart x, final ReasoningPart y) => x.text == y.text,
    (final SuggestionsPart x, final SuggestionsPart y) =>
      x.status == y.status &&
          x.expiresAt == y.expiresAt &&
          _sameList(x.items, y.items, _sameSuggestion),
    (final ToolPart x, final ToolPart y) =>
      x.tool == y.tool &&
          x.status == y.status &&
          sameJson(x.input, y.input) &&
          sameJson(x.output, y.output) &&
          x.error == y.error &&
          x.title == y.title &&
          x.duration == y.duration &&
          sameJson(x.metadata, y.metadata),
    (final StepStartPart x, final StepStartPart y) => x.step == y.step,
    (final StepFinishPart x, final StepFinishPart y) =>
      x.step == y.step &&
          x.inputTokens == y.inputTokens &&
          x.outputTokens == y.outputTokens &&
          x.cost == y.cost &&
          x.credits == y.credits &&
          x.duration == y.duration,
    (final CompactionPart x, final CompactionPart y) => x.summary == y.summary,
    (final SubtaskPart x, final SubtaskPart y) =>
      x.agent == y.agent &&
          x.description == y.description &&
          x.status == y.status &&
          x.output == y.output,
    (final PatchPart x, final PatchPart y) =>
      x.fromSnapshot == y.fromSnapshot &&
          x.toSnapshot == y.toSnapshot &&
          _sameList(x.files, y.files, _samePatchFile),
    (final FilePart x, final FilePart y) =>
      x.path == y.path &&
          x.mimeType == y.mimeType &&
          x.url == y.url &&
          x.assetId == y.assetId &&
          x.size == y.size &&
          x.transient == y.transient &&
          _sameRelation(x.relation, y.relation),
    (final AgentPart x, final AgentPart y) => x.agent == y.agent,
    (final RetryPart x, final RetryPart y) =>
      x.attempt == y.attempt && x.reason == y.reason,
    (final PlanPart x, final PlanPart y) =>
      x.path == y.path && x.status == y.status && x.content == y.content,
    (final TodoPart x, final TodoPart y) =>
      x.source == y.source && _sameList(x.items, y.items, _sameTodo),
    (final SkillJobPart x, final SkillJobPart y) =>
      x.jobId == y.jobId &&
          x.skillKey == y.skillKey &&
          x.operation == y.operation &&
          x.status == y.status &&
          x.errorCode == y.errorCode &&
          x.summary == y.summary &&
          _sameList(x.artifacts, y.artifacts, _sameArtifact),
    (final UnknownPart x, final UnknownPart y) =>
      x.rawType == y.rawType && sameJson(x.raw, y.raw),
    _ => false,
  };
}

/// Deep equality for decoded JSON: maps, lists and scalars.
bool sameJson(Object? a, Object? b) {
  if (identical(a, b)) return true;
  if (a is Map && b is Map) {
    if (a.length != b.length) return false;
    for (final entry in a.entries) {
      if (!b.containsKey(entry.key) || !sameJson(entry.value, b[entry.key])) {
        return false;
      }
    }
    return true;
  }
  if (a is List && b is List) {
    if (a.length != b.length) return false;
    for (var i = 0; i < a.length; i++) {
      if (!sameJson(a[i], b[i])) return false;
    }
    return true;
  }
  return a == b;
}

bool _sameList<T>(List<T> a, List<T> b, bool Function(T, T) same) {
  if (identical(a, b)) return true;
  if (a.length != b.length) return false;
  for (var i = 0; i < a.length; i++) {
    if (!same(a[i], b[i])) return false;
  }
  return true;
}

bool _sameTokens(TokenUsage? a, TokenUsage? b) {
  if (identical(a, b)) return true;
  if (a == null || b == null) return false;
  return a.input == b.input &&
      a.output == b.output &&
      a.cache == b.cache &&
      a.total == b.total &&
      a.limit == b.limit &&
      a.cost == b.cost &&
      a.credits == b.credits &&
      a.context == b.context;
}

bool _sameSuggestion(NextStepSuggestion a, NextStepSuggestion b) =>
    a.label == b.label && a.prompt == b.prompt && a.mode == b.mode;

bool _samePatchFile(PatchFile a, PatchFile b) =>
    a.path == b.path &&
    a.additions == b.additions &&
    a.deletions == b.deletions &&
    a.status == b.status;

bool _sameRelation(FileRelation? a, FileRelation? b) {
  if (identical(a, b)) return true;
  if (a == null || b == null) return false;
  return a.sourcePartId == b.sourcePartId &&
      a.groupId == b.groupId &&
      a.role == b.role &&
      a.kind == b.kind &&
      a.label == b.label &&
      a.caption == b.caption &&
      a.ordinal == b.ordinal &&
      a.revision == b.revision &&
      sameJson(a.metadata, b.metadata);
}

bool _sameTodo(TodoItem a, TodoItem b) =>
    a.id == b.id &&
    a.subject == b.subject &&
    a.status == b.status &&
    a.description == b.description &&
    a.activeForm == b.activeForm &&
    a.priority == b.priority &&
    a.source == b.source &&
    a.startedAt == b.startedAt;

bool _sameArtifact(SkillJobArtifact a, SkillJobArtifact b) =>
    a.assetId == b.assetId && a.name == b.name && a.mime == b.mime;
