import 'dart:convert';

import '../../../shared/models/message_part.dart';

/// What one of the assistant's memory write tools did, as shown under its
/// answer (web `lib/memory-receipt.ts`).
enum MemoryReceiptKind {
  remembered,
  alreadyRemembered,
  refused,
  paused,
  updated,
  forgotten,
}

class MemoryReceipt {
  const MemoryReceipt(this.kind, {this.memoryId, this.summary, this.revision});

  final MemoryReceiptKind kind;
  final String? memoryId;
  final String? summary;
  final int? revision;
}

const _tools = {'memory.remember', 'memory.update', 'memory.forget'};

Map<String, dynamic> _output(Object? value) {
  if (value is Map<String, dynamic>) return value;
  if (value is! String || value.isEmpty) return const {};
  try {
    final decoded = jsonDecode(value);
    return decoded is Map<String, dynamic> ? decoded : const {};
  } on FormatException {
    return const {};
  }
}

/// Only a completed memory tool's structured result counts; prose never
/// does. The live frame carries the JSON output; a reloaded transcript keeps
/// the persisted `assistant_memory` metadata, which wins where both exist.
MemoryReceipt? memoryReceipt(MessagePart part) {
  if (part is! ToolPart ||
      !_tools.contains(part.tool) ||
      part.status != ToolStatus.completed) {
    return null;
  }
  final persisted = part.metadata['assistant_memory'];
  final value = {
    ..._output(part.output),
    if (persisted is Map<String, dynamic>) ...persisted,
  };
  final memoryId = value['memory_id'] is String && value['memory_id'] != ''
      ? value['memory_id'] as String
      : null;
  final summary =
      value['summary'] is String && (value['summary'] as String).trim() != ''
      ? value['summary'] as String
      : null;
  return switch ('${part.tool}:${value['state']}') {
    'memory.remember:remembered' when memoryId != null && summary != null =>
      MemoryReceipt(
        MemoryReceiptKind.remembered,
        memoryId: memoryId,
        summary: summary,
        revision: value['revision'] is int ? value['revision'] as int : null,
      ),
    'memory.remember:already_remembered' => const MemoryReceipt(
      MemoryReceiptKind.alreadyRemembered,
    ),
    'memory.remember:refused' ||
    'memory.update:refused' => const MemoryReceipt(MemoryReceiptKind.refused),
    'memory.remember:paused' => const MemoryReceipt(MemoryReceiptKind.paused),
    'memory.update:updated' when memoryId != null && summary != null =>
      MemoryReceipt(
        MemoryReceiptKind.updated,
        memoryId: memoryId,
        summary: summary,
      ),
    'memory.forget:forgotten' => const MemoryReceipt(
      MemoryReceiptKind.forgotten,
    ),
    _ => null,
  };
}
