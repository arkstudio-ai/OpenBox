import 'dart:async';

import 'package:bossip_mobile/features/workbench/api/private_browser_api.dart';
import 'package:bossip_mobile/features/workbench/state/private_browser_controller.dart';
import 'package:bossip_mobile/shared/api/workspace_scope.dart';
import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';

import 'private_browser_fixture.dart';

void main() {
  late BrowserServer server;
  late PrivateBrowserController controller;
  setUp(() {
    server = BrowserServer();
    controller = PrivateBrowserController(server.api);
  });
  tearDown(() {
    controller.dispose();
    server.dio.close();
  });

  test(
    'opening only reads; prepare and grant require explicit actions',
    () async {
      server.prepared = false;
      await controller.start();
      expect(server.requests.map((r) => r.method), ['GET']);
      expect(controller.canPrepare, isTrue);
      await controller.ensure();
      expect(server.ensures, 1);
      expect(server.controls, isEmpty);
      await controller.control('takeover');
      expect(controller.controlled, isTrue);
      expect(controller.frame, isNotNull);
      expect(server.operations.map((r) => r['kind']), ['capture']);
      for (final request in server.requests) {
        expect(request.headers['X-Workspace-Id'], browserScope.workspaceId);
        expect(request.extra[requestScopeUserKey], browserScope.userId);
        expect(
          request.extra[requestScopeWorkspaceKey],
          browserScope.workspaceId,
        );
        expect(request.path, isNot(contains('/desktop/ticket')));
        expect(request.queryParameters, isEmpty);
        expect(request.path, isNot(contains('only-in-request-body')));
      }
    },
  );

  test(
    'finite mouse/text/key/wheel/navigation drain then capture; explicit giveback',
    () async {
      await controller.start();
      await controller.control('takeover');
      for (final op in <(String, Map<String, dynamic>)>[
        ('mouse', {'x': 100, 'y': 50, 'button': 'left'}),
        ('text', {'text': 'synthetic'}),
        ('key', {'key': 'Enter'}),
        ('wheel', {'x': 512, 'y': 384, 'delta_x': 1, 'delta_y': -2}),
        ('navigate', {'url': 'https://example.test/'}),
        ('back', {}),
        ('reload', {}),
      ]) {
        await controller.operate(op.$1, op.$2);
        expect(controller.frame, isNotNull);
        expect(server.operations[server.operations.length - 2]['kind'], op.$1);
        expect(server.operations.last['kind'], 'capture');
      }
      final count = server.operations.length;
      await controller.operate('rawEval', {'js': 'no'});
      expect(server.operations, hasLength(count));
      await controller.control('giveback');
      expect(controller.controlled, isFalse);
      expect(controller.frame, isNull);
      expect(controller.resource!.fence.epoch, 3);
      expect(controller.resource!.freshObservationRequired, isTrue);
      expect(controller.givenBack, (resumed: 1, changed: 0));
      expect(server.operations, hasLength(count));
    },
  );

  test(
    'lost control response retains original request for explicit continuation only',
    () async {
      await controller.start();
      var lost = false;
      server.intercept = (request) async {
        if (request.path.endsWith('/control') && !lost) {
          lost = true;
          await server.handle(request);
          server.timeout(request);
        }
        return server.handle(request);
      };
      await controller.control('takeover');
      final original = controller.pending!;
      expect(controller.controlled, isFalse);
      await controller.refresh();
      expect(server.controls, hasLength(1));
      await controller.control('takeover', continueOriginal: true);
      expect(server.controls, hasLength(2));
      expect(server.controls.first, server.controls.last);
      expect(server.controls.last, original.toJson());
      expect(controller.pending, isNull);
      expect(controller.controlled, isTrue);
      expect(controller.resource!.fence.epoch, 2);
    },
  );

  test(
    'server pending command is restored exactly after reopening the view',
    () async {
      server.pending = {
        'action': 'takeover',
        'expected_epoch': 1,
        'idempotency_key': 'original-fixed-key',
        'command_id': 'command-1',
      };
      await controller.start();
      expect(server.controls, isEmpty);
      await controller.control('takeover', continueOriginal: true);
      expect(server.controls.single['idempotency_key'], 'original-fixed-key');
      expect(server.controls.single['expected_epoch'], 1);
      expect(controller.controlled, isTrue);
    },
  );

  test(
    'unknown input clears credentials and never retries on status or another input',
    () async {
      await controller.start();
      await controller.control('takeover');
      server.intercept = (request) async {
        if (request.path.endsWith('/operations') &&
            server.data(request)['kind'] == 'text') {
          await server.handle(request);
          server.timeout(request);
        }
        return server.handle(request);
      };
      await controller.operate('text', {'text': 'once'});
      final count = server.operations.length;
      expect(controller.unconfirmed, isTrue);
      expect(
        controller.unconfirmedOperationId,
        server.operations.last['operation_id'],
      );
      expect(controller.controlled, isFalse);
      expect(controller.frame, isNull);
      await controller.refresh();
      await controller.operate('text', {'text': 'once'});
      await controller.operate('capture');
      expect(server.operations, hasLength(count));
      expect(controller.unconfirmed, isTrue);
      expect(server.controls, hasLength(1));
      await controller.control('giveback');
      expect(controller.unconfirmed, isFalse);
      expect(controller.givenBack, isNotNull);
    },
  );

  for (final change in ['actor', 'workspace', 'revision']) {
    test('late capture cannot enter a changed $change scope', () async {
      await controller.start();
      final arrived = Completer<void>(), release = Completer<void>();
      server.intercept = (request) async {
        final result = await server.handle(request);
        if (request.path.endsWith('/operations')) {
          arrived.complete();
          await release.future;
        }
        return result;
      };
      final takeover = controller.control('takeover');
      await arrived.future;
      switch (change) {
        case 'actor':
          server.auth.userId = 'other';
        case 'workspace':
          server.workspace.currentId = 'other';
        case 'revision':
          server.auth.revision++;
      }
      release.complete();
      await takeover;
      expect(controller.frame, isNull);
      expect(controller.controlled, isFalse);
      final count = server.requests.length;
      await controller.operate('text', {'text': 'blocked'});
      expect(server.requests, hasLength(count));
    });
  }

  test('old status response cannot overwrite an accepted new epoch', () async {
    await controller.start();
    final arrived = Completer<void>(), release = Completer<void>();
    var first = true;
    server.intercept = (request) async {
      final result = await server.handle(request);
      if (request.path.endsWith('/current') && first) {
        first = false;
        arrived.complete();
        await release.future;
      }
      return result;
    };
    final read = controller.refresh();
    await arrived.future;
    await controller.control('takeover');
    release.complete();
    await read;
    expect(controller.resource!.fence.epoch, 2);
    expect(controller.controlled, isTrue);
    expect(controller.frame, isNotNull);
  });

  test(
    'a replacement resource is refused until the view is reopened',
    () async {
      await controller.start();
      await controller.control('takeover');
      server.resourceId = otherBrowserResourceId;
      await controller.refresh();
      expect(controller.error, isTrue);
      expect(controller.controlled, isFalse);
      expect(controller.canPrepare, isFalse);
      expect(controller.frame, isNull);
      final count = server.requests.length;
      await controller.ensure();
      expect(server.requests, hasLength(count));
    },
  );

  for (final wrong in ['fence', 'operation', 'observation', 'dimensions']) {
    test('capture with wrong $wrong cannot authorize later input', () async {
      await controller.start();
      server.intercept = (request) async {
        final result = await server.handle(request);
        if (request.path.endsWith('/operations')) {
          switch (wrong) {
            case 'fence':
              result['fence'] = {...server.fence, 'epoch': 1};
            case 'operation':
              result['operation_id'] = 'different';
            case 'observation':
              browserMap(browserMap(result['result'])['observation'])['fence'] =
                  {...server.fence, 'epoch': 1};
            case 'dimensions':
              browserMap(browserMap(result['result'])['observation'])['width'] =
                  800;
          }
        }
        return result;
      };
      await controller.control('takeover');
      expect(controller.controlled, isFalse);
      expect(controller.frame, isNull);
      final count = server.operations.length;
      await controller.operate('mouse', {'x': 1, 'y': 1, 'button': 'left'});
      expect(server.operations, hasLength(count));
    });
  }

  test(
    'expired recovered grant cannot produce a token, capture, or auto giveback',
    () async {
      await controller.start();
      server.intercept = (request) async {
        final result = await server.handle(request);
        if (request.path.endsWith('/control')) {
          result.remove('human_token');
          result['human_grant_expired'] = true;
          server.expiresAt = DateTime.now().subtract(
            const Duration(seconds: 1),
          );
          server.status = 'hold';
          server.admission = 'closed';
        }
        return result;
      };
      await controller.control('takeover');
      expect(controller.pending, isNull);
      expect(controller.controlled, isFalse);
      expect(controller.resource!.canGiveback, isTrue);
      expect(server.operations, isEmpty);
      expect(server.controls, hasLength(1));
    },
  );

  test(
    'API rejects changed scope before dispatch including an explicit heartbeat',
    () async {
      await controller.start();
      final grant = BrowserGrant(
        BrowserFence.fromJson(server.fence),
        'unused',
        DateTime.now(),
      );
      server.auth.userId = 'other';
      await expectLater(
        server.api.heartbeat(grant, 'old', CancelToken()),
        throwsStateError,
      );
      expect(server.requests, hasLength(1));
    },
  );

  test(
    'close is admitted during input; late input cannot restore a frame or busy state',
    () async {
      await controller.start();
      await controller.control('takeover');
      final arrived = Completer<void>(), release = Completer<void>();
      server.intercept = (request) async {
        final response = await server.handle(request);
        if (request.path.endsWith('/operations') &&
            server.data(request)['kind'] == 'text') {
          arrived.complete();
          await release.future;
        }
        return response;
      };
      final input = controller.operate('text', {'text': 'already-submitted'});
      await arrived.future;
      expect(controller.busy, isTrue);
      await controller.control('close');
      expect(server.controls.last['action'], 'close');
      expect(controller.resource!.status, 'hold');
      expect(controller.frame, isNull);
      expect(controller.busy, isTrue);
      final count = server.operations.length;
      release.complete();
      await input;
      expect(controller.busy, isFalse);
      expect(controller.controlling, isFalse);
      expect(controller.controlled, isFalse);
      expect(controller.frame, isNull);
      expect(controller.resource!.status, 'hold');
      expect(server.operations, hasLength(count));
    },
  );
}
