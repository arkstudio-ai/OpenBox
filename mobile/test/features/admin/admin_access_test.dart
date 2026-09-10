import 'dart:async';

import 'package:bossip_mobile/app/admin_route.dart';
import 'package:bossip_mobile/features/admin/api/admin_api.dart';
import 'package:bossip_mobile/features/billing/state/billing_providers.dart';
import 'package:bossip_mobile/features/workspace/state/active_workspace_store.dart';
import 'package:bossip_mobile/features/workspace/widgets/user_row.dart';
import 'package:bossip_mobile/shared/api/api_error.dart';
import 'package:bossip_mobile/shared/api/auth_store.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/models/auth_user.dart';
import 'package:bossip_mobile/shared/models/billing.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'admin_test_support.dart';

class _Auth extends AuthController {
  _Auth(this.role);
  final String role;
  @override
  AuthState build() => AuthState(
    isLoading: false,
    user: AuthUser(id: 'operator', username: 'Operator', role: role),
  );
  void revoke() => state = const AuthState(
    isLoading: false,
    user: AuthUser(id: 'operator', username: 'Operator'),
  );
}

class _Workspace extends ActiveWorkspaceController {
  @override
  Future<ActiveWorkspaceState> build() async =>
      const ActiveWorkspaceState(currentId: 'scope-a');
}

void main() {
  setUpAll(() async => adminBundle = await I18nBundle.load());
  test(
    'scoped API survives reads but is disposed with its route container',
    () async {
      final h = AdminHarness();
      final root = ProviderContainer(
        overrides: [
          apiDioProvider.overrideWithValue(h.dio),
          authSessionProvider.overrideWithValue(h.auth),
          workspaceScopeProvider.overrideWithValue(h.workspace),
          authProvider.overrideWith(() => _Auth('admin')),
        ],
      );
      final child = ProviderContainer(
        parent: root,
        overrides: [adminScopeProvider.overrideWithValue(testScope)],
      );
      final api = child.read(adminApiProvider);
      await api.skillStore({}, CancelToken());
      await Future<void>.delayed(Duration.zero);
      await api.skillStore({}, CancelToken());
      expect(h.requests.length, 2);
      child.dispose();
      expect(api.checkAccess, throwsA(isA<ApiError>()));
      root.dispose();
      h.dispose();
    },
  );
  for (final role in ['user', 'owner', 'admin']) {
    testWidgets('account menu shows platform admin only: $role', (
      tester,
    ) async {
      final h = AdminHarness();
      await mountAdmin(
        tester,
        h,
        Align(
          alignment: Alignment.bottomCenter,
          child: UserRow(sessionCount: 1, onSignOut: () {}),
        ),
        overrides: [
          authProvider.overrideWith(() => _Auth(role)),
          billingBalanceProvider.overrideWith(
            (ref) async => const CreditBalance(
              workspaceId: 'scope-a',
              balance: '123',
              mode: 'charge',
            ),
          ),
        ],
      );
      await tester.tap(find.byTooltip('更多'));
      await tester.pumpAndSettle();
      expect(
        find.byKey(const ValueKey('admin-console-menu')),
        role == 'admin' ? findsOneWidget : findsNothing,
      );
      if (role == 'admin') {
        final container = ProviderScope.containerOf(
          tester.element(find.byType(UserRow)),
        );
        (container.read(authProvider.notifier) as _Auth).revoke();
        await tester.pumpAndSettle();
        expect(find.byKey(const ValueKey('admin-console-menu')), findsNothing);
      }
      expect(tester.takeException(), isNull);
    });
  }
  testWidgets(
    'guarded route removes pending admin reads after role revocation',
    (tester) async {
      final h = AdminHarness();
      final pending = Completer<dynamic>();
      h.responder = (_) => pending.future;
      await mountAdmin(
        tester,
        h,
        const AdminRoute(),
        settle: false,
        overrides: [
          authProvider.overrideWith(() => _Auth('admin')),
          activeWorkspaceProvider.overrideWith(_Workspace.new),
          workspaceScopeProvider.overrideWithValue(h.workspace),
          apiDioProvider.overrideWithValue(h.dio),
          authSessionProvider.overrideWithValue(h.auth),
        ],
      );
      for (var frame = 0; frame < 5; frame++) {
        await tester.pump(const Duration(milliseconds: 100));
      }
      expect(tester.takeException(), isNull);
      expect(
        h.requests,
        isNotEmpty,
        reason: find
            .byType(Text)
            .evaluate()
            .map((e) => (e.widget as Text).data)
            .join(' | '),
      );
      final container = ProviderScope.containerOf(
        tester.element(find.byType(AdminRoute)),
      );
      (container.read(authProvider.notifier) as _Auth).revoke();
      await tester.pumpAndSettle();
      expect(
        h.requests.every((r) => r.cancelToken?.isCancelled ?? false),
        isTrue,
      );
      pending.complete(testPage([]));
      await tester.pumpAndSettle();
      expect(find.text('仅平台超级管理员可访问。'), findsOneWidget);
      expect(tester.takeException(), isNull);
    },
  );
}
