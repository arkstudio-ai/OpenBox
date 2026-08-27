/// Assistant content assembly — a 1:1 port of frontend-v2
/// `features/chat/lib/content-view.ts`.
///
/// Separates the one user-facing answer from tool-step narration, and groups
/// produced files into the semantic artifacts they belong to, so a turn reads
/// as "here is the answer, here is what it made" rather than as a wall of
/// concatenated prose.
library;

import '../../../shared/models/message.dart';
import '../../../shared/models/message_part.dart';

typedef ArtifactRole = String; // input | evidence | intermediate | result | final

sealed class WorkEvent {
  WorkEvent({required this.id, required this.order});

  final String id;

  /// Position in the turn's flat part stream. A group lowers its own as
  /// later members arrive, so it sorts where its first file landed.
  int order;
}

class WorkNarration extends WorkEvent {
  WorkNarration({
    required super.id,
    required super.order,
    required this.text,
  });

  final String text;
}

class ArtifactGroup extends WorkEvent {
  ArtifactGroup({
    required super.id,
    required super.order,
    required this.artifactKind,
    required this.role,
    required this.label,
    required this.caption,
    required this.ordinal,
    required this.revision,
    required this.metadata,
    required this.sourceTool,
    required this.parts,
  });

  final String artifactKind;
  final ArtifactRole role;
  final String? label;
  final String? caption;
  final int? ordinal;
  final int? revision;
  final Map<String, dynamic> metadata;
  final ToolPart? sourceTool;
  final List<FilePart> parts;

  String? metadataString(String key) {
    final value = metadata[key];
    return value is String && value.trim().isNotEmpty ? value.trim() : null;
  }

  double? metadataNumber(String key) {
    final value = metadata[key];
    return value is num && value.isFinite ? value.toDouble() : null;
  }
}

class AssistantContentView {
  const AssistantContentView({
    required this.finalText,
    required this.finalMessageId,
    required this.hasFinal,
    required this.progress,
    required this.workEvents,
    required this.resultGroups,
    required this.verification,
    required this.incomplete,
  });

  final String finalText;
  final String? finalMessageId;
  final bool hasFinal;
  final List<WorkNarration> progress;
  final List<WorkEvent> workEvents;
  final List<ArtifactGroup> resultGroups;
  final ArtifactGroup? verification;

  /// The run stopped without ever producing an answer, yet it did work.
  final bool incomplete;
}

class _SegmentRecord {
  _SegmentRecord({required this.ordinal, this.id});

  String? id;
  final int ordinal;
  int? revision;
  String? script;
  String? transcript;
  String? sttVerdict;
  double? sttSimilarity;
}

String? _asText(Object? value) =>
    value is String && value.trim().isNotEmpty ? value.trim() : null;

int? _asNumber(Object? value) =>
    value is num && value.isFinite ? value.toInt() : null;

List<TextPart> _textParts(ChatMessage message) =>
    message.parts.whereType<TextPart>().where((p) => p.text.isNotEmpty).toList();

bool _isToolStepFinish(String? finish) =>
    finish == 'tool_calls' ||
    finish == 'tool-calls' ||
    finish == 'compact' ||
    finish == 'aborted';

String? _toolOutput(ToolPart? tool) {
  final output = tool?.output;
  return output is String ? output : null;
}

String? _toolInputValue(ToolPart? tool, String key) {
  final input = tool?.input;
  return input is Map<String, dynamic> ? _asText(input[key]) : null;
}

/// One `key=value` line out of a tool's human-readable result block.
String? _outputValue(String? output, String key) {
  if (output == null) return null;
  for (final line in output.split('\n')) {
    if (line.startsWith('$key=')) {
      final value = line.substring(key.length + 1).trim();
      return value.isEmpty ? null : value;
    }
  }
  return null;
}

