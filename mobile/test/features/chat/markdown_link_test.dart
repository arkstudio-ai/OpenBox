import 'package:bossip_mobile/features/chat/widgets/chat_link.dart';
import 'package:bossip_mobile/features/chat/widgets/markdown_view.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';

import '../voice/voice_fakes.dart';

void main() {
  test('conversation URLs keep session, query and anchor in native routes', () {
    for (final prefix in ['', 'https://ai.bossipai.com.cn']) {
      expect(
        conversationRoute('$prefix/app/s/session_123?message=m1#video'),
        '/app/s/session_123?message=m1#video',
      );
    }
    for (final url in [
      'https://ai.bossipai.com.cn.evil.test/app/s/session_123',
      'https://user@ai.bossipai.com.cn/app/s/session_123',
      '//evil.test/app/s/session_123',
      '/app/s/hello%2fother',
      '/app/s/',
      'javascript:alert(1)',
    ]) {
      expect(conversationRoute(url), isNull, reason: url);
    }
  });

  test('Light topic URLs become native routes, other Light paths do not', () {
    for (final prefix in ['', 'https://ai.bossipai.com.cn']) {
      expect(
        conversationRoute('$prefix/light/topic/footage_edit'),
        '/light/topic/footage_edit',
      );
    }
    for (final url in [
      'https://ai.bossipai.com.cn.evil.test/light/topic/video',
      '//evil.test/light/topic/video',
      '/light/topic/',
      '/light/topic/a%2fb',
      '/light/topic/video/extra',
      '/light/autopilot',
      '/light/other/video',
    ]) {
      expect(conversationRoute(url), isNull, reason: url);
    }
  });

  testWidgets('tap an assistant topic link opens the topic page in the App', (
    tester,
  ) async {
    final router = GoRouter(
      initialLocation: '/app/assistant',
      routes: [
        GoRoute(
          path: '/app/assistant',
          builder: (_, _) => const Scaffold(
            body: MarkdownView('去首页[你拍我剪](/light/topic/footage_edit)开始'),
          ),
        ),
        GoRoute(
          path: '/light/topic/:entryId',
          builder: (_, state) =>
              Scaffold(body: Text('专题 ${state.pathParameters['entryId']}')),
        ),
      ],
    );
    addTearDown(router.dispose);
    await tester.pumpWidget(
      MaterialApp.router(theme: testTheme(), routerConfig: router),
    );
    await tester.pumpAndSettle();
    await tester.tapOnText(find.textRange.ofSubstring('你拍我剪'));
    await tester.pumpAndSettle();
    expect(find.text('专题 footage_edit'), findsOneWidget);
  });

  for (final prefix in ['', 'https://ai.bossipai.com.cn']) {
    testWidgets('tap the blue preview link opens its conversation ($prefix)', (
      tester,
    ) async {
      final router = GoRouter(
        initialLocation: '/app/assistant',
        routes: [
          GoRoute(
            path: '/app/assistant',
            builder: (_, _) => Scaffold(
              body: MarkdownView('[去会话里预览视频]($prefix/app/s/session_video)'),
            ),
          ),
          GoRoute(
            path: '/app/s/:sessionId',
            builder: (_, state) =>
                Scaffold(body: Text('会话 ${state.pathParameters['sessionId']}')),
          ),
        ],
      );
      addTearDown(router.dispose);
      await tester.pumpWidget(
        MaterialApp.router(theme: testTheme(), routerConfig: router),
      );
      await tester.pumpAndSettle();
      await tester.tap(find.text('去会话里预览视频', findRichText: true));
      await tester.pumpAndSettle();
      expect(find.text('会话 session_video'), findsOneWidget);
      router.pop();
      await tester.pumpAndSettle();
      expect(find.text('去会话里预览视频', findRichText: true), findsOneWidget);
    });
  }

  testWidgets('knowledge readers keep their own link handler', (tester) async {
    String? selected;
    await tester.pumpWidget(
      MaterialApp(
        theme: testTheme(),
        home: Scaffold(
          body: MarkdownView(
            '[引用](/app/s/source)',
            onLinkTap: (url, _) => selected = url,
          ),
        ),
      ),
    );
    await tester.tap(find.text('引用', findRichText: true));
    expect(selected, '/app/s/source');
  });
}
