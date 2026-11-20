import 'dart:convert';
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
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:bossip_mobile/shared/api/ui_mode_store.dart';
import 'package:bossip_mobile/shared/config/env.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/models/ui_mode.dart';
import 'package:bossip_mobile/shared/router/paths.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
// The simulator test injects local files at the OS picker boundary.
// ignore: depend_on_referenced_packages
import 'package:image_picker_platform_interface/image_picker_platform_interface.dart';
import 'package:integration_test/integration_test.dart';
import 'package:path_provider/path_provider.dart';
import 'package:video_player/video_player.dart';

class _FixturePicker extends ImagePickerPlatform {
  _FixturePicker(this.files);

  final List<XFile> files;

  @override
  Future<List<XFile>> getMedia({required MediaOptions options}) async => files;

  @override
  Future<List<XFile>> getMultiImageWithOptions({
    MultiImagePickerOptions options = const MultiImagePickerOptions(),
  }) async => files;
}

void main() {
  final binding = IntegrationTestWidgetsFlutterBinding.ensureInitialized();
  const enabled = bool.fromEnvironment('LIGHT_QA');
  const generate = bool.fromEnvironment('LIGHT_QA_GENERATE');
  const silkRoad = bool.fromEnvironment('LIGHT_QA_SILK_ROAD');
  const verifyShotEditor = bool.fromEnvironment('LIGHT_QA_SILK_SHOT_EDITOR');
  const evidencePath = String.fromEnvironment('LIGHT_QA_EVIDENCE_DIR');
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
        username.isNotEmpty &&
            (silkRoad ||
                [password, photo, clip].every((value) => value.isNotEmpty)),
        isTrue,
      );
      final support = await getApplicationSupportDirectory();
      final evidence = await Directory(
        evidencePath.isEmpty
            ? '${support.path}/light-production-qa'
            : evidencePath,
      ).create(recursive: true);
      debugPrint('LIGHT_QA_EVIDENCE ${evidence.path}');
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
        expect(
          password,
          isNotEmpty,
          reason: 'Sign in to the simulator test account first.',
        );
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
          page < 125 && !shown('production-library-$assetId');
          page++
        ) {
          final more = find.text('加载更多').hitTestable();
          if (more.evaluate().isNotEmpty) {
            await tester.tap(more.last);
            await tester.pump(const Duration(milliseconds: 350));
          }
          await tester.drag(items, const Offset(0, -480));
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

      if (silkRoad) {
        const modern = bool.fromEnvironment('LIGHT_QA_SILK_MODERN');
        const product = String.fromEnvironment('LIGHT_QA_SILK_PRODUCT');
        const shop = String.fromEnvironment(
          'LIGHT_QA_SILK_SHOP',
          defaultValue: '塞外朵莱娜',
        );
        const customCopy = String.fromEnvironment('LIGHT_QA_SILK_BRIEF');
        const revisedShot = int.fromEnvironment('LIGHT_QA_SILK_RETAKE_INDEX');
        const revisedPrompt = String.fromEnvironment(
          'LIGHT_QA_SILK_RETAKE_PROMPT',
        );
        const story = modern
            ? '拍现代中国商圈餐饮广告《给夜班带一份》。第1张是塞外朵莱娜真实丝路香馕烤羊腿照片，第2张实拍是店内石材弧墙和花艺，第3张是演员定妆，按图保留米灰夹克男摄影师阿哲和铁锈棕西装女搭档岚姐。第4张是现代街景与共享单车参考，仅给步行、骑车和试馕路段。'
                  '两人收工吃羊腿，阿哲赶着给夜班同事打包，把玻璃柜台旁一袋浅色干馕试吃品当成正餐，店员穿现代围裙和短袖，骑共享单车到接驳站才发现。'
                  '街边用人物向下摇摄给浅黄小狗隔空闻袋再偏头的反应，小狗不吃；岚姐折回取真正的羊腿，赶到冷青玻璃站台揭包并递给阿哲。'
                  '银白接驳车内一口试吃，接站台挥手和车辆离开。原片只作为构图、运动与切点参考。片尾「给夜班带一份」。'
            : '用塞外朵莱娜的丝路香馕烤羊腿和真实摆盘，拍帐中初尝、集市误买干馕、沙里试吃、码头相赠、船上回味。'
                  '片名《丝路留香》，片尾「一路回味」。干馕袋全程只装浅色硬馕。保留原片的暖光、冷青码头和运镜，无需为轻微穿帮补拍。';
        final copy = customCopy.isNotEmpty
            ? customCopy
            : verifyShotEditor
            ? '$story 配乐按节拍，使用《Playful Pizzicato》。狗偏头留白，重点在揭开羊腿，离站自然淡出。第6段短标题用「揭开羊腿」。'
            : story;
        final clock = Stopwatch()..start();
        void mark(String stage) => debugPrint(
          'SILK_TIMING $stage ${clock.elapsedMilliseconds} ${DateTime.now().toUtc().toIso8601String()}',
        );
        final observed = <String, String>{};
        final frameEvents = <Map<String, Object?>>[];
        var readyClips = 0;
        var totalClips = 0;
        var resolvedFrames = 0;
        final dio = container.read(apiDioProvider);
        Interceptor? observer;
        void watchProduction(String id) {
          if (!verifyShotEditor && product.isEmpty) return;
          observer = InterceptorsWrapper(
            onResponse: (response, handler) {
              if (response.requestOptions.path.endsWith('/productions/$id') &&
                  response.data is Map<String, dynamic>) {
                final data = response.data as Map<String, dynamic>;
                final task = LightProduction.fromJson(data);
                final shots = task.shotWorkspace?.shots ?? [];
                totalClips = shots.length;
                readyClips = shots.where((shot) => shot.version != null).length;
                resolvedFrames = shots
                    .where(
                      (shot) =>
                          (shot.version?.first ?? shot.first)?.source ==
                              'input' &&
                          (shot.version?.last ?? shot.last)?.source == 'input',
                    )
                    .length;
                for (final shot in shots) {
                  final first =
                      shot.version?.first?.source ?? shot.first?.source;
                  final last = shot.version?.last?.source ?? shot.last?.source;
                  final version = shot.version?.number ?? 0;
                  final summary = '${shot.status}/$first/$last/v$version';
                  final key = shot.index.toString();
                  if (observed[key] != summary) {
                    observed[key] = summary;
                    final event = <String, Object?>{
                      't_ms': clock.elapsedMilliseconds,
                      'session': id,
                      'phase': task.phase,
                      'status': task.status,
                      'shot': shot.index,
                      'title': shot.title,
                      'shot_status': shot.status,
                      'first': first,
                      'last': last,
                      'version': version,
                      'ready': readyClips,
                      'total': totalClips,
                    };
                    frameEvents.add(event);
                    debugPrint('SILK_LIVE_FRAME ${jsonEncode(event)}');
                  }
                }
              }
              handler.next(response);
            },
          );
          dio.interceptors.add(observer!);
        }

        const finished = String.fromEnvironment('LIGHT_QA_SILK_RESULT');
        late final String id;
        if (finished.isNotEmpty) {
          await openFromHistory(finished);
          id = current();
          watchProduction(id);
        } else {
          await home();
          mark('start');
          await tap('light-entry-video');
          await until(() => shown('production-shop'), 'silk-shop');
          id = current();
          debugPrint('SILK_APP_SESSION $id');
          await tester.enterText(key('production-shop'), shop);
          if (photo.isNotEmpty) {
            await library(photo);
          } else {
            const fixture = String.fromEnvironment('LIGHT_QA_SILK_PHOTO_PATH');
            final paths = <(String, String)>[
              (
                fixture,
                product.isEmpty
                    ? 'silk-road-lamb.jpg'
                    : 'silk-road-product.jpg',
              ),
              if (modern) ...[
                (
                  const String.fromEnvironment('LIGHT_QA_SILK_ROOM_PATH'),
                  'silk-road-room.jpg',
                ),
                (
                  const String.fromEnvironment('LIGHT_QA_SILK_CAST_PATH'),
                  'silk-road-cast.jpg',
                ),
                (
                  const String.fromEnvironment('LIGHT_QA_SILK_STREET_PATH'),
                  'silk-road-street.jpg',
                ),
              ],
            ];
            final files = <XFile>[];
            for (final (input, name) in paths) {
              final path = '${support.path}/$name';
              if (input.isNotEmpty && await File(input).exists()) {
                await File(input).copy(path);
              }
              expect(await File(path).exists(), isTrue);
              files.add(XFile(path, mimeType: 'image/jpeg'));
            }
            final picker = ImagePickerPlatform.instance;
            ImagePickerPlatform.instance = _FixturePicker(files);
            try {
              await tap('production-source-upload');
              await until(
                () => find.text('已选 ${paths.length} 个素材').evaluate().isNotEmpty,
                'silk-material',
                seconds: 90,
              );
            } finally {
              ImagePickerPlatform.instance = picker;
            }
          }
          mark('material-selected');
          await capture('silk-materials');
          await tap('production-primary');
          await tap('production-template-silk_road');
          await capture('silk-template');
          await tap('production-primary');
          if (product.isNotEmpty) {
            expect(
              tester.widget<SwitchListTile>(key('production-beat-music')).value,
              isTrue,
            );
            await tester.enterText(key('production-product-name'), product);
          }
          await tester.ensureVisible(key('production-script'));
          await tester.enterText(key('production-script'), copy);
          FocusManager.instance.primaryFocus?.unfocus();
          await tester.pump(const Duration(milliseconds: 550));
          await tap('production-primary');
          mark('prepare');
          await until(
            () => shown('production-remake-script'),
            'silk-plan',
            seconds: 240,
          );
          mark('plan-ready');
          final plan = await api.get(id);
          expect(plan.remakePrepared, isTrue);
          expect(plan.referenceCopy, copy);
          expect(plan.script, contains(product.isEmpty ? '丝路香馕烤羊腿' : product));
          expect(plan.materials, hasLength(modern ? 4 : 1));
          if (modern) {
            expect(plan.script, contains('站台'));
          }
          await capture('silk-plan');
          watchProduction(id);
          await File('${evidence.path}/session-id.txt').writeAsString(id);
          await tap('production-primary');
          mark('render');
          await until(() => shown('production-progress'), 'silk-progress');
          await capture('silk-progress');
          if (verifyShotEditor || product.isNotEmpty) {
            await until(
              () =>
                  resolvedFrames > 0 ||
                  shown('production-result-preview') ||
                  find.text('重试制作').evaluate().isNotEmpty,
              'first-actual-shot-frames',
              seconds: 1800,
            );
            await capture('silk-progress-first-actual-frames');
            if (readyClips < totalClips) {
              await until(
                () =>
                    readyClips >= 4 ||
                    shown('production-result-preview') ||
                    find.text('重试制作').evaluate().isNotEmpty,
                'partially-completed-shots',
                seconds: 1200,
              );
              await capture('silk-progress-partial');
            }
            await File(
              '${evidence.path}/frame-events.json',
            ).writeAsString(jsonEncode(frameEvents));
          }
          await until(
            () =>
                shown('production-result-preview') ||
                find.text('重试制作').evaluate().isNotEmpty,
            'silk-result',
            seconds: 1800,
          );
          expect(shown('production-result-preview'), isTrue);
          mark('result-visible');
        }
        var result = await api.get(id);
        if (verifyShotEditor || product.isNotEmpty) {
          final deadline = DateTime.now().add(const Duration(seconds: 60));
          while (result.working && DateTime.now().isBefore(deadline)) {
            await tester.pump(const Duration(milliseconds: 450));
            await Future<void>.delayed(const Duration(seconds: 1));
            result = await api.get(id);
          }
        }
        expect(result.status, 'completed');
        expect(result.output?.video, isTrue);
        if (product.isNotEmpty) {
          expect(result.productName, product);
          expect(result.shotWorkspace!.shots, hasLength(9));
          for (final shot in result.shotWorkspace!.shots) {
            expect(shot.version!.first?.source, 'input');
            expect(shot.version!.last?.source, 'input');
            expect(
              shot.version!.framePrompts.keys,
              containsAll(['first_frame', 'last_frame']),
            );
          }
        }
        await capture('silk-result');
        await tap('production-result-preview');
        await until(
          () =>
              find.byType(VideoPlayer).evaluate().isNotEmpty &&
              tester
                      .widget<VideoPlayer>(find.byType(VideoPlayer).last)
                      .controller
                      .value
                      .position >
                  const Duration(seconds: 1),
          'silk-playing',
          seconds: 45,
        );
        final value = tester
            .widget<VideoPlayer>(find.byType(VideoPlayer).last)
            .controller
            .value;
        expect(value.duration.inMilliseconds, closeTo(62125, 250));
        expect(value.aspectRatio, closeTo(1680 / 660, 0.02));
        await capture('silk-playing');
        await tester.tap(find.byType(CloseButton).last);
        await tester.pump(const Duration(milliseconds: 350));
        if (revisedShot > 0) {
          final original = result.shotWorkspace!.current!;
          final editKey = 'production-edit-shot-$revisedShot';
          final scroll = find
              .descendant(
                of: key('production-shot-list'),
                matching: find.byType(Scrollable),
              )
              .first;
          await tester.scrollUntilVisible(
            key(editKey),
            450,
            scrollable: scroll,
            maxScrolls: 18,
          );
          await tap(editKey);
          final field = key('production-shot-prompt-field');
          await tester.ensureVisible(field);
          await tester.enterText(field, revisedPrompt);
          FocusManager.instance.primaryFocus?.unfocus();
          await tester.pump(const Duration(milliseconds: 400));
          await tap('production-retake-shot');
          mark('single-retake-submitted');
          await until(
            () => shown('production-confirm-compose'),
            'single-retake-ready',
            seconds: 650,
          );
          final pending = await api.get(id);
          expect(pending.output!.id, original.media.id);
          expect(pending.shotWorkspace!.pending.map((shot) => shot.index), [
            revisedShot,
          ]);
          await capture('silk-one-shot-confirmation');
          const hold = String.fromEnvironment('LIGHT_QA_SILK_RETAKE_REVIEW');
          if (hold.isNotEmpty) {
            await until(
              () => File(hold).existsSync(),
              'review-single-retake',
              seconds: 300,
            );
          }
          await tap('production-confirm-compose');
          mark('single-retake-compose');
          tester.state<ScrollableState>(scroll).position.jumpTo(0);
          await until(
            () => find.text('当前成片 · v2').evaluate().isNotEmpty,
            'revised-film',
            seconds: 300,
          );
          final revised = (await api.get(id)).shotWorkspace!.current!;
          expect(
            revised.segments.keys.where(
              (i) => revised.segments[i] != original.segments[i],
            ),
            [revisedShot],
          );
          mark('revised-film-ready');
          await capture('silk-one-shot-result');
          return;
        }
        if (verifyShotEditor) {
          final initial = await api.get(id);
          final workspace = initial.shotWorkspace!;
          expect(workspace.shots.length, 9);
          expect(
            workspace.shots.every(
              (shot) =>
                  shot.version?.first?.source ==
                      (shot.version!.framePrompts.isEmpty
                          ? 'video'
                          : 'input') &&
                  shot.version?.last?.source ==
                      (shot.version!.framePrompts.isEmpty ? 'video' : 'input'),
            ),
            isTrue,
          );
          final revealShot = workspace.shots.singleWhere(
            (shot) => shot.index == 6,
          );
          expect(revealShot.editable && revealShot.frameEditable, isTrue);
          expect(revealShot.title, contains('羊腿'));
          final editKey = 'production-edit-shot-${revealShot.index}';
          Finder scroll() => find
              .descendant(
                of: key('production-shot-list'),
                matching: find.byType(Scrollable),
              )
              .first;
          await tester.scrollUntilVisible(
            key(editKey),
            520,
            scrollable: scroll(),
            maxScrolls: 18,
          );
          await tap(editKey);
          await tap('production-shot-prompt-toggle');
          expect(key('production-shot-prompt-field'), findsNothing);
          await tap('production-shot-prompt-toggle');
          expect(key('production-shot-prompt-field'), findsOneWidget);
          const replacement = String.fromEnvironment('LIGHT_QA_SILK_TAIL_PATH');
          expect(await File(replacement).exists(), isTrue);
          final picker = ImagePickerPlatform.instance;
          ImagePickerPlatform.instance = _FixturePicker([
            XFile(replacement, mimeType: 'image/jpeg'),
          ]);
          try {
            await tap('production-replace-tail');
            await tester.tap(find.text('手机相册').hitTestable());
            await until(
              () => find.text('尾帧已更换，需重新生成此片段').evaluate().isNotEmpty,
              'replacement-tail-visible',
            );
          } finally {
            ImagePickerPlatform.instance = picker;
          }
          await capture('silk-replacement-tail');
          mark('retake-requested');
          await tap('production-retake-shot');
          await until(
            () => shown('production-confirm-compose'),
            'new-reveal-ready',
            seconds: 750,
          );
          mark('retake-ready');
          expect((await api.get(id)).output?.id, initial.output?.id);
          await capture('silk-compose-confirmation');
          mark('recompose-requested');
          await tap('production-confirm-compose');
          await until(
            () => shown('production-shot-list'),
            'shot-result-visible',
          );
          tester.state<ScrollableState>(scroll()).position.jumpTo(0);
          await until(
            () => find.text('当前成片 · v2').evaluate().isNotEmpty,
            'recompose-ready',
            seconds: 240,
          );
          mark('recompose-ready');
          final edited = await api.get(id);
          expect(edited.output?.id, isNot(initial.output?.id));
          expect(edited.shotWorkspace?.films.length, 2);
          for (final shot in edited.shotWorkspace!.shots) {
            expect(
              shot.versions.length,
              shot.index == revealShot.index ? 2 : 1,
            );
          }
          await tap('production-version-history');
          await capture('silk-version-history');
          expect(find.text('成片 v1'), findsOneWidget);
          await tester.tap(find.text('成片 v1').hitTestable());
          await until(
            () =>
                find.byType(VideoPlayer).evaluate().isNotEmpty &&
                tester
                        .widget<VideoPlayer>(find.byType(VideoPlayer).last)
                        .controller
                        .value
                        .position >
                    const Duration(seconds: 2),
            'old-film-playing',
            seconds: 60,
          );
          mark('old-film-playing');
          expect(
            tester
                .widget<VideoPlayer>(find.byType(VideoPlayer).last)
                .controller
                .value
                .duration
                .inMilliseconds,
            closeTo(62125, 250),
          );
          await capture('silk-old-film-playing');
          await File(
            '${evidence.path}/frame-events.json',
          ).writeAsString(jsonEncode(frameEvents));
          await File('${evidence.path}/films.json').writeAsString(
            jsonEncode({
              'session': id,
              'v1': initial.output?.id,
              'v2': edited.output?.id,
            }),
          );
          if (observer != null) dio.interceptors.remove(observer!);
          debugPrint('SILK_SHOT_EDITOR_DONE $id');
          return;
        }
        await tap('production-advanced');
        await until(
          () => find.byType(ChatScreen).evaluate().isNotEmpty,
          'silk-agent',
        );
        expect(
          tester.widget<ChatScreen>(find.byType(ChatScreen)).sessionId,
          id,
        );
        await until(
          () => key('production-return').hitTestable().evaluate().isNotEmpty,
          'silk-return-control',
        );
        await tester.tap(key('production-return').hitTestable());
        await until(() => shown('production-result-preview'), 'silk-returned');
        await openFromHistory(id);
        expect((await api.get(id)).output?.id, result.output?.id);
        debugPrint(
          'SILK_APP_RESULT $id ${result.output!.id} ${value.duration.inMilliseconds}',
        );
        debugPrint('LIGHT_QA_EVIDENCE ${evidence.path}');
        return;
      }

      if (const bool.fromEnvironment('LIGHT_QA_SODA_STORY')) {
        const brief = String.fromEnvironment('LIGHT_QA_REFERENCE_COPY');
        expect(brief, isNotEmpty);
        await home();
        await tap('light-entry-video');
        await until(() => shown('production-shop'), 'soda-materials');
        final id = current();
        debugPrint('SODA_APP_SESSION $id');
        await tester.enterText(key('production-shop'), '塞外朵莱娜');
        expect(shown('production-source-library'), isTrue);
        expect(shown('production-source-camera'), isTrue);
        expect(shown('production-source-upload'), isTrue);
        await capture('soda-materials-empty');
        final files = await Directory(
          '${support.path}/soda-fixtures',
        ).list().where((file) => file.path.endsWith('.mp4')).toList();
        files.sort((a, b) => a.path.compareTo(b.path));
        expect(files.length, 4);
        final originalPicker = ImagePickerPlatform.instance;
        ImagePickerPlatform.instance = _FixturePicker([
          for (final file in files) XFile(file.path, mimeType: 'video/mp4'),
        ]);
        final clock = Stopwatch()..start();
        void mark(String stage) => debugPrint(
          'SODA_TIMING $stage ${clock.elapsedMilliseconds} '
          '${DateTime.now().toUtc().toIso8601String()}',
        );
        mark('import');
        try {
          await tap('production-source-upload');
          await until(
            () => find.text('已选 4 个素材').evaluate().isNotEmpty,
            'soda-four-materials',
            seconds: 180,
          );
        } finally {
          ImagePickerPlatform.instance = originalPicker;
        }
        mark('materials-ready');
        await tap('production-primary');
        await tap('production-template-soda_story');
        await tap('production-soda-preview');
        await until(
          () => find.byType(VideoPlayer).evaluate().isNotEmpty,
          'soda-sample-preview',
        );
        await tester.tap(find.byType(CloseButton).last);
        await tester.pump(const Duration(milliseconds: 350));
        await tap('production-primary');
        await tester.enterText(key('production-script'), brief);
        FocusManager.instance.primaryFocus?.unfocus();
        await tester.pump(const Duration(milliseconds: 550));
        await tap('production-primary');
        mark('prepare-pressed');
        await until(
          () => shown('production-remake-script'),
          'soda-plan-ready',
          seconds: 300,
        );
        mark('plan-ready');
        final plan = await api.get(id);
        expect(plan.remakePrepared, isTrue);
        expect(plan.referenceCopy, brief);
        expect(plan.script, contains('丝路香馕烤羊腿'));
        const videoModel = String.fromEnvironment('LIGHT_QA_SODA_VIDEO_MODEL');
        if (videoModel.isNotEmpty) {
          await container
              .read(apiDioProvider)
              .patch<dynamic>(
                '/api/agent/session/$id',
                data: {'video_model': videoModel, 'video_resolution': '720p'},
              );
        }
        await tap('production-primary');
        mark('render-pressed');
        await until(
          () =>
              shown('production-result-preview') ||
              find.text('重试制作').evaluate().isNotEmpty,
          'soda-production-finished',
          seconds: 600,
        );
        expect(shown('production-result-preview'), isTrue);
        mark('result-visible');
        expect((await api.get(id)).output?.video, isTrue);
        await capture('soda-result');
        await tap('production-result-preview');
        await until(
          () =>
              find.byType(VideoPlayer).evaluate().isNotEmpty &&
              tester
                      .widget<VideoPlayer>(find.byType(VideoPlayer).last)
                      .controller
                      .value
                      .position >
                  const Duration(seconds: 1),
          'soda-playing',
          seconds: 45,
        );
        final player = tester.widget<VideoPlayer>(
          find.byType(VideoPlayer).last,
        );
        expect(
          player.controller.value.duration.inSeconds,
          inInclusiveRange(18, 40),
        );
        mark('playing');
        await capture('soda-playing');
        return;
      }

      if (const bool.fromEnvironment('LIGHT_QA_WAN_STORY')) {
        const words = String.fromEnvironment(
          'LIGHT_QA_REFERENCE_COPY',
          defaultValue:
              '按原片的餐厅解围和次日办公室反转，改编成巷口小龙虾广告。劝酒时女主管替男同事解围，端上唯一一锅虾。第二天他问昨晚那顿，她笑问今晚还想去，他说我请。文案只是参考，宣传题字用「有局有虾」。请用短镜头和正反打，上菜前桌面不能有虾锅。',
        );
        const existing = String.fromEnvironment('LIGHT_QA_WAN_CONTINUE');
        const finished = String.fromEnvironment('LIGHT_QA_WAN_RESULT');
        const setup = String.fromEnvironment('LIGHT_QA_WAN_SETUP');
        late final String id;
        if (finished.isNotEmpty) {
          await openFromHistory(finished);
          id = current();
          debugPrint('WAN_APP_SESSION $id');
        } else if (existing.isEmpty) {
          if (setup.isNotEmpty) {
            await home();
            await tap('light-production-history');
            await tap('production-history-$setup');
            await until(
              () => shown('production-script'),
              'wan-copy-retry-ready',
            );
            id = current();
            expect(id, setup);
            debugPrint('WAN_APP_SESSION $id');
          } else {
            await home();
            await tap('light-entry-video');
            await until(() => shown('production-shop'), 'wan-materials');
            id = current();
            debugPrint('WAN_APP_SESSION $id');
            await tester.enterText(key('production-shop'), '巷口小龙虾');
            await library(photo);
            await capture('wan-materials');
            await tap('production-tier-picker');
            await tap('production-model-medium');
            await tap('production-primary');
            await tap('production-template-soda_story');
            await tap('production-soda-preview');
            await until(
              () => find.byType(VideoPlayer).evaluate().isNotEmpty,
              'wan-preview',
            );
            await tap('media-viewer-close');
            await capture('wan-template');
            await tap('production-primary');
            await until(() => shown('production-script'), 'wan-copy-ready');
          }
          await tester.enterText(key('production-script'), words);
          FocusManager.instance.primaryFocus?.unfocus();
          await tester.pump(const Duration(milliseconds: 600));
          await tap('production-optimize');
          await until(() => shown('production-progress'), 'wan-optimize-start');
          await until(
            () => shown('production-remake-script'),
            'wan-approved-plan',
            seconds: 380,
          );
          final approved = await api.get(id);
          expect(approved.referenceCopy, words);
          expect(approved.remakePrepared, isTrue);
          expect(approved.script, contains('小龙虾'));
          if (words.contains('有局有虾')) {
            expect(approved.script, contains('有局有虾'));
          }
          await capture('wan-copy');
          debugPrint(
            'WAN_RENDER_TAP ${DateTime.now().toUtc().toIso8601String()}',
          );
          await tap('production-primary');
        } else {
          await home();
          await tap('light-production-history');
          await tap('production-history-$existing');
          await until(
            () => find.text('重试制作').evaluate().isNotEmpty,
            'wan-retry-from-history',
            seconds: 300,
          );
          id = current();
          expect(id, existing);
          debugPrint('WAN_APP_SESSION $id');
          final draft = await api.get(id);
          expect(draft.referenceCopy, words);
          expect(draft.remakePrepared, isTrue);
          await tap('production-primary');
        }
        if (finished.isEmpty) {
          await until(
            () => shown('production-progress'),
            'wan-production-start',
          );
          await capture('wan-progress');
          await until(
            () =>
                shown('production-result-preview') ||
                find.text('重试制作').evaluate().isNotEmpty,
            'wan-production-done',
            seconds: 3600,
          );
        }
        expect(shown('production-result-preview'), isTrue);
        debugPrint(
          'WAN_RESULT_VISIBLE ${DateTime.now().toUtc().toIso8601String()}',
        );
        var result = await api.get(id);
        expect(result.status, 'completed');
        expect(result.output?.video, isTrue);
        await capture('wan-result');
        await tap('production-result-preview');
        await until(
          () =>
              find.byType(VideoPlayer).evaluate().isNotEmpty &&
              tester
                      .widget<VideoPlayer>(find.byType(VideoPlayer).last)
                      .controller
                      .value
                      .position >
                  const Duration(seconds: 1),
          'wan-playing',
          seconds: 60,
        );
        final duration = tester
            .widget<VideoPlayer>(find.byType(VideoPlayer).last)
            .controller
            .value
            .duration;
        expect(duration.inMilliseconds, inInclusiveRange(18000, 96000));
        const expectedResultMs = int.fromEnvironment(
          'LIGHT_QA_EXPECTED_RESULT_DURATION_MS',
        );
        if (expectedResultMs > 0) {
          expect(
            (duration.inMilliseconds - expectedResultMs).abs(),
            lessThan(100),
          );
        }
        await capture('wan-playing');
        await tester.tap(find.byType(CloseButton).last);
        await tester.pump(const Duration(milliseconds: 350));
        await tap('production-advanced');
        await until(
          () => find.byType(ChatScreen).evaluate().isNotEmpty,
          'wan-same-agent',
        );
        expect(
          tester.widget<ChatScreen>(find.byType(ChatScreen)).sessionId,
          id,
        );
        await capture('wan-agent');
        if (const bool.fromEnvironment('LIGHT_QA_WAN_ADVANCED_EDIT')) {
          await until(
            () => container.read(chatSessionProvider(id)).session?.id == id,
            'wan-agent-fetched',
          );
          final before = result.output!.id;
          const advancedCopy = String.fromEnvironment(
            'LIGHT_QA_ADVANCED_COPY',
            defaultValue:
                '请直接修改刚完成的这份完整视频：只在最后的商品画面下方增加一行清楚的小字「现炒现做 · 趁热吃虾」。'
                '保留全部剧情、原声、配乐、原有毛笔题字和整条时长，直接使用已有成片合成新版，不要重新生成图片或视频镜头。'
                '这次已经确认制作，请直接完成并附上新版完整视频。',
          );
          await container
              .read(chatSessionProvider(id).notifier)
              .send(advancedCopy);
          final deadline = DateTime.now().add(const Duration(minutes: 15));
          do {
            await tester.pump(const Duration(seconds: 1));
            await Future<void>.delayed(const Duration(seconds: 1));
            result = await api.get(id);
          } while ((result.working || result.output?.id == before) &&
              DateTime.now().isBefore(deadline));
          expect(result.status, 'completed');
          expect(result.output?.id, isNot(before));
          expect(result.output?.video, isTrue);
          debugPrint('WAN_APP_ADVANCED_RESULT $id ${result.output!.id}');
          await capture('wan-advanced-edited');
        }
        final backToForm = key('production-return').hitTestable();
        await until(
          () => backToForm.evaluate().isNotEmpty,
          'wan-return-control',
        );
        await tester.tap(backToForm);
        await until(() => shown('production-result-preview'), 'wan-returned');
        expect(current(), id);
        if (const bool.fromEnvironment('LIGHT_QA_WAN_ADVANCED_EDIT')) {
          expect((await api.get(id)).output?.id, result.output!.id);
          await tap('production-result-preview');
          await until(
            () =>
                find.byType(VideoPlayer).evaluate().isNotEmpty &&
                tester
                        .widget<VideoPlayer>(find.byType(VideoPlayer).last)
                        .controller
                        .value
                        .position >
                    const Duration(seconds: 1),
            'wan-advanced-playing',
          );
          final edited = tester
              .widget<VideoPlayer>(find.byType(VideoPlayer).last)
              .controller
              .value
              .duration;
          const expectedEditMs = int.fromEnvironment(
            'LIGHT_QA_EXPECTED_EDIT_DURATION_MS',
          );
          expect(
            (edited.inMilliseconds -
                    (expectedEditMs > 0
                        ? expectedEditMs
                        : duration.inMilliseconds))
                .abs(),
            // A requested trim may include a short Agent-chosen overlap transition.
            lessThan(expectedEditMs > 0 ? 1000 : 200),
          );
          await capture('wan-advanced-playing');
          await tester.tap(find.byType(CloseButton).last);
          await tester.pump(const Duration(milliseconds: 350));
        }
        await home();
        await tap('light-production-history');
        await tap('production-history-$id');
        await until(() => shown('production-result-preview'), 'wan-history');
        expect((await api.get(id)).output?.id, result.output?.id);
        debugPrint(
          'WAN_APP_RESULT $id ${result.output!.id} ${duration.inMilliseconds}',
        );
        debugPrint('LIGHT_QA_EVIDENCE ${evidence.path}');
        return;
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
          () => shown('production-remake-script'),
          'copy-generated',
          seconds: 100,
        );
        final prepared = await api.get(videoId);
        expect(prepared.script, contains('带两双筷子'));
        const reference = '小龙虾刚出锅，趁热吃。';
        await tester.enterText(key('production-script'), reference);
        FocusManager.instance.primaryFocus?.unfocus();
        await tester.pump(const Duration(milliseconds: 700));
        await tap('production-optimize');
        await until(() => shown('production-progress'), 'optimize-progress');
        await until(
          () => shown('production-remake-script'),
          'copy-optimized',
          seconds: 100,
        );
        final revised = await api.get(videoId);
        final version = revised.agentRevision;
        expect(revised.referenceCopy, reference);
        expect(revised.script, contains('带两双筷子'));
        expect(version, greaterThan(prepared.agentRevision));
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
            .send('请直接把制作表单里的参考文案更新为「$replaced」只改草稿。');
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
        expect(updated.referenceCopy, replaced);
        expect(updated.remakePrepared, isFalse);
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
      expect(shown('production-script'), isFalse);
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
    timeout: Timeout(
      Duration(
        minutes:
            const bool.fromEnvironment('LIGHT_QA_WAN_STORY') ||
                verifyShotEditor ||
                silkRoad
            ? 68
            : 16,
      ),
    ),
  );
}