/// Locate the one assistant step whose prose is the user-facing answer.
///
/// New rows carry `TextPart.channel`. `message.finish` is the durable
/// fallback for rows written before that field existed. A completely old
/// transcript may have no finish metadata at all; only in that case do we
/// preserve the former behaviour and treat its newest prose as the answer.
int _finalMessageIndex(List<ChatMessage> messages, bool streaming) {
  var candidate = -1;
  for (final (index, message) in messages.indexed) {
    final texts = _textParts(message);
    if (texts.any((p) => p.isFinal)) {
      candidate = index;
    } else if (message.finish == 'stop' && texts.any((p) => !p.isCommentary)) {
      candidate = index;
    }
  }
  if (candidate >= 0) return candidate;

  final newest = messages.length - 1;
  if (streaming && newest >= 0) {
    final message = messages[newest];
    final hasText = _textParts(message).any((p) => !p.isCommentary);
    final hasTool = message.parts
        .any((p) => p is ToolPart || p is SubtaskPart);
    if (hasText && !hasTool && !_isToolStepFinish(message.finish)) return newest;
  }

  // Compatibility for early OpenBox rows, which predate finish persistence.
  if (messages.every((m) => m.finish == null && m.error == null)) {
    for (var index = newest; index >= 0; index -= 1) {
      if (_textParts(messages[index]).any((p) => p.channel == null)) return index;
    }
  }
  return -1;
}

final _assetIdPattern = RegExp(r'\basset_id=([^;\s]+)');

List<String> _metadataAssetIds(ToolPart tool) {
  final ids = <String>[];
  final one = _asText(tool.metadata['asset_id']);
  if (one != null) ids.add(one);
  final many = tool.metadata['asset_ids'];
  if (many is List) {
    for (final value in many) {
      final id = _asText(value);
      if (id != null) ids.add(id);
    }
  }
  // Older tool metadata was deliberately pruned before persistence, while the
  // human-readable result still retained asset_id=… references.
  final output = _toolOutput(tool);
  if (output != null) {
    for (final match in _assetIdPattern.allMatches(output)) {
      final id = _asText(match.group(1));
      if (id != null) ids.add(id);
    }
  }
  return ids;
}

final _segmentLinePattern =
    RegExp(r'^segment_(\d+)_(id|revision|script|transcript|stt)=(.*)$');

Map<String, _SegmentRecord> _parseSegmentRecords(List<ToolPart> tools) {
  final byOrdinal = <int, _SegmentRecord>{};
  for (final tool in tools) {
    final output = _toolOutput(tool);
    if (tool.tool != 'video_project' || output == null) continue;
    for (final line in output.split('\n')) {
      final match = _segmentLinePattern.firstMatch(line);
      if (match == null) continue;
      final ordinal = int.tryParse(match.group(1)!) ?? 0;
      final record =
          byOrdinal.putIfAbsent(ordinal, () => _SegmentRecord(ordinal: ordinal));
      final value = match.group(3)!;
      switch (match.group(2)) {
        case 'id':
          record.id = value.trim();
        case 'revision':
          record.revision = int.tryParse(value.trim());
        case 'script':
          record.script = value.trim();
        case 'transcript':
          record.transcript = value.trim();
        case 'stt':
          final parts = value.split(':');
          final verdict = parts.isNotEmpty ? parts.first : '';
          record.sttVerdict = verdict.isEmpty ? null : verdict;
          if (parts.length > 1) {
            record.sttSimilarity = double.tryParse(parts[1]);
          }
      }
    }
  }

  final records = <String, _SegmentRecord>{};
  for (final record in byOrdinal.values) {
    records['ordinal:${record.ordinal}'] = record;
    if (record.id != null) records[record.id!] = record;
  }
  // STT happens after the video file was attached. Its later tool result is
  // therefore the freshest QA source for both old and new transcript rows.
  for (final tool in tools) {
    if (tool.tool != 'video_transcribe') continue;
    final output = _toolOutput(tool);
    final segmentId = _toolInputValue(tool, 'segment_id') ??
        _outputValue(output, 'segment_id');
    if (segmentId == null) continue;
    final record = records[segmentId] ?? _SegmentRecord(ordinal: 0, id: segmentId);
    record.transcript = _outputValue(output, 'transcript') ?? record.transcript;
    record.sttVerdict = _outputValue(output, 'verdict') ?? record.sttVerdict;
    final similarity = double.tryParse(_outputValue(output, 'similarity') ?? '');
    if (similarity != null) record.sttSimilarity = similarity;
    records[segmentId] = record;
  }
  return records;
}

