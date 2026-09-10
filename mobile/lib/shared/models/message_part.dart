import 'json.dart';
import 'todo.dart';

/// The `MessagePart` discriminated union, mirroring frontend-v2
/// `shared/types/api.ts:47-164` / backend `models/message.py`.
sealed class MessagePart {
  const MessagePart({required this.id});

  final String id;

  String get type;

  static MessagePart fromJson(Map<String, dynamic> json) {
    final id = asString(json['id']) ?? '';
    switch (asString(json['type'])) {
      case 'text':
        return TextPart(
          id: id,
          text: asString(json['text']) ?? '',
          channel: asString(json['channel']),
          synthetic: asBool(json['synthetic']) ?? false,
        );
      case 'reasoning':
        return ReasoningPart(id: id, text: asString(json['text']) ?? '');
      case 'suggestions':
        return SuggestionsPart.fromJson(json);
      case 'tool':
        return ToolPart(
          id: id,
          tool: asString(json['tool']) ?? '',
          status: toolStatusFrom(asString(json['status'])),
          input: json['input'],
          output: json['output'],
          error: asString(json['error']),
          title: asString(json['title']),
          duration: asDouble(json['duration']),
          metadata: asMap(json['metadata']),
        );
      case 'step-start':
        return StepStartPart(id: id, step: asInt(json['step']) ?? 0);
      case 'step-finish':
        return StepFinishPart(
          id: id,
          step: asInt(json['step']) ?? 0,
          inputTokens: asInt(json['input_tokens']) ?? 0,
          outputTokens: asInt(json['output_tokens']) ?? 0,
          cost: asDouble(json['cost']) ?? 0,
          credits: asString(json['credits']),
          duration: asDouble(json['duration']) ?? 0,
        );
      case 'compaction':
        return CompactionPart(id: id, summary: asString(json['summary']));
      case 'subtask':
        return SubtaskPart(
          id: id,
          agent: asString(json['agent']) ?? '',
          description: asString(json['description']) ?? '',
          status: asString(json['status']) ?? '',
          output: asString(json['output']),
        );
      case 'patch':
        return PatchPart(
          id: id,
          files: asList(
            json['files'],
          ).whereType<Map<String, dynamic>>().map(PatchFile.fromJson).toList(),
          fromSnapshot: asString(json['from_snapshot']),
          toSnapshot: asString(json['to_snapshot']),
        );
      case 'file':
        return FilePart(
          id: id,
          path: asString(json['path']) ?? '',
          mimeType: asString(json['mime_type']),
          url: asString(json['url']),
          assetId: asString(json['asset_id']),
          size: asInt(json['size']),
          transient: asBool(json['transient']) ?? false,
          relation: json['relation'] is Map<String, dynamic>
              ? FileRelation.fromJson(json['relation'] as Map<String, dynamic>)
              : null,
        );
      case 'agent':
        return AgentPart(id: id, agent: asString(json['agent']) ?? '');
      case 'retry':
        return RetryPart(
          id: id,
          attempt: asInt(json['attempt']) ?? 0,
          reason: asString(json['reason']),
        );
      case 'plan':
        return PlanPart(
          id: id,
          path: asString(json['path']) ?? '',
          status: asString(json['status']) ?? 'writing',
          content: asString(json['content']) ?? '',
        );
      case 'todo':
        return TodoPart(
          id: id,
          items: asList(
            json['items'],
          ).whereType<Map<String, dynamic>>().map(TodoItem.fromJson).toList(),
          source: asString(json['source']),
        );
      case 'skill_job':
        return SkillJobPart(
          id: id,
          jobId: asString(json['jobId']) ?? '',
          skillKey: asString(json['skillKey']) ?? '',
          operation: asString(json['operation']) ?? '',
          status: asString(json['status']) ?? '',
          errorCode: asString(json['errorCode']),
          summary: asString(json['summary']) ?? '',
          artifacts: asList(json['artifacts'])
              .whereType<Map<String, dynamic>>()
              .map(SkillJobArtifact.fromJson)
              .toList(),
        );
      default:
        return UnknownPart(
          id: id,
          rawType: asString(json['type']) ?? '',
          raw: json,
        );
    }
  }
}

enum SuggestionMode { send, draft }

class NextStepSuggestion {
  const NextStepSuggestion({
    required this.label,
    required this.prompt,
    required this.mode,
  });

  final String label;
  final String prompt;
  final SuggestionMode mode;
}

