import 'dart:async';

import 'package:bossip_mobile/features/skills/api/skills_api.dart';
import 'package:bossip_mobile/shared/api/auth_store.dart';
import 'package:bossip_mobile/shared/models/auth_user.dart';
import 'package:bossip_mobile/shared/models/skill.dart';
import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

class _SwitchingSkillsApi extends SkillsApi {
  _SwitchingSkillsApi(this.bobSkills, this.bobServers) : super(Dio());

  final Completer<List<InstalledSkill>> bobSkills;
  final Completer<List<McpServer>> bobServers;
  int skillCalls = 0;
  int serverCalls = 0;

  @override
  Future<List<InstalledSkill>> listSkills() {
    skillCalls += 1;
    if (skillCalls == 1) {
      return Future.value(const [InstalledSkill(name: 'alice-only')]);
    }
    return bobSkills.future;
  }

  @override
  Future<List<McpServer>> listServers() {
    serverCalls += 1;
    if (serverCalls == 1) {
      return Future.value(const [
        McpServer(name: 'alice-mcp', type: 'stdio', status: 'connected'),
      ]);
    }
    return bobServers.future;
  }
}

Dio _catalogueDio(Map<String, dynamic> payload) {
  final dio = Dio(BaseOptions(baseUrl: 'https://openbox.invalid'));
  dio.interceptors.add(
    InterceptorsWrapper(
      onRequest: (options, handler) => handler.resolve(
        Response<Map<String, dynamic>>(
          requestOptions: options,
          statusCode: 200,
          data: payload,
        ),
      ),
    ),
  );
  return dio;
}

void main() {
  test(
    'A -> signed-out -> B never exposes A data through the current account',
    () async {
      final bobSkills = Completer<List<InstalledSkill>>();
      final bobServers = Completer<List<McpServer>>();
      final api = _SwitchingSkillsApi(bobSkills, bobServers);
      final container = ProviderContainer(
        overrides: [skillsApiProvider.overrideWithValue(api)],
      );
      addTearDown(container.dispose);

      container
          .read(authProvider.notifier)
          .setAuth('token-a', const AuthUser(id: 'alice', username: 'Alice'));
      final aliceId = container.read(skillsAccountProvider);
      final aliceSubscription = container.listen(
        installedSkillsProvider(aliceId),
        (_, _) {},
        fireImmediately: true,
      );
      addTearDown(aliceSubscription.close);
      final alice = await container.read(
        installedSkillsProvider(aliceId).future,
      );
      final aliceServers = await container.read(
        mcpServersProvider(aliceId).future,
      );
      expect(alice.single.name, 'alice-only');
      expect(aliceServers.single.name, 'alice-mcp');

      container.read(authProvider.notifier).clearAuth();
      final signedOutId = container.read(skillsAccountProvider);
      expect(signedOutId, isNull);
      expect(
        await container.read(installedSkillsProvider(signedOutId).future),
        isEmpty,
      );
      expect(
        await container.read(mcpServersProvider(signedOutId).future),
        isEmpty,
      );

      container
          .read(authProvider.notifier)
          .setAuth('token-b', const AuthUser(id: 'bob', username: 'Bob'));
      final bobId = container.read(skillsAccountProvider);
      final bobSubscription = container.listen(
        installedSkillsProvider(bobId),
        (_, _) {},
        fireImmediately: true,
      );
      addTearDown(bobSubscription.close);
      final pendingBob = container.read(installedSkillsProvider(bobId).future);
      final pendingBobServers = container.read(
        mcpServersProvider(bobId).future,
      );

      final loadingBob = container.read(installedSkillsProvider(bobId));
      final loadingBobServers = container.read(mcpServersProvider(bobId));
      expect(loadingBob.isLoading, isTrue);
      expect(loadingBob.valueOrNull, isNull);
      expect(loadingBobServers.isLoading, isTrue);
      expect(loadingBobServers.valueOrNull, isNull);
      expect(
        container
            .read(installedSkillsProvider(aliceId))
            .valueOrNull
            ?.single
            .name,
        'alice-only',
      );

      bobSkills.complete(const [InstalledSkill(name: 'bob-only')]);
      bobServers.complete(const [
        McpServer(name: 'bob-mcp', type: 'remote', status: 'connected'),
      ]);
      expect((await pendingBob).single.name, 'bob-only');
      expect((await pendingBobServers).single.name, 'bob-mcp');
      expect(api.skillCalls, 2);
      expect(api.serverCalls, 2);
    },
  );

  test('catalogue MCP status=error is surfaced as a failed install', () async {
    final api = SkillsApi(
      _catalogueDio({
        'ok': true,
        'installed': [
          {
            'kind': 'mcp',
            'id': 'memory',
            'name': 'memory',
            'status': 'error',
            'error': 'initialize timed out',
          },
        ],
      }),
    );

    await expectLater(
      api.installFromCatalog(id: 'memory', kind: 'mcp'),
      throwsA(
        isA<McpCatalogInstallException>().having(
          (error) => error.message,
          'message',
          contains(
            "MCP server 'memory' failed to connect: initialize timed out",
          ),
        ),
      ),
    );
  });

  test('catalogue MCP connected status remains a successful install', () async {
    final api = SkillsApi(
      _catalogueDio({
        'ok': true,
        'installed': [
          {
            'kind': 'mcp',
            'id': 'memory',
            'name': 'memory',
            'status': 'connected',
            'error': null,
          },
        ],
      }),
    );

    await api.installFromCatalog(id: 'memory', kind: 'mcp');
  });
}
