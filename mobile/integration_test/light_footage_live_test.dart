import 'dart:convert';
import 'dart:io';

import 'package:bossip_mobile/app/app.dart';
import 'package:bossip_mobile/app/router.dart';
import 'package:bossip_mobile/features/chat/chat_screen.dart';
import 'package:bossip_mobile/features/chat/state/chat_session_controller.dart';
import 'package:bossip_mobile/features/light/production/production_api.dart';
import 'package:bossip_mobile/features/light/production/production_page.dart';
import 'package:bossip_mobile/main.dart' as app;
import 'package:bossip_mobile/shared/api/auth_store.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:bossip_mobile/shared/config/env.dart';
import 'package:bossip_mobile/shared/router/paths.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
// The simulator replaces the system picker with the person's selected files.
// ignore: depend_on_referenced_packages
import 'package:image_picker_platform_interface/image_picker_platform_interface.dart';
import 'package:integration_test/integration_test.dart';
import 'package:video_player/video_player.dart';

class _FootagePicker extends ImagePickerPlatform {
  _FootagePicker(this.paths);

  final List<String> paths;

  @override
  Future<List<XFile>> getMedia({required MediaOptions options}) async => [
    for (final path in paths) XFile(path),
  ];
}

void main() {
  final binding = IntegrationTestWidgetsFlutterBinding.ensureInitialized();
  const directory = String.fromEnvironment('FOOTAGE_QA_SOURCE');
  const output = String.fromEnvironment('FOOTAGE_QA_OUT');
  const resume = String.fromEnvironment('FOOTAGE_QA_RESUME');
  const creative = bool.fromEnvironment('FOOTAGE_QA_CREATIVE');

  testWidgets(
    'App edits the selected real footage and plays the IMS result',
    (tester) async {
      expect(Uri.parse(Env.apiBase).host, isIn(['localhost', '127.0.0.1']));
      final evidence = Directory(output)..createSync(recursive: true);
      final events = File('${evidence.path}/steps.log');
      final watch = Stopwatch()..start();
      void mark(String stage) {
        final line = '${watch.elapsed.inSeconds}s $stage';
        debugPrint('FOOTAGE_QA $line');
        events.writeAsStringSync('$line\n', mode: FileMode.append);
      }

      Finder key(String name) => find.byKey(Key(name));
      bool shown(String name) => key(name).hitTestable().evaluate().isNotEmpty;

      Future<void> capture(String name) async {
        await tester.pump(const Duration(milliseconds: 350));
        final bytes = await binding.takeScreenshot(name);
        File('${evidence.path}/$name.png').writeAsBytesSync(bytes);
      }

      Future<void> until(
        bool Function() ready,
        String stage, {
        int seconds = 35,
      }) async {
        final end = DateTime.now().add(Duration(seconds: seconds));
        while (!ready() && DateTime.now().isBefore(end)) {
          await tester.pump(const Duration(milliseconds: 180));
          await Future<void>.delayed(const Duration(milliseconds: 150));
        }
        if (!ready()) await capture('failure-$stage');
        expect(ready(), isTrue, reason: stage);
        mark(stage);
      }

      Future<void> tap(String name) async {
        await until(() => key(name).evaluate().isNotEmpty, name);
        await tester.ensureVisible(key(name).first);
        await tester.pump(const Duration(milliseconds: 180));
        await until(() => shown(name), 'visible-$name');
        await tester.tap(key(name).hitTestable().first);
        await tester.pump(const Duration(milliseconds: 380));
      }

      await app.main();
      await tester.pump();
      final c = ProviderScope.containerOf(
        tester.element(find.byType(BossipApp)),
        listen: false,
      );
      await until(() => !c.read(authProvider).isLoading, 'signed-in');
      expect(c.read(authProvider).user?.username, 'devtest');
      final router = c.read(routerProvider);
      final api = c.read(lightProductionApiProvider);
      final dio = c.read(apiDioProvider);
      late final String id;
      if (resume.isNotEmpty) {
        id = resume;
        router.go(Paths.home);
        await tap('light-production-history');
        await tap('production-history-$id');
      } else {
        final paths =
            Directory(directory)
                .listSync()
                .whereType<File>()
                .map((file) => file.path)
                .where((path) => path.toLowerCase().endsWith('.mp4'))
                .toList()
              ..sort();
        expect(paths.length, 4);
        final original = ImagePickerPlatform.instance;
        ImagePickerPlatform.instance = _FootagePicker(paths);
        addTearDown(() => ImagePickerPlatform.instance = original);
        router.go(Paths.home);
        await tap('light-entry-footage_edit');
        await until(() => shown('production-shop'), 'draft-ready');
        id = tester
            .widget<ProductionPage>(find.byType(ProductionPage))
            .sessionId;
        File('${evidence.path}/session.txt').writeAsStringSync(id);
        mark('session $id');
        await tester.enterText(key('production-shop'), '塞外朵莱娜');
        FocusManager.instance.primaryFocus?.unfocus();
        await tester.pump(const Duration(milliseconds: 260));
        await tap('production-source-upload');
        await until(
          () => find.text('已选 4 个素材').evaluate().isNotEmpty,
          'four-clips-uploaded',
          seconds: 105,
        );
        await capture('materials');
        await tap('production-primary');
        await tap(
          creative
              ? 'production-template-product'
              : 'production-template-documentary',
        );
        if (creative) await tap('production-creative-effects');
        expect(find.text('开始剪辑'), findsOneWidget);
        expect(find.text('保留原声'), findsOneWidget);
        await capture('style');
        watch.reset();
        mark('render-tap');
        await tap('production-primary');
        await until(() => shown('production-progress'), 'one-tap-progress');
        expect(key('production-script'), findsNothing);
        await capture('progress');
      }

      await until(
        () =>
            shown('production-result-preview') ||
            find.text('重试制作').hitTestable().evaluate().isNotEmpty,
        'render-settles',
        seconds: creative ? 540 : 360,
      );
      final task = await api.get(id);
      File('${evidence.path}/result.json').writeAsStringSync(
        jsonEncode({
          'session': id,
          'status': task.status,
          'phase': task.phase,
          'shop': task.shop,
          'style': task.template,
          'creative_effects': task.creativeEffects,
          'copy': task.script,
          'asset': task.output?.id,
          'sources': [
            for (final media in task.materials)
              {'id': media.id, 'name': media.name},
          ],
        }),
      );
      await capture('result');
      expect(task.status, 'completed');
      expect(task.output?.video, isTrue);
      if (resume.isEmpty) {
        expect(task.materials.length, 4);
        expect(task.script, isEmpty);
        expect(task.creativeEffects, creative);
      }
      await tap('production-result-preview');
      await until(
        () => tester
            .widgetList<VideoPlayer>(find.byType(VideoPlayer))
            .any(
              (v) =>
                  v.controller.value.isInitialized &&
                  v.controller.value.position >
                      const Duration(milliseconds: 600),
            ),
        'playing-result',
        seconds: 45,
      );
      await capture('playback');
      final asset = (await dio.get<Map<String, dynamic>>(
        '/api/assets/${task.output!.id}/url',
      )).data!;
      await Dio().download(
        asset['url'] as String,
        '${evidence.path}/edited.mp4',
      );
      await tap('media-viewer-close');
      await tap('production-advanced');
      await until(
        () => find.byType(ChatScreen).evaluate().isNotEmpty,
        'advanced-chat',
      );
      expect(tester.widget<ChatScreen>(find.byType(ChatScreen)).sessionId, id);
      await until(
        () => c.read(chatSessionProvider(id)).session?.id == id,
        'same-agent-session',
      );
      await capture('advanced');
      await tap('production-return');
      await until(
        () => shown('production-result-preview'),
        'returned-to-same-result',
      );
      expect((await api.get(id)).output?.id, task.output?.id);
      expect(tester.takeException(), isNull);
    },
    skip: directory.isEmpty && resume.isEmpty,
  );
}
