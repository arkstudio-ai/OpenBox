// Read-only production latency and invalid-token checks. No credentials needed.
import 'dart:convert';

import 'package:bossip_mobile/shared/config/env.dart';
import 'package:dio/dio.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:integration_test/integration_test.dart';

void main() {
  IntegrationTestWidgetsFlutterBinding.ensureInitialized();
  testWidgets(
    'Android reaches production and rejects unauthenticated requests',
    (tester) async {
      expect(Env.apiBase, 'https://ai.bossipai.com.cn');
      final dio = Dio(
        BaseOptions(
          connectTimeout: const Duration(seconds: 10),
          receiveTimeout: const Duration(seconds: 10),
          validateStatus: (_) => true,
        ),
      );
      addTearDown(dio.close);
      final metrics = <String, List<int>>{};
      final config = await dio.get<Map<String, dynamic>>(
        '${Env.apiBase}/api/auth/logto/config',
      );
      expect(config.statusCode, 200);
      expect(config.data?['endpoint'], 'https://auth.bossipai.com.cn');
      expect(config.data?['native_app_id'], isNotEmpty);
      for (final entry in {
        'production_config': '${Env.apiBase}/api/auth/logto/config',
        'logto_discovery':
            'https://auth.bossipai.com.cn/oidc/.well-known/openid-configuration',
      }.entries) {
        metrics[entry.key] = [];
        for (var i = 0; i < 10; i++) {
          final timer = Stopwatch()..start();
          final response = await dio.get<dynamic>(entry.value);
          timer.stop();
          expect(response.statusCode, 200);
          metrics[entry.key]!.add(timer.elapsedMilliseconds);
        }
      }
      final invalid = await dio.post<dynamic>(
        '${Env.apiBase}/api/auth/logto/id-token',
        data: {'id_token': 'android-smoke-invalid-token'},
      );
      expect(invalid.statusCode, 401);
      final unauthorized = await dio.get<dynamic>('${Env.apiBase}/api/auth/me');
      expect(unauthorized.statusCode, 401);
      debugPrint('ANDROID_PRODUCTION_METRICS ${jsonEncode(metrics)}');
    },
  );
}
