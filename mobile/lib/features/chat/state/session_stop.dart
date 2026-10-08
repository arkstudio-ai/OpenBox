import 'dart:convert';
import 'dart:math';

import 'package:crypto/crypto.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../../../shared/api/api_error.dart';
import '../../../shared/models/session.dart';
import '../api/assistant_api.dart';

/// Retain the exact observed stop through response loss and process restart.
/// Only control identities are stored; no chat or task text is persisted here.
Future<void> stopAssistantExecution({
  required Session session,
  required AssistantApi api,
  required SharedPreferences prefs,
  required bool Function() isCurrent,
}) async {
  final scope = api.scope;
  if (session.userId != scope.userId ||
      session.workspaceId != scope.workspaceId ||
      !isCurrent()) {
    throw StateError('Assistant scope changed');
  }
  final key =
      'session-stop:v1:${sha256.convert(utf8.encode(jsonEncode([scope.userId, scope.workspaceId, session.id])))}';
  final saved = prefs.getString(key);
  final target = session.taskControl;
  if (saved == null && target == null) {
    throw ApiError(
      status: 409,
      code: 'ASSISTANT_TASK_CONTROL_REQUIRED',
      message:
          'Reload this execution page or use its original task card to stop the task',
    );
  }
  final body = saved != null
      ? jsonDecode(saved) as Map<String, dynamic>
      : <String, dynamic>{
          'task_id': target!['task_id'],
          'expected_revision': target['expected_revision'],
          'expected_run': target['expected_run'],
          'idempotency_key':
              'stop-${List.generate(24, (_) => Random.secure().nextInt(256).toRadixString(16).padLeft(2, '0')).join()}',
        };
  if (!await prefs.setString(key, jsonEncode(body))) {
    throw StateError('Could not retain stop identity');
  }
  if (!isCurrent()) throw StateError('Assistant scope changed');
  try {
    final receipt = await api.stopExecution(session.id, body);
    final control = receipt['task_control'];
    if (receipt['ok'] != true ||
        control is! Map ||
        control['command_id'] is! String) {
      throw const FormatException('Unconfirmed task stop receipt');
    }
    if (isCurrent()) await prefs.remove(key);
  } catch (error) {
    final status = apiErrorOf(error)?.status ?? 0;
    if (isCurrent() &&
        status >= 400 &&
        status < 500 &&
        status != 408 &&
        status != 429) {
      await prefs.remove(key);
    }
    rethrow;
  }
}
