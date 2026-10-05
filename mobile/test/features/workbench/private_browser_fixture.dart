import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:bossip_mobile/features/workbench/api/private_browser_api.dart';
import 'package:bossip_mobile/shared/api/auth_session.dart';
import 'package:bossip_mobile/shared/api/workspace_scope.dart';
import 'package:dio/dio.dart';

const browserScope = (
  userId: 'owner',
  workspaceId: 'workspace',
  sessionId: 'private-session',
  authRevision: 0,
);
const browserResourceId =
    'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';
const otherBrowserResourceId =
    'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb';
const png =
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4nGP4DwQACfsD/fteaysAAAAASUVORK5CYII=';

/// All requests cross the actual Dart API/Dio adapter; there is no network
/// transport underneath this fixture, including unexpected paths.
class BrowserServer implements HttpClientAdapter {
  BrowserServer({this.scope = browserScope}) {
    auth.userId = scope.userId;
    auth.revision = scope.authRevision;
    workspace.currentId = scope.workspaceId;
    dio.httpClientAdapter = this;
    api = PrivateBrowserApi(dio, auth, workspace, scope);
  }
  final PrivateBrowserScope scope;
  final auth = AuthSession();
  final workspace = WorkspaceScope();
  final dio = Dio(BaseOptions(baseUrl: 'https://browser.invalid'));
  late final PrivateBrowserApi api;
  final requests = <RequestOptions>[];
  final controls = <Map<String, dynamic>>[];
  final operations = <Map<String, dynamic>>[];
  final receipts = <String, Map<String, dynamic>>{};
  FutureOr<Map<String, dynamic>> Function(RequestOptions options)? intercept;
  int epoch = 1, heartbeatCount = 0, ensures = 0;
  String ownerKind = 'automation', resourceId = browserResourceId;
  String status = 'active', admission = 'open';
  bool prepared = true, sessionPrivate = true;
  Duration ttl = const Duration(minutes: 2);
  DateTime? expiresAt;
  Map<String, dynamic>? pending;

  Map<String, dynamic> get fence => {
    'resource_id': resourceId,
    'epoch': epoch,
    'owner_kind': ownerKind,
    'owner_id': ownerKind == 'human' ? scope.userId : scope.workspaceId,
  };
  Map<String, dynamic> get snapshot => {
    'resource_id': resourceId,
    'resource_type': 'browser_profile',
    'fence': fence,
    'status': status,
    'admission': admission,
    'expires_at': expiresAt?.toUtc().toIso8601String(),
    'remote_available': true,
    'can_takeover': ownerKind == 'automation',
    'can_giveback': ownerKind == 'human',
    'fresh_observation_required': true,
    'pending_control': pending,
  };
  Map<String, dynamic> capture(String id) => {
    'operation_id': id,
    'state': 'completed',
    'fence': fence,
    'result': {
      'png_base64': png,
      'observation': {
        'fence': fence,
        'eligible': true,
        'width': 1024,
        'height': 768,
        'observation_id': 'cccccccccccccccccccccccccccccccc',
        'url': 'https://example.test/',
      },
    },
  };
  Map<String, dynamic> data(RequestOptions options) => browserMap(options.data);

  Future<Map<String, dynamic>> handle(RequestOptions request) async {
    final path = request.path;
    if (request.method == 'GET' && path.startsWith('/api/agent/session/')) {
      return {
        'id': scope.sessionId,
        'workspace_id': scope.workspaceId,
        'user_id': scope.userId,
        'kind': sessionPrivate ? 'assistant' : 'chat',
        'visibility': sessionPrivate ? 'private' : 'workspace',
      };
    }
    if (request.method == 'GET' &&
        path == '${PrivateBrowserApi.base}/current') {
      return {'resource': prepared ? snapshot : null};
    }
    if (request.method == 'POST' &&
        path == '${PrivateBrowserApi.base}/ensure') {
      ensures++;
      prepared = true;
      return snapshot;
    }
    if (request.method == 'POST' && path.endsWith('/control')) {
      final body = data(request);
      controls.add(Map.of(body));
      final key = body['idempotency_key'] as String;
      if (receipts[key] != null) return receipts[key]!;
      final action = body['action'] as String;
      if (action != 'close') epoch++;
      ownerKind = action == 'takeover'
          ? 'human'
          : action == 'giveback'
          ? 'automation'
          : ownerKind;
      expiresAt = ownerKind == 'human' ? DateTime.now().add(ttl) : null;
      admission = action == 'close' ? 'closed' : 'open';
      status = action == 'close' ? 'hold' : 'active';
      pending = null;
      return receipts[key] = {
        'command_id': 'command-${receipts.length + 1}',
        'state': 'applied',
        'fence': fence,
        if (action == 'takeover') ...{
          'human_token': 'only-in-request-body',
          'expires_at': expiresAt!.toUtc().toIso8601String(),
        },
        if (action == 'giveback') ...{
          'resume_requested_task_ids': ['original-task'],
          'task_control_changed_ids': <String>[],
        },
      };
    }
    if (request.method == 'POST' && path.endsWith('/operations')) {
      final body = data(request);
      operations.add(Map.of(body));
      return body['kind'] == 'capture'
          ? capture(body['operation_id'] as String)
          : {
              'operation_id': body['operation_id'],
              'state': 'completed',
              'fence': fence,
              'result': {'delivered': true},
            };
    }
    if (request.method == 'POST' && path.endsWith('/heartbeat')) {
      heartbeatCount++;
      expiresAt = DateTime.now().add(ttl);
      return {
        'fence': fence,
        'expires_at': expiresAt!.toUtc().toIso8601String(),
      };
    }
    throw StateError('Unexpected offline request: ${request.method} $path');
  }

  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) async {
    requests.add(options);
    final value = await (intercept?.call(options) ?? handle(options));
    return ResponseBody.fromString(
      jsonEncode(value),
      200,
      headers: {
        Headers.contentTypeHeader: [Headers.jsonContentType],
      },
    );
  }

  @override
  void close({bool force = false}) {}

  Never timeout(RequestOptions options) => throw DioException(
    requestOptions: options,
    type: DioExceptionType.receiveTimeout,
    message: 'Synthetic response loss; no network transport',
  );
}
