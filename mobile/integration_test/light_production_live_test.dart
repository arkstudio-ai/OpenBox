import 'dart:io';

import 'package:bossip_mobile/app/app.dart';
import 'package:bossip_mobile/app/router.dart';
import 'package:bossip_mobile/features/auth/state/auth_flow.dart';
import 'package:bossip_mobile/features/chat/chat_screen.dart';
import 'package:bossip_mobile/features/chat/state/chat_session_controller.dart';
import 'package:bossip_mobile/features/light/production/production_api.dart';
import 'package:bossip_mobile/features/light/production/production_media.dart';
import 'package:bossip_mobile/features/light/production/production_page.dart';
import 'package:bossip_mobile/main.dart' as app;
import 'package:bossip_mobile/shared/api/auth_store.dart';
import 'package:bossip_mobile/shared/api/ui_mode_store.dart';
import 'package:bossip_mobile/shared/config/env.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/models/ui_mode.dart';
import 'package:bossip_mobile/shared/router/paths.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:integration_test/integration_test.dart';
import 'package:path_provider/path_provider.dart';
import 'package:video_player/video_player.dart';

void main() {
  final binding = IntegrationTestWidgetsFlutterBinding.ensureInitialized();
  const enabled = bool.fromEnvironment('LIGHT_QA');
  const generate = bool.fromEnvironment('LIGHT_QA_GENERATE');
  const username = String.fromEnvironment('LIGHT_QA_USERNAME');
  const password = String.fromEnvironment('LIGHT_QA_PASSWORD');
  const photo = String.fromEnvironment('LIGHT_QA_PHOTO');
  const clip = String.fromEnvironment('LIGHT_QA_CLIP');
  const resume = String.fromEnvironment('LIGHT_QA_RESUME_VIDEO');
  const remake = String.fromEnvironment('LIGHT_QA_REMAKE_RESULT');
  const reviewOutputs = bool.fromEnvironment('LIGHT_QA_REVIEW_OUTPUTS');
  const editOutput = String.fromEnvironment('LIGHT_QA_EDIT_RESULT');
  const menuOutput = String.fromEnvironment('LIGHT_QA_MENU_RESULT');

  testWidgets(
    'Light production on the local backend',
    (tester) async {
      expect(Uri.parse(Env.apiBase).host, isIn(['localhost', '127.0.0.1']));
      expect(
        [username, password, photo, clip].every((value) => value.isNotEmpty),
        isTrue,
      );
      final support = await getApplicationSupportDirectory();
      final evidence = await Directory(
        '${support.path}/light-production-qa',
      ).create(recursive: true);
      final marker = DateTime.now().millisecondsSinceEpoch;
      Future<void> capture(String name) async {
        await tester.pump(const Duration(milliseconds: 300));
        await File(
          '${evidence.path}/$name.png',
        ).writeAsBytes(await binding.takeScreenshot(name));
      }

      Future<void> until(
        bool Function() ready,
        String stage, {
        int seconds = 45,
      }) async {
        final deadline = DateTime.now().add(Duration(seconds: seconds));
        while (!ready() && DateTime.now().isBefore(deadline)) {
          await tester.pump(const Duration(milliseconds: 230));
          await Future<void>.delayed(const Duration(milliseconds: 80));
        }
        if (!ready()) await capture('failure-$stage');
        expect(ready(), isTrue, reason: stage);
        debugPrint('LIGHT_QA $stage');
      }

      Finder key(String name) => find.byKey(Key(name));
      bool shown(String name) => key(name).evaluate().isNotEmpty;
      Future<void> tap(String name) async {
        await until(() => shown(name), name);
        await tester.ensureVisible(key(name).first);
        await tester.pump(const Duration(milliseconds: 300));
        await tester.tap(key(name).first);
        await tester.pump(const Duration(milliseconds: 300));
      }

      String current() =>
          tester.widget<ProductionPage>(find.byType(ProductionPage)).sessionId;
      String displayedScript() =>
          tester.widget<TextField>(key('production-script')).controller!.text;

      await app.main();
      await tester.pump();
      final container = ProviderScope.containerOf(
        tester.element(find.byType(BossipApp)),
        listen: false,
      );
      if (container.read(authProvider).user?.username != username) {
        await container.read(authFlowProvider).login(username, password);
      }
      await container.read(uiModeProvider.notifier).setMode(UiMode.light);
      container.read(i18nProvider.notifier).setLanguage('zh-CN');
      final router = container.read(routerProvider);
      final api = container.read(lightProductionApiProvider);

      Future<void> home() async {
        router.go(Paths.home);
        final list = find.descendant(
          of: key('light-home'),
          matching: find.byType(Scrollable),
        );
        await until(() => list.evaluate().isNotEmpty, 'home-scroll');
        tester.state<ScrollableState>(list.first).position.jumpTo(0);
        await tester.pump(const Duration(milliseconds: 300));
        await until(() => shown('light-entry-video'), 'home');
      }

      Future<void> openFromHistory(String id) async {
        await home();
        await tap('light-production-history');
        await tap('production-history-$id');
        await until(() => shown('production-result-preview'), 'result-$id');
        expect(current(), id);
      }

      if (reviewOutputs) {
        for (final (id, filename) in [
          (editOutput, '16-footage-result'),
          (menuOutput, '17-menu-result'),
          (remake, '18-remake-result'),
        ]) {
          await openFromHistory(id);
          final image = find.descendant(
            of: key('production-result-preview'),
            matching: find.byType(Image),
          );
          await until(() => image.evaluate().isNotEmpty, 'preview-image-$id');
          final element = tester.element(image.first);
          Object? failure;
          await precacheImage(
            tester.widget<Image>(image.first).image,
            element,
            onError: (error, _) => failure = error,
          ).timeout(const Duration(seconds: 30));
          expect(failure, isNull);
          await capture(filename);
          await Future<void>.delayed(const Duration(seconds: 3));
        }
        await home();
        await capture('01-home');
        await Future<void>.delayed(const Duration(seconds: 3));
        return;
      }

      Future<void> library(String assetId, {bool video = false}) async {
        await tap('production-source-library');
        if (video) {
          await until(
            () => find.text('视频').evaluate().isNotEmpty,
            'library-video-tab',
          );
          await tester.tap(find.text('视频').last);
          await tester.pump(const Duration(milliseconds: 350));
        }
        final items = find.descendant(
          of: find.byType(ProductionLibrary),
          matching: find.byType(CustomScrollView),
        );
        await until(() => items.evaluate().isNotEmpty, 'library-open');
        for (
          var page = 0;
          page < 14 && !shown('production-library-$assetId');
          page++
        ) {
          await tester.drag(items, const Offset(0, -350));
          await tester.pump(const Duration(milliseconds: 500));
          await Future<void>.delayed(const Duration(milliseconds: 300));
        }
        await tap('production-library-$assetId');
        final confirm = find.text('确认 1 个素材');
        expect(confirm, findsOneWidget);
        await tester.tap(confirm);
        await until(
          () => find.text('已选 1 个素材').evaluate().isNotEmpty,
          'material-saved',
        );
      }

      var videoId = resume;
      const replaced = '刚出锅的麻辣小龙虾，趁热吃。';
      if (videoId.isEmpty) {
        await home();
        expect(
          shown('light-entry-footage_edit') && shown('light-entry-menu'),
          isTrue,
        );
        expect(
          tester.getTopLeft(key('light-entry-video')).dy,
          lessThan(tester.getTopLeft(find.text('今天可以做')).dy),
        );
        await capture('01-home');

        await tap('light-entry-video');
        await until(() => shown('production-shop'), 'video-materials');
        videoId = current();
        await tester.enterText(key('production-shop'), '巷口小龙虾');
        await library(photo);
        await capture('02-video-materials');
        await tap('production-primary');
        await tap('production-template-twist');
        await capture('03-video-template');
        await tap('production-case-preview');
        await until(
          () => find.byType(VideoPlayer).evaluate().isNotEmpty,
          'crawfish-case-playing',
        );
        await tester.tap(find.byType(CloseButton).last);
        await tester.pump(const Duration(milliseconds: 450));
        await tap('production-primary');
        await until(() => shown('production-progress'), 'copy-progress');
        await capture('04-video-copy-progress');
        await until(
          () =>
              shown('production-script') && displayedScript().trim().isNotEmpty,
          'copy-generated',
          seconds: 100,
        );
        final preparedVersion = (await api.get(videoId)).agentRevision;
        await tester.enterText(key('production-script'), '保留门头。小龙虾刚出锅，趁热吃。');
        FocusManager.instance.primaryFocus?.unfocus();
        await tester.pump(const Duration(milliseconds: 700));
        await tap('production-optimize');
        await until(() => shown('production-progress'), 'optimize-progress');
        await until(
          () =>
              shown('production-script') && displayedScript().trim().isNotEmpty,
          'copy-optimized',
          seconds: 100,
        );
        final version = (await api.get(videoId)).agentRevision;
        expect(version, greaterThan(preparedVersion));
        await capture('05-video-copy');

        await tap('production-advanced');
        await until(
          () => find.byType(ChatScreen).evaluate().isNotEmpty,
          'advanced-agent',
        );
        expect(
          tester.widget<ChatScreen>(find.byType(ChatScreen)).sessionId,
          videoId,
        );
        await until(
          () =>
              container.read(chatSessionProvider(videoId)).session?.id ==
              videoId,
          'advanced-agent-fetched',
        );
        await container
            .read(chatSessionProvider(videoId).notifier)
            .send('请直接把制作表单里的视频文案更新为「$replaced」只改草稿。');
        await capture('06-advanced-chat');
        var updated = await api.get(videoId);
        final advancedDeadline = DateTime.now().add(
          const Duration(seconds: 100),
        );
        while ((updated.agentRevision <= version || updated.working) &&
            DateTime.now().isBefore(advancedDeadline)) {
          await tester.pump(const Duration(seconds: 1));
          await Future<void>.delayed(const Duration(seconds: 1));
          updated = await api.get(videoId);
        }
        expect(updated.agentRevision, greaterThan(version));
        expect(updated.script, contains(replaced));
        await tap('production-return');
        await until(
          () =>
              shown('production-script') &&
              displayedScript().contains(replaced),
          'advanced-return-synced',
        );
        await capture('07-return-synced');
      }

      await home();
      await tap('light-production-history');
      await until(() => shown('production-history-$videoId'), 'history-list');
      await capture('08-history');
      await tap('production-history-$videoId');
      await until(
        () =>
            shown('production-script') && displayedScript().contains(replaced),
        'resume-same-draft',
      );
      expect(current(), videoId);

      await home();
      await tap('light-entry-menu');
      await until(() => shown('production-dish-add'), 'menu-materials');
      final menuId = current();
      expect(menuId, isNot(videoId));
      await tester.enterText(key('production-shop'), '巷口小龙虾');
      await tap('production-dish-add');
      await tester.enterText(key('production-dish-name'), '麻辣小龙虾');
      await tester.enterText(key('production-dish-price'), '¥68');
      await tap('production-dish-save');
      await library(photo);
      await tap('production-primary');
      await until(
        () => shown('production-template-warm_red'),
        'menu-templates',
      );
      await capture('09-menu-template');
      await tap('production-template-warm_red');
      await tap('production-primary');
      await until(
        () =>
            find.text('核对菜品').evaluate().isNotEmpty &&
            !shown('production-progress'),
        'menu-review',
        seconds: 105,
      );
      expect(find.text('麻辣小龙虾'), findsOneWidget);
      expect(find.text('¥68'), findsOneWidget);
      await capture('10-menu-review');
      if (generate) {
        await tap('production-primary');
        await until(() => shown('production-progress'), 'menu-progress');
        await capture('11-menu-progress');
      }

      await home();
      await tap('light-entry-footage_edit');
      await until(() => shown('production-shop'), 'footage-materials');
      final footageId = current();
      expect({videoId, menuId, footageId}, hasLength(3));
      await tester.enterText(key('production-shop'), '巷口小龙虾');
      await library(clip, video: true);
      await capture('12-footage-materials');
      await tap('production-primary');
      await until(
        () => shown('production-template-documentary'),
        'footage-style',
      );
      await capture('13-footage-style');
      await tap('production-template-documentary');
      await tap('production-primary');
      await until(
        () => shown('production-script'),
        'footage-copy',
        seconds: 140,
      );
      await capture('14-footage-review');
      if (generate) {
        await tap('production-primary');
        await until(() => shown('production-progress'), 'ims-progress');
        await capture('15-footage-progress');
        await until(
          () => shown('production-result-preview'),
          'ims-output',
          seconds: 240,
        );
        final footage = await api.get(footageId);
        expect(footage.output?.video, isTrue);
        await capture('16-footage-result');
        await tap('production-result-preview');
        await until(
          () => find.byType(VideoPlayer).evaluate().isNotEmpty,
          'ims-playback',
        );
        await tester.tap(find.byType(CloseButton).last);

        await home();
        await tap('light-production-history');
        await tap('production-history-$menuId');
        await until(
          () => shown('production-result-preview'),
          'menu-output',
          seconds: 200,
        );
        final menu = await api.get(menuId);
        expect(menu.output?.mime, startsWith('image/'));
        await capture('17-menu-result');
      }
      if (remake.isNotEmpty) {
        await home();
        await tap('light-production-history');
        await tap('production-history-$remake');
        await until(
          () => shown('production-result-preview'),
          'remake-result',
          seconds: 70,
        );
        await capture('18-remake-result');
        await tap('production-result-preview');
        await until(
          () => find.byType(VideoPlayer).evaluate().isNotEmpty,
          'remake-result-playing',
        );
        await tester.tap(find.byType(CloseButton).last);
      }
      await File(
        '${evidence.path}/sessions-$marker.txt',
      ).writeAsString('$videoId\n$menuId\n$footageId\n');
      await home();
      debugPrint('LIGHT_QA_EVIDENCE ${evidence.path}');
    },
    skip: !enabled,
    timeout: const Timeout(Duration(minutes: 16)),
  );
}