/// Auxiliary UI data, never rendered as assistant prose or executed as tools.
class SuggestionsPart extends MessagePart {
  const SuggestionsPart({required super.id, required this.items});

  factory SuggestionsPart.fromJson(Map<String, dynamic> json) {
    final items = <NextStepSuggestion>[];
    final labels = <String>{};
    final prompts = <String>{};
    for (final raw in asList(json['items']).whereType<Map<String, dynamic>>()) {
      final label = asString(raw['label'])?.trim() ?? '';
      final prompt = asString(raw['prompt'])?.trim() ?? '';
      final mode = switch (raw['mode']) {
        'send' => SuggestionMode.send,
        'draft' => SuggestionMode.draft,
        _ => null,
      };
      if (mode == null ||
          label.isEmpty ||
          prompt.isEmpty ||
          label.runes.length > 32 ||
          prompt.runes.length > 800 ||
          labels.contains(label) ||
          prompts.contains(prompt)) {
        continue;
      }
      labels.add(label);
      prompts.add(prompt);
      items.add(NextStepSuggestion(label: label, prompt: prompt, mode: mode));
      if (items.length == 3) break;
    }
    return SuggestionsPart(
      id: asString(json['id']) ?? '',
      items: List.unmodifiable(items),
    );
  }

  final List<NextStepSuggestion> items;

  @override
  String get type => 'suggestions';
}

enum ToolStatus { pending, running, completed, error, waitingInput }

ToolStatus toolStatusFrom(String? value) => switch (value) {
  'running' => ToolStatus.running,
  'completed' => ToolStatus.completed,
  'error' => ToolStatus.error,
  'waiting_input' => ToolStatus.waitingInput,
  _ => ToolStatus.pending,
};

class TextPart extends MessagePart {
  const TextPart({
    required super.id,
    required this.text,
    this.channel,
    this.synthetic = false,
  });

  final String text;

  /// Tool-step narration is `commentary`; only terminal prose is `final`.
  /// Null on rows written before the backend carried the field.
  final String? channel;
  final bool synthetic;

  bool get isCommentary => channel == 'commentary';

  bool get isFinal => channel == 'final';

  @override
  String get type => 'text';

  TextPart appendDelta(String delta) => TextPart(
    id: id,
    text: text + delta,
    channel: channel,
    synthetic: synthetic,
  );
}

class ReasoningPart extends MessagePart {
  const ReasoningPart({required super.id, required this.text});

  final String text;

  @override
  String get type => 'reasoning';

  ReasoningPart appendDelta(String delta) =>
      ReasoningPart(id: id, text: text + delta);
}

class ToolPart extends MessagePart {
  const ToolPart({
    required super.id,
    required this.tool,
    required this.status,
    this.input,
    this.output,
    this.error,
    this.title,
    this.duration,
    this.metadata = const {},
  });

  final String tool;
  final ToolStatus status;
  final dynamic input;
  final dynamic output;
  final String? error;
  final String? title;
  final double? duration;
  final Map<String, dynamic> metadata;

  @override
  String get type => 'tool';
}

class StepStartPart extends MessagePart {
  const StepStartPart({required super.id, required this.step});

  final int step;

  @override
  String get type => 'step-start';
}

class StepFinishPart extends MessagePart {
  const StepFinishPart({
    required super.id,
    required this.step,
    required this.inputTokens,
    required this.outputTokens,
    required this.cost,
    this.credits,
    required this.duration,
  });

  final int step;
  final int inputTokens;
  final int outputTokens;
  final double cost;
  final String? credits;
  final double duration;

  @override
  String get type => 'step-finish';
}

class CompactionPart extends MessagePart {
  const CompactionPart({required super.id, this.summary});

  final String? summary;

  @override
  String get type => 'compaction';
}

class SubtaskPart extends MessagePart {
  const SubtaskPart({
    required super.id,
    required this.agent,
    required this.description,
    required this.status,
    this.output,
  });

  final String agent;
  final String description;
  final String status;
  final String? output;

  @override
  String get type => 'subtask';
}

class PatchFile {
  const PatchFile({
    required this.path,
    required this.additions,
    required this.deletions,
    required this.status,
  });

  factory PatchFile.fromJson(Map<String, dynamic> json) => PatchFile(
    path: asString(json['path']) ?? '',
    additions: asInt(json['additions']) ?? 0,
    deletions: asInt(json['deletions']) ?? 0,
    status: asString(json['status']) ?? 'modified',
  );

