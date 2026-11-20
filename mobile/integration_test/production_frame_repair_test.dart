import 'dart:io';

import 'package:bossip_mobile/app/app.dart';
import 'package:bossip_mobile/app/router.dart';
import 'package:bossip_mobile/features/auth/state/auth_flow.dart';
import 'package:bossip_mobile/features/light/production/production_api.dart';
import 'package:bossip_mobile/features/light/production/production_shots.dart';
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
import 'package:integration_test/integration_test.dart';
import 'package:video_player/video_player.dart';

const _head =
    '实拍电影感，宽银幕横幅。现代中国城市通勤接驳站的室外玻璃站边，阴天冷调自然光，'
    '背景是玻璃候车厅、共享单车、灰色地砖和城市街树。阿哲在画面左近处，以肩后视角入镜，'
    '露出短黑发，穿浅灰米色现代摄影夹克和浅色圆领衫，深色相机肩带，手拿棕色纸制食品袋。'
    '对面中景是岚姐，利落短波波头，锈棕色现代西装内搭黑色圆领衫，双臂松松交叠，'
    '看阿哲从袋口取一小块薄的烤馕。以项目人物照片和现代站边环境为准。'
    '街边是现代通勤日常，头部没有帽子或头巾，画面没有古装、骆驼、驼具、动物、帐篷、'
    '羊腿、标题、字幕或标志，避免粗麻绳和历史道具。';

const _tail =
    '实拍电影感，宽银幕横幅。同一个现代城市玻璃接驳站外，同样的阴天冷调自然光，'
    '共享单车、灰色地砖与玻璃墙在背后虚化。阿哲同一张脸和短黑发，穿同件浅灰米色现代'
    '摄影夹克、浅色圆领衫、深色相机肩带。胸口以上的正面偏三分之一侧景，阿哲低眼看手，'
    '手持同样的棕色纸袋和被咬下一小口的薄烤馕，用力抿嘴嚼了一下，表情克制。'
    '真实的人物比例和城市摄影质感。头上没有帽子或头巾，背景没有古装、骆驼、动物、'
    '驼具、帐篷或羊腿。没有题字、字幕、标志和可读文字。';

const _clip =
    '现代中国城市通勤接驳站外，低饱和阴天自然光，玻璃幕墙、街树与共享单车，电影实拍质感。'
    '严格沿用首尾帧的演员脸、当代服装、纸袋和相机肩带。阿哲是短黑发、浅灰米色摄影夹克和'
    '浅色圆领衫，岚姐是短波波头、锈棕西装和黑色圆领衫。'
    '先从阿哲肩后中近景开始：岚姐隔着半步看着对方，阿哲从棕纸袋掰一小块薄烤馕。'
    '接阿哲手和脸的近景，咬一小口，咬和咀嚼动作自然，发现馕硬后轻停一拍。'
    '最后一个镜头落到阿哲胸口以上，他低眼看着同一个纸袋和馕，接到尾帧。'
    '道具、视线、动作和光线在剪点连续，稳机位，不跨方向。站边只有现代人物和器物。'
    '画面中无帽冠、围巾、古装、骆驼、驼具、动物、帐篷、羊腿、字幕、题字和商标。';

const _continuityClip =
    '电影实拍，一镜到底。以两张参考图的人物身份、现代服装、棕纸袋和街边采光为准。'
    '在城市玻璃候车厅外，摄影机从阿哲左肩后平滑向左绕半步，沿幕墙缓推到面部三分之二侧的近景，'
    '构图最后落到尾帧。移动只在这几步范围内，始终拍向同一面玻璃站房和同一排停着的共享单车。'
    '岚姐穿锈棕西装，双臂松交叠，克制地看着阿哲，随后随着相机推进自然出了画。'
    '阿哲短黑发、浅灰米色摄影外套，深色相机肩带在同一侧。他从手中的棕纸袋取出一小块折开的薄馕，'
    '咬一口，嚼一下，发现有点硬，视线低下来。阿哲始终站在原处，纸袋一直在手里。'
    '近景用浅景深，静止的玻璃幕墙和停放的自行车填满背景，光线保持自然一致。静音画面。';

