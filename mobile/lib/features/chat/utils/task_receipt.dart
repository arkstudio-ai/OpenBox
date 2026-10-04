import 'dart:convert';

import '../../../shared/models/message_part.dart';

typedef TaskReceipt = ({String taskId, String commandId});

/// IDs only come from successful canonical write-tool receipts. Assistant prose,
/// user tools and failed/partial outputs never create an actionable task card.
TaskReceipt? taskReceipt(MessagePart part) {
  if (part is! ToolPart ||
      part.status != ToolStatus.completed ||
      !const {
        'tasks.submit',
        'tasks.followup',
        'tasks.pause',
        'tasks.resume',
        'tasks.cancel',
        'tasks.link_existing',
        'assets.attach',
      }.contains(part.tool) ||
      part.output is! String) {
    return null;
  }
  try {
    final value = jsonDecode(part.output as String);
    if (value is! Map<String, dynamic> ||
        value['state'] !=
            (part.tool == 'tasks.link_existing' ? 'linked' : 'accepted') ||
        value['task_id'] is! String ||
        value['command_id'] is! String ||
        (value['task_id'] as String).isEmpty ||
        (value['command_id'] as String).isEmpty) {
      return null;
    }
    return (
      taskId: value['task_id'] as String,
      commandId: value['command_id'] as String,
    );
  } on FormatException {
    return null;
  }
}
