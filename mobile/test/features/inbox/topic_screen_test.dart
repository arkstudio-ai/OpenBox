import 'package:bossip_mobile/features/inbox/topic_screen.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

I18nBundle _bundle() => I18nBundle({
  'en-US': {
    'inbox': {
      'topic': {
        'title': 'Topic',
        'loadFailed': 'Could not load this topic.',
        'notFound': 'This topic does not exist or was taken down.',
      },
    },
    'common': {
      'action': {'retry': 'Retry'},
    },
  },
});

Dio _failingDio(int status) {
  final dio = Dio(BaseOptions(baseUrl: 'https://test.invalid'));
  dio.interceptors.add(
    InterceptorsWrapper(
      onRequest: (options, handler) => handler.reject(
        DioException(
          requestOptions: options,
          response: Response<dynamic>(
            requestOptions: options,
            statusCode: status,
          ),
          type: DioExceptionType.badResponse,
        ),
      ),
    ),
  );
  return dio;
}

Future<void> _pump(WidgetTester tester, int status) async {
  SharedPreferences.setMockInitialValues({'bossip:lang': 'en-US'});
  final prefs = await SharedPreferences.getInstance();
  await tester.pumpWidget(
    ProviderScope(
      overrides: [
        i18nProvider.overrideWith(() => I18nController(_bundle(), prefs)),
        apiDioProvider.overrideWithValue(_failingDio(status)),
      ],
      child: MaterialApp(
        theme: ThemeData(
          extensions: [
            BossipTokens.resolve(BossipThemeName.default_, Brightness.light),
          ],
        ),
        home: const TopicScreen(slug: 'spring-sale'),
      ),
    ),
  );
  // pump, not pumpAndSettle: the i18n controller keeps a listener alive and
  // the loading spinner never settles.
  for (var i = 0; i < 20; i++) {
    if (find.byType(CircularProgressIndicator).evaluate().isEmpty) break;
    await tester.pump(const Duration(milliseconds: 10));
  }
}

void main() {
  testWidgets('a topic that is gone says so and offers no retry', (
    tester,
  ) async {
    await _pump(tester, 404);

    expect(
      find.text('This topic does not exist or was taken down.'),
      findsOneWidget,
    );
    expect(find.text('Retry'), findsNothing);
  });

  testWidgets('a failed request can be retried', (tester) async {
    await _pump(tester, 502);

    expect(find.text('Could not load this topic.'), findsOneWidget);
    expect(find.text('Retry'), findsOneWidget);
  });
}
