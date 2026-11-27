import 'dart:convert';
import 'dart:io';

import 'package:bossip_mobile/app/app.dart';
import 'package:bossip_mobile/app/router.dart';
import 'package:bossip_mobile/features/auth/state/auth_flow.dart';
import 'package:bossip_mobile/features/light/production/production_api.dart';
import 'package:bossip_mobile/main.dart' as app;
import 'package:bossip_mobile/shared/api/auth_store.dart';
import 'package:bossip_mobile/shared/api/ui_mode_store.dart';
import 'package:bossip_mobile/shared/config/env.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/models/ui_mode.dart';
import 'package:bossip_mobile/shared/router/paths.dart';
import 'package:bossip_mobile/shared/widgets/media_viewer.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
// ignore: depend_on_referenced_packages
import 'package:image_picker_platform_interface/image_picker_platform_interface.dart';
import 'package:integration_test/integration_test.dart';
import 'package:path_provider/path_provider.dart';
import 'package:video_player/video_player.dart';

class _Picker extends ImagePickerPlatform {
  _Picker(this.file);
  final XFile file;
  @override
  Future<List<XFile>> getMedia({required MediaOptions options}) async => [file];
  @override
  Future<List<XFile>> getMultiImageWithOptions({
    MultiImagePickerOptions options = const MultiImagePickerOptions(),
  }) async => [file];
}

