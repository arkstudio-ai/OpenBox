import 'dart:async';
import 'dart:typed_data';

import 'package:bossip_mobile/features/admin/api/admin_api.dart';
import 'package:bossip_mobile/features/admin/models/admin_data.dart';
import 'package:bossip_mobile/shared/api/api_error.dart';
import 'package:bossip_mobile/shared/api/workspace_scope.dart';
import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';

import 'admin_test_support.dart';

void main() {
  late AdminHarness h;
  setUp(() => h = AdminHarness());
  tearDown(() => h.dispose());
  test(
    'only global admin in original identity/workspace can request',
    () async {
      h.allowed = false;
      await expectLater(
        h.api.skillStore({}, CancelToken()),
        throwsA(isA<ApiError>()),
      );
      h.allowed = true;
      h.workspace.currentId = 'scope-b';
      await expectLater(
        h.api.skillStore({}, CancelToken()),
        throwsA(isA<ApiError>()),
      );
      h.workspace.currentId = testScope.workspaceId;
      h.auth.userId = 'another-user';
      await expectLater(
        h.api.skillStore({}, CancelToken()),
        throwsA(isA<ApiError>()),
      );
      expect(h.requests, isEmpty);
    },
  );
  test('late responses are rejected after role revocation', () async {
    final pending = Completer<dynamic>();
    h.responder = (_) => pending.future;
    final future = h.api.orders({}, CancelToken());
    await Future<void>.delayed(Duration.zero);
    h.allowed = false;
    pending.complete(testPage([]));
    await expectLater(future, throwsA(isA<ApiError>()));
  });
  test(
    'disposal cancels shared token until every fleet request ends',
    () async {
      final pending = Completer<dynamic>();
      h.responder = (o) => o.path.endsWith('/pool')
          ? {'states': <String, int>{}}
          : pending.future;
      final cancel = CancelToken();
      final future = h.api.fleet(cancel);
      final expectation = expectLater(future, throwsA(isA<DioException>()));
      await Future<void>.delayed(Duration.zero);
      h.api.dispose();
      expect(cancel.isCancelled, isTrue);
      pending.complete(testPage([]));
      await expectation;
    },
  );
  test(
    'requests pin scope and disable redirects, billing remains GET',
    () async {
      await h.api.subscriptions({
        'plan': 'pro',
        'offset': 25,
        'limit': 25,
      }, CancelToken());
      await h.api.orders({
        'from': '2026-09-01',
        'to': '2026-09-10',
      }, CancelToken());
      await h.api.workspaceBilling('tenant:one', CancelToken());
      await h.api.paymentProviders(CancelToken());
      expect(
        h.requests.every((o) => o.method == 'GET' && !o.followRedirects),
        isTrue,
      );
      expect(h.requests.first.headers['X-Workspace-Id'], 'scope-a');
      expect(h.requests.first.extra[requestScopeUserKey], 'operator');
      expect(h.requests.first.extra[requestScopeWorkspaceKey], 'scope-a');
      expect(h.requests[1].queryParameters['from'], '2026-09-01');
      expect(h.requests[2].path, '/api/admin/billing/workspaces/tenant%3Aone');
      expect(h.requests.last.path, '/api/billing/providers');
    },
  );
  test(
    'fleet operations preserve approved targets and dry-run flags',
    () async {
      await h.api.ensurePool(true, CancelToken());
      await h.api.desktopAction('d:one', 'recycle', CancelToken());
      await h.api.adoptDesktop(
        ' d:two ',
        poolState: 'reserve',
        rebuild: false,
        gatewayReleaseVerified: true,
        cancel: CancelToken(),
      );
      expect(h.requests[0].queryParameters, {'dry_run': true});
      expect(h.requests[1].path, '/api/admin/fleet/desktops/d%3Aone/recycle');
      expect(h.requests[1].data, {'approve': true});
      expect(h.requests[2].data, {
        'pool_state': 'reserve',
        'rebuild': false,
        'approve': false,
        'gateway_release_verified': true,
      });
      await expectLater(
        h.api.desktopAction('d', 'delete', CancelToken()),
        throwsArgumentError,
      );
    },
  );
  test(
    'listing/review require reason, community identity not origin',
    () async {
      await expectLater(
        h.api.setListing('community:a', 'delisted', ' ', CancelToken()),
        throwsArgumentError,
      );
      await expectLater(
        h.api.reviewSkill('community:a', false, '', CancelToken()),
        throwsArgumentError,
      );
      await expectLater(
        h.api.reviewSkill('skill:a', true, '', CancelToken()),
        throwsArgumentError,
      );
      await expectLater(
        h.api.setOfficial('skill:a', true, CancelToken()),
        throwsArgumentError,
      );
      expect(h.requests, isEmpty);
      await h.api.setListing(
        'community:a/b',
        'delisted',
        ' audit ',
        CancelToken(),
      );
      expect(
        h.requests.single.path,
        '/api/admin/skills/store/community%3Aa%2Fb/listing',
      );
      expect(h.requests.single.data, {'listing': 'delisted', 'note': 'audit'});
    },
  );
  test(
    'edits carry captured revision and do not fabricate unchanged content',
    () async {
      await h.api.saveSkill(
        id: 'community:a',
        name: 'fixed',
        kind: 'skill',
        revision: 7,
        fields: {'title': 'Updated'},
        cancel: CancelToken(),
      );
      expect(h.requests.single.method, 'PATCH');
      expect(h.requests.single.data, {
        'title': 'Updated',
        'expected_revision': 7,
      });
    },
  );
  test('ZIP upload uses files multipart and checks per-file failure', () async {
    h.responder = (o) {
      expect(o.data, isA<FormData>());
      expect((o.data as FormData).files.single.key, 'files');
      expect((o.data as FormData).files.single.value.length, 4);
      expect(o.contentType, startsWith('multipart/form-data'));
      return {
        'items': [
          {'ok': false, 'error': 'duplicate'},
        ],
      };
    };
    await expectLater(
      h.api.uploadSkill(
        filename: 'skill.zip',
        length: 4,
        openRead: () => Stream.value([80, 75, 3, 4]),
        cancel: CancelToken(),
      ),
      throwsA(isA<ApiError>()),
    );
  });
  test(
    'batch deletion preserves partial results, restore stays explicit',
    () async {
      h.responder = (_) => testPage([
        {'catalog_id': 'skill:a', 'ok': true},
        {'catalog_id': 'skill:b', 'ok': false, 'error': 'conflict'},
      ]);
      final result = await h.api.deleteSkills(
        ['skill:a', 'skill:b'],
        'audit',
        CancelToken(),
      );
      expect(result.items.last.flag('ok'), isFalse);
      expect(h.requests.single.data, {
        'catalog_ids': ['skill:a', 'skill:b'],
        'reason': 'audit',
      });
      await h.api.restoreSkill('skill:a', CancelToken());
      expect(h.requests.last.path.endsWith('/restore'), isTrue);
    },
  );
  test(
    'protected installs refuse writes and removable targets remain exact',
    () async {
      await expectLater(
        h.api.uninstallSkill(
          'desktop',
          'member-1',
          const AdminRecord({'removable': false}),
          'audit',
          CancelToken(),
        ),
        throwsArgumentError,
      );
      expect(h.requests, isEmpty);
      await h.api.scanDesktop('desktop', 'member-2', CancelToken());
      expect(h.requests.single.queryParameters, {'user_id': 'member-2'});
      await h.api.uninstallSkill(
        'desktop',
        'member-1',
        const AdminRecord(testSkill),
        ' audit ',
        CancelToken(),
      );
      expect(h.requests.last.data, {
        'user_id': 'member-1',
        'kind': 'skill',
        'install_dir': '/home/member-1/skills/bundle',
        'reason': 'audit',
      });
    },
  );
  test(
    'review archive streams exact bytes and stops on scope change',
    () async {
      final chunks = StreamController<Uint8List>();
      h.responder = (_) => ResponseBody(chunks.stream, 200);
      final cancel = CancelToken();
      final stream = await h.api.reviewArchive('community:a', cancel);
      final values = <int>[];
      final done = Completer<void>();
      stream.listen(
        values.addAll,
        onError: (Object error) {
          expect(error, isA<ApiError>());
          done.complete();
        },
      );
      chunks.add(Uint8List.fromList([80, 75]));
      await Future<void>.delayed(Duration.zero);
      h.allowed = false;
      chunks.add(Uint8List.fromList([3, 4]));
      await done.future;
      await chunks.close();
      expect(values, [80, 75]);
      expect(h.requests.single.responseType, ResponseType.stream);
    },
  );
  test('money preserves integer fen and unknown currencies', () {
    expect(
      adminMoney(const AdminRecord({'amount_fen': 1, 'currency': 'CNY'})),
      '¥0.01',
    );
    expect(
      adminMoney(
        const AdminRecord({'amount_fen': -1234567, 'currency': 'XYZ'}),
      ),
      '-XYZ 12345.67',
    );
    expect(adminMoney(const AdminRecord({'amount_fen': null})), '—');
    expect(
      const AdminRecord({'balance': '0.000000001'}).string('balance'),
      '0.000000001',
    );
    expect(adminFilters({'plan': 'all', 'q': ' foo ', 'from': ''}), {
      'q': 'foo',
    });
  });
}
