import 'dart:convert';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/auth_session.dart';
import '../../../shared/api/providers.dart';
import '../../../shared/api/workspace_scope.dart';

/// Session is a local view boundary. The API authorizes the actor's private
/// main/browser binding; it never accepts a client-selected execution Session.
typedef PrivateBrowserScope = ({
  String userId,
  String workspaceId,
  String sessionId,
  int authRevision,
});

const privateBrowserKeys = [
  'Enter',
  'Tab',
  'Backspace',
  'Delete',
  'Escape',
  'ArrowLeft',
  'ArrowRight',
  'ArrowUp',
  'ArrowDown',
  'Home',
  'End',
  'PageUp',
  'PageDown',
  'Space',
];
const privateBrowserOperations = {
  'capture',
  'navigate',
  'back',
  'reload',
  'mouse',
  'key',
  'text',
  'wheel',
};

Map<String, dynamic> browserMap(Object? value) {
  if (value is! Map<String, dynamic>) {
    throw const FormatException('Invalid browser response');
  }
  return value;
}

String _string(Map<String, dynamic> value, String key) {
  final result = value[key];
  if (result is! String || result.isEmpty) {
    throw const FormatException('Incomplete browser response');
  }
  return result;
}

class BrowserFence {
  BrowserFence.fromJson(Map<String, dynamic> json)
    : resourceId = _string(json, 'resource_id'),
      epoch = json['epoch'] is int ? json['epoch'] as int : 0,
      ownerKind = _string(json, 'owner_kind'),
      ownerId = _string(json, 'owner_id') {
    if (!RegExp(r'^[0-9a-f]{64}$').hasMatch(resourceId) ||
        epoch < 1 ||
        !{'automation', 'human'}.contains(ownerKind)) {
      throw const FormatException('Invalid browser fence');
    }
  }
  final String resourceId, ownerKind, ownerId;
  final int epoch;
  Map<String, dynamic> toJson() => {
    'resource_id': resourceId,
    'epoch': epoch,
    'owner_kind': ownerKind,
    'owner_id': ownerId,
  };
  @override
  bool operator ==(Object other) =>
      other is BrowserFence &&
      resourceId == other.resourceId &&
      epoch == other.epoch &&
      ownerKind == other.ownerKind &&
      ownerId == other.ownerId;
  @override
  int get hashCode => Object.hash(resourceId, epoch, ownerKind, ownerId);
}

class BrowserControlCommand {
  const BrowserControlCommand({
    required this.resourceId,
    required this.action,
    required this.expectedEpoch,
    required this.idempotencyKey,
    this.commandId,
  });
  final String resourceId, action, idempotencyKey;
  final int expectedEpoch;
  final String? commandId;

  factory BrowserControlCommand.fromSnapshot(
    String resourceId,
    Map<String, dynamic> json,
  ) {
    final action = _string(json, 'action');
    final epoch = json['expected_epoch'];
    if (!{'takeover', 'giveback', 'close'}.contains(action) ||
        epoch is! int ||
        epoch < 1) {
      throw const FormatException('Invalid original browser command');
    }
    return BrowserControlCommand(
      resourceId: resourceId,
      action: action,
      expectedEpoch: epoch,
      idempotencyKey: _string(json, 'idempotency_key'),
      commandId: _string(json, 'command_id'),
    );
  }

  Map<String, dynamic> toJson() => {
    'action': action,
    'expected_epoch': expectedEpoch,
    'idempotency_key': idempotencyKey,
  };
  bool sameRequest(BrowserControlCommand other) =>
      resourceId == other.resourceId &&
      action == other.action &&
      expectedEpoch == other.expectedEpoch &&
      idempotencyKey == other.idempotencyKey;
}

class BrowserSnapshot {
  BrowserSnapshot.fromJson(Map<String, dynamic> json)
    : resourceId = _string(json, 'resource_id'),
      fence = BrowserFence.fromJson(browserMap(json['fence'])),
      status = _string(json, 'status'),
      admission = _string(json, 'admission'),
      expiresAt = DateTime.tryParse(json['expires_at'] as String? ?? ''),
      remoteAvailable = json['remote_available'] == true,
      canTakeover = json['can_takeover'] == true,
      canGiveback = json['can_giveback'] == true,
      freshObservationRequired = json['fresh_observation_required'] != false,
      pending = json['pending_control'] == null
          ? null
          : BrowserControlCommand.fromSnapshot(
              _string(json, 'resource_id'),
              browserMap(json['pending_control']),
            ) {
    if (json['resource_type'] != 'browser_profile' ||
        resourceId != fence.resourceId ||
        !{'active', 'draining', 'hold'}.contains(status) ||
        !{'open', 'closed'}.contains(admission)) {
      throw const FormatException('Invalid browser binding');
    }
  }
  final String resourceId, status, admission;
  final BrowserFence fence;
  final DateTime? expiresAt;
  final bool remoteAvailable,
      canTakeover,
      canGiveback,
      freshObservationRequired;
  final BrowserControlCommand? pending;
}