void main() {
  IntegrationTestWidgetsFlutterBinding.ensureInitialized();
  testWidgets(
    'Submit four templates through the production form',
    (tester) async {
      expect(Uri.parse(Env.apiBase).host, '127.0.0.1');
      const origin = String.fromEnvironment('LIGHT_QA_FIXTURES');
      final temp = await getApplicationSupportDirectory();
      final stateFile = File(
        '${temp.path}/${const String.fromEnvironment('LIGHT_QA_BATCH')}-templates.json',
      );
      final sessions = stateFile.existsSync()
          ? Map<String, String>.from(
              jsonDecode(stateFile.readAsStringSync()) as Map<String, dynamic>,
            )
          : <String, String>{};
      const resumeBatch = String.fromEnvironment('LIGHT_QA_EXISTING_SESSIONS');
      if (resumeBatch.isNotEmpty) {
        sessions.addAll(
          Map<String, String>.from(
            jsonDecode(utf8.decode(base64Url.decode(resumeBatch)))
                as Map<String, dynamic>,
          ),
        );
      }
      Finder k(String name) => find.byKey(Key(name));
      Future<void> wait(
        bool Function() test,
        String what, {
        int seconds = 65,
      }) async {
        final end = DateTime.now().add(Duration(seconds: seconds));
        while (!test() && DateTime.now().isBefore(end)) {
          await tester.pump(const Duration(milliseconds: 250));
          await Future<void>.delayed(const Duration(milliseconds: 120));
        }
        expect(test(), isTrue, reason: what);
      }

      Future<void> tap(Finder target) async {
        await wait(() => target.evaluate().isNotEmpty, 'present: $target');
        await tester.ensureVisible(target.first);
        await tester.pump(const Duration(milliseconds: 300));
        await tester.tap(target.first);
        await tester.pump(const Duration(milliseconds: 500));
      }

      Future<void> fill(String key, String value) async {
        await tester.ensureVisible(k(key));
        await tester.enterText(k(key), value);
        FocusManager.instance.primaryFocus?.unfocus();
        await tester.pump(const Duration(milliseconds: 650));
      }

      await app.main();
      await tester.pump();
      final container = ProviderScope.containerOf(
        tester.element(find.byType(BossipApp)),
        listen: false,
      );
      const username = String.fromEnvironment('LIGHT_QA_USERNAME');
      if (container.read(authProvider).user?.username != username) {
        await container
            .read(authFlowProvider)
            .login(username, const String.fromEnvironment('LIGHT_QA_PASSWORD'));
      }
      await container.read(uiModeProvider.notifier).setMode(UiMode.light);
      container.read(i18nProvider.notifier).setLanguage('zh-CN');
      final router = container.read(routerProvider),
          api = container.read(lightProductionApiProvider);
      if (const bool.fromEnvironment('LIGHT_QA_RESULTS_ONLY')) {
        expect(sessions.length, 4);
        for (final entry in sessions.entries) {
          router.go(Paths.lightProduction(entry.value));
          await tester.pump(const Duration(milliseconds: 500));
          await wait(
            () => k('production-result-preview').evaluate().isNotEmpty,
            'result ${entry.key}',
          );
          final result = await api.get(entry.value);
          expect(result.status, 'completed');
          expect(result.output?.video, isTrue);
          await tap(k('production-result-preview'));
          final viewer = find.byWidgetPredicate(
            (widget) =>
                widget is MediaViewer && widget.assetId == result.output!.id,
          );
          await wait(() => viewer.evaluate().isNotEmpty, 'viewer ${entry.key}');
          final player = find.descendant(
            of: viewer,
            matching: find.byType(VideoPlayer),
          );
          await wait(() {
            if (player.evaluate().isEmpty) return false;
            final value = tester.widget<VideoPlayer>(player).controller.value;
            return !value.hasError && value.position.inMilliseconds > 500;
          }, 'playback ${entry.key}');
          debugPrint('MODES_RESULT ${entry.key} ${result.output!.id} played');
          Navigator.of(tester.element(viewer)).pop();
          await tester.pump(const Duration(milliseconds: 500));
        }
        return;
      }
      final cases = {
        'soda_story': (
          'headphones',
          '云朵耳机',
          '云间声音耳机咖啡馆雨天广告，依照七镜反转。结尾摘下耳机才发现雨停了。两名成年朋友一问一答，说“雨停了吗？”“再听一首吧。”将两句自然译成目标语言原生配音，逐句配可读的同期字幕，画面广告文字也用目标语言。',
        ),
        'product_story': (
          'headphones',
          '云朵耳机',
          '云间声音的奶油色耳机，五场广告从清晨家中到街角，按模板三次动作匹配，展现戴上、出门、交接及最后的街角反转。按原模板时长完成全片。',
        ),
        'shortcut_story': (
          'crayfish',
          '麻辣小龙虾',
          '兴兴向荣餐馆麻辣小龙虾，45元一斤。两个人忙着绕路买晚饭，朋友早在店里剥好一只等他们。按七镜节奏和笑点成片。',
        ),
        'silk_road': (
          'lamb',
          '丝路香馕烤羊腿',
          '朵薇娅丝路香馕烤羊腿，按约一分钟的丝路故事：误拿干馕，两次试错，码头终于揭包看到烤羊腿，船上分享。按原模板完整节奏、宽银幕和旅途场景来制作。',
        ),
      };
      for (final item in cases.entries) {
        final id = item.key;
        if (sessions.containsKey(id)) continue;
        const resume = String.fromEnvironment('LIGHT_QA_RESUME_SODA');
        if (id == 'soda_story' && resume.isNotEmpty) {
          router.go(Paths.lightProduction(resume));
        } else {
          router.go(Paths.home);
          final home = k('light-entry-video');
          await tap(home);
          await wait(
            () => k('production-source-upload').evaluate().isNotEmpty,
            'materials',
          );
          final file = File('${temp.path}/${item.value.$1}.jpg');
          await Dio().download('$origin/${item.value.$1}.jpg', file.path);
          final old = ImagePickerPlatform.instance;
          ImagePickerPlatform.instance = _Picker(
            XFile(file.path, mimeType: 'image/jpeg'),
          );
          try {
            await tap(k('production-source-upload'));
            await wait(
              () => find.text('已选 1 个素材').evaluate().isNotEmpty,
              'upload',
              seconds: 150,
            );
          } finally {
            ImagePickerPlatform.instance = old;
          }
          await tap(k('production-primary'));
          await tap(find.text('复刻爆款'));
          await tap(k('production-template-$id'));
          await tap(k('production-primary'));
        }
        await wait(
          () => k('production-output-language').evaluate().isNotEmpty,
          'video copy',
        );
        expect(
          tester.widget<SwitchListTile>(k('production-perfect-film')).value,
          isFalse,
        );
        if (k('production-product-name').evaluate().isNotEmpty) {
          await fill('production-product-name', item.value.$2);
        }
        await fill('production-script', item.value.$3);
        if (id == 'soda_story') {
          await tap(k('production-output-language'));
          await tap(k('production-language-manual'));
          await fill('production-language-input', '越南语');
          await tap(k('production-language-save'));
          await tap(k('production-perfect-film'));
          await tap(find.text('暂不开启'));
          expect(
            tester.widget<SwitchListTile>(k('production-perfect-film')).value,
            isFalse,
          );
          await tap(k('production-perfect-film'));
          await tap(k('production-perfect-confirm'));
          expect(
            tester.widget<SwitchListTile>(k('production-perfect-film')).value,
            isTrue,
          );
        }
        await tap(k('production-primary'));
        // Retrieve the submitted draft through the App's read API, then reopen the same
        // route when a generated storyboard needs the production button pressed.
        final history = await api.history(kind: ProductionKind.video);
        final latest = history.items.firstWhere(
          (t) => t.template == id && t.referenceCopy == item.value.$3,
        );
        expect(latest.perfectFilm, id == 'soda_story');
        expect(latest.outputLanguage, id == 'soda_story' ? '越南语' : '');
        sessions[id] = latest.sessionId;
        stateFile.writeAsStringSync(jsonEncode(sessions));
        debugPrint('MODES_APP $id ${latest.sessionId}');
      }
      for (final item in sessions.entries) {
        final id = item.key, sid = item.value;
        var task = await api.get(sid);
        if (task.status == 'failed' && task.phase == 'render') {
          router.go(Paths.lightProduction(sid));
          await wait(
            () => find.text('重试制作').evaluate().isNotEmpty,
            'retry $id',
          );
          await tap(k('production-primary'));
          final retried = await api.get(sid);
          expect(retried.status, isIn(['working', 'completed']));
          debugPrint('MODES_RETRY $id $sid');
          await Future<void>.delayed(const Duration(seconds: 8));
        } else if (id != 'product_story') {
          final end = DateTime.now().add(const Duration(minutes: 12));
          while (task.working &&
              task.phase == 'copy' &&
              DateTime.now().isBefore(end)) {
            await Future<void>.delayed(const Duration(seconds: 3));
            task = await api.get(sid);
          }
          if (task.status == 'draft' && task.remakePrepared) {
            router.go(Paths.lightProduction(sid));
            await wait(
              () => k('production-perfect-film').evaluate().isNotEmpty,
              'prepared $id',
            );
            await tap(k('production-primary'));
            final started = await api.get(sid);
            expect(started.status, isIn(['working', 'completed']));
            expect(started.phase, isNot('copy'));
            debugPrint('MODES_RENDER $id $sid');
          } else {
            expect(
              task.status,
              isIn(['working', 'completed']),
              reason: '$id ${task.status}',
            );
          }
        }
      }
    },
    timeout: const Timeout(Duration(minutes: 35)),
    skip: !const bool.fromEnvironment('LIGHT_QA'),
  );
}