String _inferKind(FilePart part, ToolPart? tool) {
  final declared = part.relation?.kind;
  if (declared != null && declared != 'file') return declared;
  return switch (tool?.tool) {
    'computer' => 'computer_screenshot',
    'view_image' => 'inspection_image',
    'image_gen' => 'generated_image',
    'video_generate' => 'video_segment',
    'video_render' => 'video_final',
    'share_file' => 'shared_file',
    _ => part.transient ? 'evidence' : 'file',
  };
}

ArtifactRole _inferRole(FilePart part, String kind) {
  final declared = part.relation?.role;
  if (declared != null && declared.isNotEmpty) return declared;
  if (kind == 'computer_screenshot' ||
      kind == 'inspection_image' ||
      part.transient) {
    return 'evidence';
  }
  if (kind == 'video_segment') return 'intermediate';
  if (kind == 'video_final') return 'final';
  return 'result';
}

ToolPart? _sourceForFile(
  FilePart part,
  List<ToolPart> precedingTools,
  Map<String, ToolPart> toolsById,
  Map<String, ToolPart> toolsByAsset,
) {
  final declared = _asText(part.relation?.sourcePartId);
  if (declared != null && toolsById.containsKey(declared)) {
    return toolsById[declared];
  }
  final assetId = part.assetId;
  if (assetId != null && toolsByAsset.containsKey(assetId)) {
    return toolsByAsset[assetId];
  }
  for (final tool in precedingTools) {
    if (part.path.contains(tool.id)) return tool;
  }
  return precedingTools.isEmpty ? null : precedingTools.last;
}

_SegmentRecord? _segmentFor(
  FilePart part,
  ToolPart? tool,
  Map<String, _SegmentRecord> records,
) {
  final id = _asText(part.relation?.metadata['segment_id']) ??
      _toolInputValue(tool, 'segment_id') ??
      _outputValue(_toolOutput(tool), 'segment_id');
  if (id != null && records.containsKey(id)) return records[id];
  final ordinal = part.relation?.ordinal;
  return ordinal == null ? null : records['ordinal:$ordinal'];
}

String? _captionFor(
  FilePart part,
  ToolPart? tool,
  String kind,
  _SegmentRecord? segment,
) {
  final declared = _asText(part.relation?.caption);
  if (declared != null) return declared;
  if (kind == 'generated_image') return _toolInputValue(tool, 'prompt');
  if (kind == 'video_segment') return segment?.script;
  return null;
}

int _resultOrder(ArtifactGroup group) {
  if (group.role == 'final') return 0;
  if (group.role == 'result') return 1;
  return 2;
}