void main() {
  final binding = IntegrationTestWidgetsFlutterBinding.ensureInitialized();
  const session = String.fromEnvironment('FRAME_QA_SESSION');
  const root = String.fromEnvironment('FRAME_QA_OUT');
  const manualReplay = bool.fromEnvironment('FRAME_QA_MANUAL_REPLAY');
  testWidgets(
    'Repair one clip through its image prompts and play the old film',
    (tester) async {
      expect(Uri.parse(Env.apiBase).host, isIn(['127.0.0.1', 'localhost']));
      final evidence = await Directory(root).create(recursive: true);
      final total = Stopwatch()..start();
      void mark(String step) {
        debugPrint(
          'FRAME_QA ${DateTime.now().toIso8601String()} ${total.elapsed.inSeconds}s $step',
        );
        File('${evidence.path}/steps.log').writeAsStringSync(
          '${DateTime.now().toIso8601String()} ${total.elapsed.inSeconds}s $step\n',
          mode: FileMode.append,
        );
      }

      Finder key(String value) => find.byKey(Key(value));
      bool shown(String value) =>
          key(value).hitTestable().evaluate().isNotEmpty;

      Future<void> until(
        bool Function() ready,
        String step, {
        int seconds = 45,
      }) async {
        final end = DateTime.now().add(Duration(seconds: seconds));
        while (!ready() && DateTime.now().isBefore(end)) {
          await tester.pump(const Duration(milliseconds: 160));
          await Future<void>.delayed(const Duration(milliseconds: 200));
        }
        expect(ready(), isTrue, reason: step);
      }

      Future<void> tap(Finder target) async {
        await until(
          () => target.hitTestable().evaluate().isNotEmpty,
          'tap $target',
        );
        await tester.tap(target.hitTestable().first);
        await tester.pump(const Duration(milliseconds: 450));
      }

      Future<void> capture(String name) async {
        await tester.pump(const Duration(milliseconds: 450));
        await Future.wait([
          for (final element in find.byType(Image).hitTestable().evaluate())
            if ((element.widget as Image).image is NetworkImage)
              precacheImage(
                (element.widget as Image).image,
                element,
                onError: (_, _) {},
              ).timeout(const Duration(seconds: 8), onTimeout: () {}),
        ]);
        await Future<void>.delayed(const Duration(milliseconds: 460));
        await tester.pump();
        final bytes = await binding.takeScreenshot(name);
        await File('${evidence.path}/$name.png').writeAsBytes(bytes);
      }

      Finder scrollable(Finder within) =>
          find.descendant(of: within, matching: find.byType(Scrollable)).first;
      final list = key('production-shot-list');
      Future<void> top() async {
        tester.state<ScrollableState>(scrollable(list)).position.jumpTo(0);
        await tester.pump(const Duration(milliseconds: 350));
      }

      Future<void> openShot(int index) async {
        final target = key('production-edit-shot-$index');
        await top();
        await tester.scrollUntilVisible(
          target,
          480,
          scrollable: scrollable(list),
          maxScrolls: 18,
        );
        await tap(target);
      }

      await app.main();
      await tester.pump();
      final container = ProviderScope.containerOf(
        tester.element(find.byType(BossipApp)),
        listen: false,
      );
      if (container.read(authProvider).user?.username != 'devtest') {
        await container
            .read(authFlowProvider)
            .login(
              'devtest',
              const String.fromEnvironment('FRAME_QA_PASSWORD'),
            );
      }
      await container.read(uiModeProvider.notifier).setMode(UiMode.light);
      container.read(i18nProvider.notifier).setLanguage('zh-CN');
      final api = container.read(lightProductionApiProvider);
      String? submissionError;
      container
          .read(apiDioProvider)
          .interceptors
          .add(
            InterceptorsWrapper(
              onError: (error, handler) {
                if (error.requestOptions.path.contains('/shot-')) {
                  final body = error.response?.data;
                  submissionError = body is Map
                      ? '${body['detail']}'
                      : error.type.name;
                  mark('submission-error $submissionError');
                }
                handler.next(error);
              },
            ),
          );
      final original = await api.get(session);
      final oldFilm = original.shotWorkspace!.films.firstWhere(
        (film) => film.number == 2,
      );
      const baseline = String.fromEnvironment('FRAME_QA_BASELINE');
      if (baseline.isNotEmpty) expect(oldFilm.media.id, baseline);
      final initialFourth = original.shotWorkspace!.shots.firstWhere(
        (shot) => shot.index == 4,
      );
      expect(initialFourth.frameEditable, isTrue);

      final router = container.read(routerProvider);
      if (manualReplay) {
        router.go(Paths.home);
        await tap(key('light-production-history'));
        await tap(key('production-history-$session'));
      } else {
        router.go(Paths.lightProduction(session));
      }
      await until(
        () => shown('production-version-history'),
        'open original production',
      );
      mark('old-film-opened');
      if (original.shotWorkspace!.current!.number == oldFilm.number) {
        if (!original.shotWorkspace!.canCompose) {
          await openShot(5);
          final hide = key('production-hide-shot-title');
          await tester.scrollUntilVisible(
            hide,
            260,
            scrollable: scrollable(find.byType(ProductionShotPage)),
          );
          if (!tester.widget<SwitchListTile>(hide).value) await tap(hide);
          await until(
            () => tester.widget<SwitchListTile>(hide).value,
            'chapter title excluded',
          );
          await capture('01-hide-dog-title');
          Navigator.of(tester.element(hide)).pop();
          await tester.pump(const Duration(milliseconds: 550));
          mark('hide-title');
        }

        final redoMidshot =
            !manualReplay &&
            const bool.fromEnvironment('FRAME_QA_CONTINUITY') &&
            initialFourth.version?.number == 2;
        if (initialFourth.version?.number == 1 || redoMidshot) {
          await openShot(4);
          if (manualReplay) await capture('00-original-frames');
          Future<void> generate(
            String replacementKey,
            String prompt,
            String role,
          ) async {
            await tap(key(replacementKey));
            await tap(key('production-edit-frame-prompt'));
            final field = key('production-frame-prompt');
            await until(
              () => shown('production-frame-prompt'),
              'frame prompt editor',
            );
            await tester.enterText(field, prompt);
            FocusManager.instance.primaryFocus?.unfocus();
            await tester.pump(const Duration(milliseconds: 350));
            final use = key('production-use-current-frame');
            await tester.ensureVisible(use);
            if (tester.widget<CheckboxListTile>(use).value == true) {
              await tap(use);
            }
            if (role == 'head') await capture('02-frame-prompt');
            await tap(key('production-generate-frame'));
            await until(
              () =>
                  find.text('生成中').evaluate().isNotEmpty ||
                  find.text('已更换').evaluate().isNotEmpty,
              '$role image requested',
            );
            mark('$role-image-requested');
          }

          if (!redoMidshot &&
              initialFourth.draft(false)?.status != 'ready' &&
              initialFourth.draft(false)?.status != 'generating') {
            await generate('production-replace-head', _head, 'head');
          }
          if (!redoMidshot &&
              initialFourth.draft(true)?.status != 'ready' &&
              initialFourth.draft(true)?.status != 'generating') {
            await generate('production-replace-tail', _tail, 'tail');
          }
          if (find.text('生成中').evaluate().isNotEmpty) {
            await capture('03-images-generating');
          }
          if (!redoMidshot) {
            await until(
              () => find.text('已更换').evaluate().length == 2,
              'both generated images previewed',
              seconds: 400,
            );
            mark('both-images-ready');
            await capture('04-generated-images');
          }
          if (!redoMidshot && const bool.fromEnvironment('FRAME_QA_REVIEW')) {
            await until(
              () => File('${evidence.path}/approve').existsSync(),
              'review stills',
              seconds: 300,
            );
          }
          final prompt = key('production-shot-prompt-field');
          await tester.ensureVisible(prompt);
          await tester.enterText(
            prompt,
            redoMidshot || manualReplay ? _continuityClip : _clip,
          );
          FocusManager.instance.primaryFocus?.unfocus();
          await tester.pump(const Duration(milliseconds: 420));
          await tap(key('production-retake-shot'));
          mark(
            redoMidshot ? 'continuity-clip-requested' : 'only-clip-4-requested',
          );
          await until(
            () => key('production-confirm-compose').evaluate().isNotEmpty,
            'offer to assemble after regenerated clip',
            seconds: 500,
          );
        } else {
          await top();
          await tap(key('production-review-compose'));
        }
        final before = await api.get(session);
        expect(before.output?.id, oldFilm.media.id);
        expect(before.shotWorkspace!.pending.map((shot) => shot.index), [4]);
        expect(before.shotWorkspace!.titleEdits.map((shot) => shot.index), [5]);
        mark('new-clip-ready');
        await capture('05-compose-confirmation');
        if (manualReplay) {
          await tap(key('production-defer-compose'));
          await top();
          expect(find.text('当前成片 · v2'), findsOneWidget);
          expect((await api.get(session)).output?.id, oldFilm.media.id);
          mark('v2-still-current-while-deferred');
          final play = key('production-play-shot-4');
          await tester.scrollUntilVisible(
            play,
            470,
            scrollable: scrollable(list),
            maxScrolls: 18,
          );
          await tap(play);
          final player = find.byType(VideoPlayer);
          await until(
            () =>
                player.evaluate().isNotEmpty &&
                tester
                        .widget<VideoPlayer>(player.last)
                        .controller
                        .value
                        .position >
                    const Duration(seconds: 6),
            'watch through the middle of the repaired clip',
            seconds: 60,
          );
          await capture('09-clip-preview');
          Navigator.of(tester.element(player.last)).pop();
          await tester.pump(const Duration(milliseconds: 450));
          await top();
          mark('new-clip-previewed-in-app');
        }
        if (const bool.fromEnvironment('FRAME_QA_CONTINUITY')) {
          await until(
            () => File('${evidence.path}/approve-clip').existsSync(),
            'check full replacement clip',
            seconds: 300,
          );
        }
        if (manualReplay) await tap(key('production-review-compose'));
        await tap(key('production-confirm-compose'));
        mark('ims-compose-requested');
        await top();
        await until(
          () =>
              submissionError != null ||
              find.text('当前成片 · v3').evaluate().isNotEmpty,
          'revision three finished',
          seconds: 270,
        );
        expect(submissionError, isNull);
        mark('v3-ready');
      }

      await top();
      expect(find.text('当前成片 · v3'), findsOneWidget);
      final repaired = await api.get(session);
      final newFilm = repaired.shotWorkspace!.current!;
      expect(newFilm.number, 3);
      expect(
        newFilm.segments.keys.where(
          (i) => newFilm.segments[i] != oldFilm.segments[i],
        ),
        [4],
      );
      expect(
        repaired.shotWorkspace!.shots[3].version!.framePrompts.values,
        containsAll([_head, _tail]),
      );
      expect(repaired.shotWorkspace!.shots[4].titleHidden, isTrue);
      await openShot(4);
      await capture('08-repaired-frames');
      Navigator.of(tester.element(key('production-shot-prompt-toggle'))).pop();
      await tester.pump(const Duration(milliseconds: 450));
      await top();
      await tap(key('production-version-history'));
      await capture('06-film-history');
      await tap(find.text('成片 v2'));
      await until(
        () {
          final player = find.byType(VideoPlayer).evaluate();
          return player.isNotEmpty &&
              tester
                      .widget<VideoPlayer>(find.byType(VideoPlayer).last)
                      .controller
                      .value
                      .position >
                  const Duration(seconds: 2);
        },
        'old v2 playback',
        seconds: 65,
      );
      expect(
        tester
            .widget<VideoPlayer>(find.byType(VideoPlayer).last)
            .controller
            .value
            .duration
            .inMilliseconds,
        closeTo(62125, 250),
      );
      mark('v2-playback-ok');
      await capture('07-v2-playing');
    },
    skip: session.isEmpty || root.isEmpty,
    timeout: const Timeout(Duration(minutes: 24)),
  );
}
