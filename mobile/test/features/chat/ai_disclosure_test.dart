import 'package:bossip_mobile/features/chat/utils/content_origin.dart';
import 'package:bossip_mobile/features/chat/widgets/ai_disclosure.dart';
import 'package:bossip_mobile/features/legal/legal_page.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/models/message_part.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:shared_preferences/shared_preferences.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  test(
    'watermarks require generation provenance, not merely an agent attachment',
    () {
      for (final kind in [
        'generated_image',
        'video_segment',
        'video_final',
        'generated_audio',
      ]) {
        expect(
          isGeneratedMedia(
            FilePart(
              id: 'f',
              path: 'file',
              relation: FileRelation(kind: kind),
            ),
          ),
          isTrue,
        );
      }
      for (final kind in [
        null,
        'shared_file',
        'qr_code',
        'computer_screenshot',
      ]) {
        expect(
          isGeneratedMedia(
            FilePart(
              id: 'f',
              path: 'file',
              relation: FileRelation(kind: kind),
            ),
          ),
          isFalse,
        );
      }
      expect(
        isGeneratedMedia(
          const FilePart(
            id: 'f',
            path: 'file',
            transient: true,
            relation: FileRelation(kind: 'generated_image'),
          ),
        ),
        isFalse,
      );
      expect(
        isGeneratedMedia(
          const FilePart(
            id: 'f',
            path: 'file',
            relation: FileRelation(kind: 'generated_image', role: 'evidence'),
          ),
        ),
        isFalse,
      );
    },
  );

  testWidgets(
    'the persistent notice opens a public document and returns to the conversation',
    (tester) async {
      tester.view.physicalSize = const Size(375, 812);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      SharedPreferences.setMockInitialValues({'bossip:lang': 'zh-CN'});
      final prefs = await SharedPreferences.getInstance();
      final bundle = await I18nBundle.load();
      final router = GoRouter(
        initialLocation: '/conversation',
        routes: [
          GoRoute(
            path: '/conversation',
            builder: (_, _) => const Scaffold(
              body: Align(
                alignment: Alignment.bottomCenter,
                child: AiDisclosure(),
              ),
            ),
          ),
          GoRoute(
            path: '/legal/ai',
            builder: (_, _) => const LegalPage(document: 'ai'),
          ),
        ],
      );
      addTearDown(router.dispose);
      await tester.pumpWidget(
        ProviderScope(
          overrides: [
            i18nProvider.overrideWith(() => I18nController(bundle, prefs)),
          ],
          child: MaterialApp.router(
            routerConfig: router,
            theme: ThemeData(
              extensions: [
                BossipTokens.resolve(
                  BossipThemeName.default_,
                  Brightness.light,
                ),
              ],
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();
      expect(find.text('内容由 AI 生成，请注意核实。'), findsOneWidget);
      await tester.tap(find.byTooltip('AI 服务说明'));
      await tester.pumpAndSettle();
      expect(find.text('公开文档 · 无需登录'), findsOneWidget);
      expect(find.text('关于生成内容'), findsOneWidget);
      expect(tester.takeException(), isNull);
      await tester.tap(find.byTooltip('返回'));
      await tester.pumpAndSettle();
      expect(find.text('内容由 AI 生成，请注意核实。'), findsOneWidget);
    },
  );
}