/// Build the assistant view over a turn's messages.
AssistantContentView buildAssistantContentView(
  List<ChatMessage> messages,
  bool streaming,
) {
  final finalIndex = _finalMessageIndex(messages, streaming);
  final finalParts = finalIndex >= 0 ? _textParts(messages[finalIndex]) : <TextPart>[];
  final finalText =
      finalParts.where((p) => !p.isCommentary).map((p) => p.text).join();
  final hasFinal = finalText.trim().isNotEmpty;

  final tools = [
    for (final message in messages) ...message.parts.whereType<ToolPart>(),
  ];
  final toolsById = {for (final tool in tools) tool.id: tool};
  final toolsByAsset = <String, ToolPart>{};
  for (final tool in tools) {
    for (final assetId in _metadataAssetIds(tool)) {
      toolsByAsset[assetId] = tool;
    }
  }
  final segmentRecords = _parseSegmentRecords(tools);

  final progress = <WorkNarration>[];
  final groups = <String, ArtifactGroup>{};
  var order = 0;

  for (final (messageIndex, message) in messages.indexed) {
    final precedingTools = <ToolPart>[];
    for (final part in message.parts) {
      order += 1;
      if (part is ToolPart) precedingTools.add(part);
      if (part is TextPart) {
        final isFinalPart = messageIndex == finalIndex &&
            !part.isCommentary &&
            !_isToolStepFinish(message.finish);
        if (!isFinalPart && part.text.trim().isNotEmpty) {
          progress.add(
            WorkNarration(id: part.id, order: order, text: part.text),
          );
        }
      }
      if (part is! FilePart) continue;

      final sourceTool =
          _sourceForFile(part, precedingTools, toolsById, toolsByAsset);
      final artifactKind = _inferKind(part, sourceTool);
      final role = _inferRole(part, artifactKind);
      final segment = _segmentFor(part, sourceTool, segmentRecords);
      final metadata = <String, dynamic>{...?part.relation?.metadata};
      if (segment?.transcript != null && metadata['transcript'] == null) {
        metadata['transcript'] = segment!.transcript;
      }
      if (segment?.sttVerdict != null && metadata['stt_verdict'] == null) {
        metadata['stt_verdict'] = segment!.sttVerdict;
      }
      if (segment?.sttSimilarity != null && metadata['stt_similarity'] == null) {
        metadata['stt_similarity'] = segment!.sttSimilarity;
      }
      final toolOutput = _toolOutput(sourceTool);
      final segmentId = _asText(metadata['segment_id']) ??
          _toolInputValue(sourceTool, 'segment_id') ??
          _outputValue(toolOutput, 'segment_id');
      final productionId = _asText(metadata['production_id']) ??
          _toolInputValue(sourceTool, 'production_id') ??
          _outputValue(toolOutput, 'production_id');
      final groupId = _asText(part.relation?.groupId) ??
          (artifactKind == 'video_segment' && segmentId != null
              ? 'video:${productionId ?? "unknown"}:segment:$segmentId'
              : artifactKind == 'video_final' && productionId != null
                  ? 'video:$productionId:final'
                  : sourceTool != null
                      ? 'tool:${sourceTool.id}'
                      : 'file:${part.id}');

      final existing = groups[groupId];
      if (existing != null) {
        existing.parts.add(part);
        if (order < existing.order) existing.order = order;
        continue;
      }
      groups[groupId] = ArtifactGroup(
        id: groupId,
        order: order,
        artifactKind: artifactKind,
        role: role,
        label: _asText(part.relation?.label) ?? _asText(sourceTool?.title),
        caption: _captionFor(part, sourceTool, artifactKind, segment),
        ordinal: _asNumber(part.relation?.ordinal) ?? segment?.ordinal,
        revision: _asNumber(part.relation?.revision) ?? segment?.revision,
        metadata: metadata,
        sourceTool: sourceTool,
        parts: [part],
      );
    }
  }

  final ordered = groups.values.toList()
    ..sort((a, b) => a.order.compareTo(b.order));
  final evidence = ordered.where((g) => g.role == 'evidence').toList();
  final results = ordered
      .where((g) => g.role != 'evidence' && g.role != 'input')
      .toList()
    ..sort((a, b) {
      final byRole = _resultOrder(a) - _resultOrder(b);
      if (byRole != 0) return byRole;
      if (a.artifactKind == 'video_segment' && b.artifactKind == 'video_segment') {
        return (a.ordinal ?? 1 << 30).compareTo(b.ordinal ?? 1 << 30);
      }
      return a.order.compareTo(b.order);
    });

  // Computer-use produces one screenshot per action. Keep checkpoints in the
  // work log, but surface the last frame as final verification only when the
  // turn has no richer deliverable of its own.
  final computerEvidence =
      evidence.where((g) => g.artifactKind == 'computer_screenshot').toList();
  final verification = hasFinal && results.isEmpty && computerEvidence.isNotEmpty
      ? computerEvidence.last
      : null;
  final workEvidence = verification == null
      ? evidence
      : evidence.where((g) => g.id != verification.id).toList();
  final workEvents = <WorkEvent>[...progress, ...workEvidence]
    ..sort((a, b) => a.order.compareTo(b.order));
  final hasWork =
      progress.isNotEmpty || tools.isNotEmpty || ordered.isNotEmpty;

  return AssistantContentView(
    finalText: finalText,
    finalMessageId: finalIndex >= 0 ? messages[finalIndex].id : null,
    hasFinal: hasFinal,
    progress: progress,
    workEvents: workEvents,
    resultGroups: results,
    verification: verification,
    incomplete: !streaming && !hasFinal && hasWork,
  );
}
