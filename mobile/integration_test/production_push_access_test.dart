import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:integration_test/integration_test.dart';

// Explicit opt-in. These requests carry no account credentials and cannot queue
// a push. They verify real emulator TLS/connectivity and deployed route guards.
void main() {
  IntegrationTestWidgetsFlutterBinding.ensureInitialized();
  const base = String.fromEnvironment('LIVE_API_BASE');
  testWidgets('emulator reaches deployed push routes over HTTPS', (
    tester,
  ) async {
    expect(Uri.parse(base).scheme, 'https');
    final dio = Dio(
      BaseOptions(
        baseUrl: base,
        followRedirects: false,
        connectTimeout: const Duration(seconds: 15),
        receiveTimeout: const Duration(seconds: 15),
        validateStatus: (_) => true,
        headers: {'X-Client-Type': 'mobile'},
      ),
    );
    try {
      final environment = await dio.get<dynamic>('/api/environment');
      expect(environment.statusCode, 200);
      expect(environment.data, isA<Map<String, dynamic>>());
      for (final endpoint in [
        ('GET', '/api/admin/push'),
        ('POST', '/api/admin/push/test'),
        ('POST', '/api/admin/push/messages/nonexistent/receipt'),
        ('POST', '/api/push/test'),
      ]) {
        final response = await dio.request<dynamic>(
          endpoint.$2,
          options: Options(method: endpoint.$1),
          data: endpoint.$1 == 'POST' ? <String, dynamic>{} : null,
        );
        expect(response.statusCode, 401, reason: endpoint.$2);
      }
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: Center(
              child: Text(
                'HTTPS: $base\nEnvironment: 200\nPush access guards: 4 × 401',
              ),
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();
      expect(find.textContaining('4 × 401'), findsOneWidget);
    } finally {
      dio.close(force: true);
    }
  }, skip: base.isEmpty);
}