  final String path;
  final int additions;
  final int deletions;
  final String status; // added | modified | deleted
}

class PatchPart extends MessagePart {
  const PatchPart({
    required super.id,
    required this.files,
    this.fromSnapshot,
    this.toSnapshot,
  });

  final List<PatchFile> files;
  final String? fromSnapshot;
  final String? toSnapshot;

  @override
  String get type => 'patch';
}

/// Where a produced file came from and what it is (`FileRelation`,
/// frontend-v2 `shared/types/api.ts`). Absent on rows written before the
/// backend started recording provenance — every reader must cope with null.
class FileRelation {
  const FileRelation({
    this.sourcePartId,
    this.groupId,
    this.role,
    this.kind,
    this.label,
    this.caption,
    this.ordinal,
    this.revision,
    this.metadata = const {},
  });

  factory FileRelation.fromJson(Map<String, dynamic> json) => FileRelation(
    sourcePartId: asString(json['source_part_id']),
    groupId: asString(json['group_id']),
    role: asString(json['role']),
    kind: asString(json['kind']),
    label: asString(json['label']),
    caption: asString(json['caption']),
    ordinal: asInt(json['ordinal']),
    revision: asInt(json['revision']),
    metadata: asMap(json['metadata']),
  );

  /// Tool part that produced this resource.
  final String? sourcePartId;

  /// Resources in one semantic variant/result set share a group.
  final String? groupId;

  /// input | evidence | intermediate | result | final
  final String? role;

  /// Extensible renderer hint, e.g. computer_screenshot or video_segment.
  final String? kind;
  final String? label;
  final String? caption;
  final int? ordinal;
  final int? revision;
  final Map<String, dynamic> metadata;
}

class FilePart extends MessagePart {
  const FilePart({
    required super.id,
    required this.path,
    this.mimeType,
    this.url,
    this.assetId,
    this.size,
    this.transient = false,
    this.relation,
  });

  final String path;
  final String? mimeType;
  final String? url;
  final String? assetId;
  final int? size;

  /// Working evidence such as Computer screenshots; excluded from Resources.
  final bool transient;
  final FileRelation? relation;

  @override
  String get type => 'file';
}

class AgentPart extends MessagePart {
  const AgentPart({required super.id, required this.agent});

  final String agent;

  @override
  String get type => 'agent';
}

class RetryPart extends MessagePart {
  const RetryPart({required super.id, required this.attempt, this.reason});

  final int attempt;
  final String? reason;

  @override
  String get type => 'retry';
}

class PlanPart extends MessagePart {
  const PlanPart({
    required super.id,
    required this.path,
    required this.status,
    required this.content,
  });

  final String path;
  final String status; // writing | ready | accepted | rejected
  final String content;

  @override
  String get type => 'plan';
}

class TodoPart extends MessagePart {
  const TodoPart({required super.id, required this.items, this.source});

  final List<TodoItem> items;
  final String? source;

  @override
  String get type => 'todo';
}

/// Historical platform-written receipt retained for old transcript rendering.
class SkillJobPart extends MessagePart {
  const SkillJobPart({
    required super.id,
    required this.jobId,
    required this.skillKey,
    required this.operation,
    required this.status,
    this.errorCode,
    this.summary = '',
    this.artifacts = const [],
  });

  final String jobId;
  final String skillKey;
  final String operation;
  final String status;
  final String? errorCode;
  final String summary;
  final List<SkillJobArtifact> artifacts;

  @override
  String get type => 'skill_job';
}

/// Small, durable artifact envelope embedded in historical skill-job receipts.
/// Fields are tolerant because receipts written before the envelope existed
/// omit it, and a partial old row should still render an unavailable fallback.
class SkillJobArtifact {
  const SkillJobArtifact({this.assetId = '', this.name = '', this.mime});

  final String assetId;
  final String name;
  final String? mime;

  factory SkillJobArtifact.fromJson(Map<String, dynamic> json) =>
      SkillJobArtifact(
        assetId: asString(json['assetId']) ?? '',
        name: asString(json['name']) ?? '',
        mime: asString(json['mime']),
      );
}

class UnknownPart extends MessagePart {
  const UnknownPart({
    required super.id,
    required this.rawType,
    required this.raw,
  });

  final String rawType;
  final Map<String, dynamic> raw;

  @override
  String get type => rawType;
}
