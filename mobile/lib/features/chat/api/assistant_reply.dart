import 'dart:convert';
import 'dart:math';

import 'package:crypto/crypto.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../../../shared/api/api_error.dart';
import '../../../shared/api/providers.dart';
import 'assistant_api.dart';

/// Shared by the main entry and the original execution page. The exact
/// request revision and user choice survive an unknown transport outcome;
/// the persisted record contains a digest, never answer text.
class AssistantReplyService {
  AssistantReplyService(this.prefs, this.currentScope, this.api);
  final SharedPreferences prefs;
  final AssistantScope? Function() currentScope;
  final AssistantApi Function(AssistantScope) api;
  final _inFlight = <String>{};

  Future<Map<String, dynamic>> reply({
    required String kind,
    required String id,
    required Map<String, dynamic> binding,
    required Map<String, dynamic> answer,
    bool reject = false,
  }) async {
    final scope = currentScope();
    if (scope == null ||
        binding['workspace_id'] != scope.workspaceId ||
        binding['assistant_session_id'] is! String ||
        (binding['request_revision'] as String?)?.length != 64 ||
        (binding['options_hash'] as String?)?.length != 64 ||
        !const {'question', 'permission'}.contains(kind)) {
      throw StateError('Assistant request scope or version changed');
    }
    final scopeKey = jsonEncode([
      scope.userId,
      scope.workspaceId,
      binding['assistant_session_id'],
      kind,
      id,
    ]);
    final key = 'assistant-reply-v1:${sha256.convert(utf8.encode(scopeKey))}';
    if (!_inFlight.add(key)) {
      throw StateError('Reply is already being submitted');
    }
    try {
      final digest = sha256
          .convert(utf8.encode(jsonEncode([reject, answer])))
          .toString();
      final saved = prefs.getString(key);
      final pending = saved == null
          ? null
          : jsonDecode(saved) as Map<String, dynamic>;
      if (pending != null && pending['digest'] != digest) {
        throw ApiError(
          status: 409,
          code: 'ASSISTANT_SEND_UNCERTAIN',
          message: 'Retry the original pending reply',
        );
      }
      final replyId =
          pending?['id'] ??
          'mobile-${List.generate(24, (_) => Random.secure().nextInt(256).toRadixString(16).padLeft(2, '0')).join()}';
      final body = {
        ...answer,
        'expected_request_revision':
            pending?['revision'] ?? binding['request_revision'],
        'options_hash': pending?['options_hash'] ?? binding['options_hash'],
        'source_ref': {'kind': 'card'},
      };
      if (!await prefs.setString(
        key,
        jsonEncode({
          'id': replyId,
          'digest': digest,
          'revision': body['expected_request_revision'],
          'options_hash': body['options_hash'],
        }),
      )) {
        throw StateError('Could not persist reply identity');
      }
      if (currentScope() != scope) {
        throw StateError('Assistant request scope changed');
      }
      try {
        final receipt = await api(
          scope,
        ).reply(kind, id, {...body, 'reply_id': replyId}, reject: reject);
        if (receipt['command_id'] is! String ||
            receipt['reply_id'] != replyId ||
            receipt['request_id'] != id ||
            receipt['request_kind'] != kind ||
            receipt['assistant_session_id'] !=
                binding['assistant_session_id'] ||
            receipt['request_revision'] != body['expected_request_revision'] ||
            receipt['options_hash'] != body['options_hash'] ||
            !const {
              'accepted',
              'applying',
              'applied',
              'failed',
            }.contains(receipt['state'])) {
          throw const FormatException('Unconfirmed request receipt');
        }
        await prefs.remove(key);
        if (currentScope() != scope) {
          throw StateError('Assistant request scope changed');
        }
        return receipt;
      } catch (error) {
        final status = apiErrorOf(error)?.status ?? 0;
        if (status >= 400 && status < 500 && status != 408 && status != 429) {
          await prefs.remove(key);
        }
        rethrow;
      }
    } finally {
      _inFlight.remove(key);
    }
  }
}

final assistantReplyProvider = Provider<AssistantReplyService>(
  (ref) => AssistantReplyService(ref.read(prefsProvider), () {
    final user = ref.read(authSessionProvider).userId;
    final workspace = ref.read(workspaceScopeProvider).currentId;
    return user == null || workspace == null
        ? null
        : (userId: user, workspaceId: workspace);
  }, (scope) => ref.read(assistantApiProvider(scope))),
);

String replyStateKey(Object? state) => switch (state) {
  'applied' => 'chat:assistant.requests.applied',
  'applying' => 'chat:assistant.requests.applying',
  'failed' => 'chat:assistant.requests.failed',
  _ => 'chat:assistant.requests.accepted',
};