class BrowserGrant {
  const BrowserGrant(this.fence, this.token, this.expiresAt);
  final BrowserFence fence;
  final String token;
  final DateTime expiresAt;
}

class BrowserFrame {
  BrowserFrame.fromReceipt(Map<String, dynamic> result, BrowserFence expected)
    : observation = Map.unmodifiable(browserMap(result['observation'])),
      bytes = _png(result['png_base64']) {
    if (observation['eligible'] != true ||
        BrowserFence.fromJson(browserMap(observation['fence'])) != expected ||
        observation['width'] != 1024 ||
        observation['height'] != 768 ||
        !RegExp(
          r'^[0-9a-f]{32}$',
        ).hasMatch(observation['observation_id'] as String? ?? '')) {
      throw const FormatException('Unavailable current browser observation');
    }
  }
  final Map<String, dynamic> observation;
  final Uint8List bytes;
  String get url =>
      observation['url'] is String ? observation['url'] as String : '';
  BrowserFence get fence =>
      BrowserFence.fromJson(browserMap(observation['fence']));

  static Uint8List _png(Object? value) {
    if (value is! String || value.length > 8 * 1024 * 1024) {
      throw const FormatException('Invalid browser image');
    }
    final bytes = base64Decode(value);
    const magic = [137, 80, 78, 71, 13, 10, 26, 10];
    if (bytes.length < magic.length ||
        List.generate(
          magic.length,
          (i) => bytes[i] == magic[i],
        ).contains(false)) {
      throw const FormatException('Invalid browser image');
    }
    return bytes;
  }
}

/// Only the authenticated backend's finite API. No native ticket, service key,
/// CDP, arbitrary proxy, terminal or persisted human token enters this client.
class PrivateBrowserApi {
  PrivateBrowserApi(this._dio, this._auth, this._workspace, this.scope);
  final Dio _dio;
  final AuthSession _auth;
  final WorkspaceScope _workspace;
  final PrivateBrowserScope scope;
  static const base = '/api/assistant/browser-resources';

  bool get isCurrent =>
      _auth.userId == scope.userId &&
      _auth.revision == scope.authRevision &&
      _workspace.currentId == scope.workspaceId;

  Options get _options {
    if (!isCurrent) throw StateError('Browser view scope changed');
    return Options(
      headers: {'X-Workspace-Id': scope.workspaceId},
      extra: {
        requestScopeUserKey: scope.userId,
        requestScopeWorkspaceKey: scope.workspaceId,
      },
      receiveTimeout: const Duration(seconds: 40),
    );
  }

  Future<Map<String, dynamic>> _request(
    String method,
    String path,
    CancelToken cancel, [
    Map<String, dynamic>? body,
  ]) async {
    final options = _options..method = method;
    final response = await _dio.request<Map<String, dynamic>>(
      path,
      data: body,
      options: options,
      cancelToken: cancel,
    );
    if (!isCurrent) throw StateError('Browser view scope changed');
    return browserMap(response.data);
  }

  Future<Map<String, dynamic>> session(CancelToken cancel) => _request(
    'GET',
    '/api/agent/session/${Uri.encodeComponent(scope.sessionId)}',
    cancel,
  );

  Future<BrowserSnapshot?> current(CancelToken cancel) async {
    final data = await _request('GET', '$base/current', cancel);
    return data['resource'] == null
        ? null
        : BrowserSnapshot.fromJson(browserMap(data['resource']));
  }

  Future<BrowserSnapshot> ensure(CancelToken cancel) async =>
      BrowserSnapshot.fromJson(
        await _request('POST', '$base/ensure', cancel, {}),
      );

  Future<Map<String, dynamic>> control(
    BrowserControlCommand command,
    CancelToken cancel,
  ) => _request(
    'POST',
    '$base/${Uri.encodeComponent(command.resourceId)}/control',
    cancel,
    command.toJson(),
  );

  Future<Map<String, dynamic>> operation(
    BrowserGrant grant,
    String id,
    String kind,
    Map<String, dynamic> args,
    CancelToken cancel,
  ) {
    if (!privateBrowserOperations.contains(kind)) {
      throw ArgumentError('Finite browser operation required');
    }
    return _request(
      'POST',
      '$base/${grant.fence.resourceId}/operations',
      cancel,
      {
        'fence': grant.fence.toJson(),
        'human_token': grant.token,
        'operation_id': id,
        'kind': kind,
        'args': args,
      },
    );
  }

  Future<Map<String, dynamic>> heartbeat(
    BrowserGrant grant,
    String id,
    CancelToken cancel,
  ) => _request('POST', '$base/${grant.fence.resourceId}/heartbeat', cancel, {
    'fence': grant.fence.toJson(),
    'human_token': grant.token,
    'command_id': id,
  });
}

final privateBrowserApiProvider = Provider.autoDispose
    .family<PrivateBrowserApi, PrivateBrowserScope>(
      (ref, scope) => PrivateBrowserApi(
        ref.watch(apiDioProvider),
        ref.watch(authSessionProvider),
        ref.watch(workspaceScopeProvider),
        scope,
      ),
    );
