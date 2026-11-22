import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:bossip_mobile/app/app.dart';
import 'package:bossip_mobile/app/router.dart';
import 'package:bossip_mobile/features/auth/state/auth_flow.dart';
import 'package:bossip_mobile/features/light/api/light_api.dart';
import 'package:bossip_mobile/features/light/production/production_api.dart';
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
import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:image_picker/image_picker.dart' show XFile;
import 'package:integration_test/integration_test.dart';
// Only the iOS microphone signal is substituted on hosts without input hardware.
// ignore: depend_on_referenced_packages
import 'package:record_platform_interface/record_platform_interface.dart';
import 'package:video_player/video_player.dart';

class _RecordingFilePicker extends FilePickerPlatform {
  _RecordingFilePicker(this.path);
  final String path;

  @override
  Future<List<PlatformFile>> pickFiles({
    String? dialogTitle,
    String? initialDirectory,
    FileType type = FileType.any,
    List<String>? allowedExtensions,
    void Function(FilePickerStatus)? onFileLoading,
    int compressionQuality = 0,
    AndroidOptions androidOptions = const AndroidOptions(),
    DarwinOptions darwinOptions = const DarwinOptions(),
    WindowsOptions windowsOptions = const WindowsOptions(),
    LinuxOptions linuxOptions = const LinuxOptions(),
    WebOptions webOptions = const WebOptions(),
  }) async {
    expect(type, FileType.custom);
    expect(allowedExtensions, contains('wav'));
    return [_PickedFile(path)];
  }
}

final class _PickedFile extends PlatformFile {
  _PickedFile(String path) : xFile = XFile(path);
  @override
  final XFile xFile;
  @override
  String get name => xFile.name;
  @override
  Uri get uri => Uri.file(xFile.path);
  @override
  int lengthSync() => File(xFile.path).lengthSync();
  @override
  Future<int> length() => xFile.length();
  @override
  Future<Uint8List> readAsBytes() => xFile.readAsBytes();
  @override
  Stream<Uint8List> readAsByteStream() => xFile.openRead();
}

class _FixtureMicrophone extends RecordPlatform {
  _FixtureMicrophone(this.sample);
  final String sample;
  String? _path;

  @override
  Future<void> create(String recorderId) async {}

  @override
  Future<bool> hasPermission(String recorderId, {bool request = true}) async =>
      true;

  @override
  Future<void> start(
    String recorderId,
    RecordConfig config, {
    required String path,
  }) async {
    expect(config.encoder, AudioEncoder.wav);
    _path = path;
  }

  @override
  Stream<RecordState> onStateChanged(String recorderId) => const Stream.empty();

  @override
  Future<bool> isRecording(String recorderId) async => _path != null;

  @override
  Future<Amplitude> getAmplitude(String recorderId) async =>
      Amplitude(current: -24, max: -8);

  @override
  Future<String?> stop(String recorderId) async {
    final path = _path;
    if (path == null) return null;
    _path = null;
    await File(sample).copy(path);
    return path;
  }

  @override
  Future<void> dispose(String recorderId) async {}

  @override
  dynamic noSuchMethod(Invocation invocation) => super.noSuchMethod(invocation);
}

void main() {
  final binding = IntegrationTestWidgetsFlutterBinding.ensureInitialized();
  const folder = String.fromEnvironment('NARRATION_QA_OUT');
  const fixture = String.fromEnvironment('NARRATION_QA_SOURCES');
  const only = String.fromEnvironment('NARRATION_QA_ONLY');
  const substituteMicrophone = bool.fromEnvironment('NARRATION_QA_FIXTURE_MIC');
  const savedClonedName = String.fromEnvironment('NARRATION_QA_CLONED_NAME');
  const resumeResult = String.fromEnvironment('NARRATION_QA_RESUME_RESULT');

  testWidgets('devtest drafts, auditions and renders voiceover on iOS', (
    tester,
  ) async {
    expect(Uri.parse(Env.apiBase).host, isIn(['localhost', '127.0.0.1']));
    final evidence = Directory(folder)..createSync(recursive: true);
    final watch = Stopwatch()..start();
    void mark(String value) {
      final line = '${watch.elapsed.inMilliseconds}ms $value';
      debugPrint('NARRATION_QA $line');
      File(
        '$folder/simulator.log',
      ).writeAsStringSync('$line\n', mode: FileMode.append);
    }

    Finder key(String name) => find.byKey(ValueKey(name));
    bool shown(String name) => key(name).hitTestable().evaluate().isNotEmpty;
    Future<void> screenshot(String name) async {
      File(
        '${evidence.path}/$name.png',
      ).writeAsBytesSync(await binding.takeScreenshot(name));
    }

    Future<void> until(
      bool Function() done,
      String what, {
      int timeout = 45,
    }) async {
      final end = DateTime.now().add(Duration(seconds: timeout));
      while (!done() && DateTime.now().isBefore(end)) {
        await tester.pump(const Duration(milliseconds: 150));
        await Future<void>.delayed(const Duration(milliseconds: 130));
      }
      if (!done()) await screenshot('failure-$what');
      expect(done(), isTrue, reason: what);
      mark(what);
    }

    Future<void> tapFinder(Finder target) async {
      await until(
        () => target.evaluate().isNotEmpty,
        'find-${target.describeMatch(Plurality.one)}',
      );
      await tester.ensureVisible(target.first);
      await tester.pump(const Duration(milliseconds: 200));
      await tester.tap(target.hitTestable().first);
      await tester.pump(const Duration(milliseconds: 390));
    }

    Future<void> tap(String name) => tapFinder(key(name));
    Future<void> text(String value) => tapFinder(find.text(value));
    bool playingVideo() => tester
        .widgetList<VideoPlayer>(find.byType(VideoPlayer))
        .any(
          (player) =>
              player.controller.value.isInitialized &&
              player.controller.value.position >
                  const Duration(milliseconds: 400),
        );

    await app.main();
    await tester.pump();
    final container = ProviderScope.containerOf(
      tester.element(find.byType(BossipApp)),
      listen: false,
    );
    await until(() => !container.read(authProvider).isLoading, 'auth-ready');
    if (container.read(authProvider).user?.username != 'devtest') {
      await container
          .read(authFlowProvider)
          .login(
            'devtest',
            File('$folder/.devtest-password').readAsStringSync().trim(),
          );
    }
    expect(container.read(authProvider).user?.username, 'devtest');
    await container.read(uiModeProvider.notifier).setMode(UiMode.light);
    container.read(i18nProvider.notifier).setLanguage('zh-CN');
    final router = container.read(routerProvider);
    final api = container.read(lightProductionApiProvider);
    final dio = container.read(apiDioProvider);
    final raw =
        jsonDecode(File(fixture).readAsStringSync()) as Map<String, dynamic>;
    final sources = (raw['sources'] as List<dynamic>)
        .cast<Map<String, dynamic>>();
    final ids = sources.map((row) => row['id'] as String).toList();

    Future<LightProduction> start(String entry, {int count = 4}) async {
      router.go(Paths.home);
      await tester.pump(const Duration(milliseconds: 400));
      await tap('light-entry-$entry');
      await until(() => shown('production-shop'), 'materials-$entry');
      final page = tester.widget<ProductionPage>(find.byType(ProductionPage));
      final task = await api.get(page.sessionId);
      final saved = await api.edit(task, {
        'shop': '塞外朵莱娜',
        'asset_ids': ids.take(count).toList(),
      });
      // Load the devtest library fixtures back into the guided page.
      router.go(Paths.home);
      await tester.pump(const Duration(milliseconds: 400));
      router.go(Paths.lightProduction(page.sessionId));
      await tester.pump(const Duration(milliseconds: 400));
      await until(
        () => find.text('塞外朵莱娜').evaluate().isNotEmpty,
        'fixtures-$entry',
      );
      return saved;
    }

    Future<void> saveFilm(
      LightProduction task,
      String label, {
      int timeout = 330,
    }) async {
      await until(
        () =>
            shown('production-result-preview') ||
            find.text('重试制作').hitTestable().evaluate().isNotEmpty,
        '$label-settled',
        timeout: timeout,
      );
      var ready = await api.get(task.sessionId);
      // A finished IMS asset can appear briefly before the guided Agent receipt settles.
      for (var attempt = 0; ready.working && attempt < 30; attempt++) {
        await tester.pump(const Duration(milliseconds: 400));
        await Future<void>.delayed(const Duration(milliseconds: 400));
        ready = await api.get(task.sessionId);
      }
      expect(ready.status, 'completed');
      expect(ready.redoNarration, label == 'replacement');
      final asset = ready.output!.id;
      mark('$label-output $asset');
      final data = (await dio.get<Map<String, dynamic>>(
        '/api/assets/$asset/url',
      )).data!;
      await Dio().download(data['url'] as String, '$folder/$label.mp4');
      await tap('production-result-preview');
      await until(playingVideo, '$label-playback');
      await screenshot('$label-playback');
      await tap('media-viewer-close');
    }

    if (only == 'voice-entry-settings') {
      final video = await start('video', count: 1);
      expect(video.voiceId, isEmpty);
      expect(key('production-voice-settings'), findsNothing);
      if (resumeResult.isNotEmpty) {
        final savedVideo = await api.get(resumeResult);
        expect(savedVideo.kind, ProductionKind.video);
        expect(savedVideo.voiceId, isEmpty);
        expect(savedVideo.output, isNotNull);
      }
      await screenshot('video-settings');

      final edit = await start('footage_edit', count: 1);
      expect(edit.redoNarration, isFalse);
      await tap('production-voice-settings');
      await until(
        () => shown('production-voice-system:documentary'),
        'footage-voices',
      );
      await tap('production-my-voices');
      if (savedClonedName.isNotEmpty) {
        await until(
          () => shown('production-clone-$savedClonedName'),
          'footage-saved-clone',
        );
      }
      await screenshot('footage-voices');
      return;
    }

    if (only == 'soda') {
      final task = await start('video');
      final sid = task.sessionId;
      File('$folder/session.txt').writeAsStringSync(sid);
      mark('soda-session $sid');
      await tap('production-tier-picker');
      await tap('production-model-medium');
      expect(key('production-voice-settings'), findsNothing);
      await screenshot('soda-settings');
      await tap('production-primary');
      await text('复刻爆款');
      await tap('production-template-soda_story');
      await tap('production-primary');
      const copy =
          '做一条塞外朵莱娜的餐饮宣传，主打片中实拍的丝路香馕烤羊腿。保留参考案例的生活化叙事和最后的菜品亮相，门店和菜品按实拍素材。';
      await until(() => shown('production-script'), 'soda-review');
      await tester.enterText(key('production-script'), copy);
      FocusManager.instance.primaryFocus?.unfocus();
      await tester.pump(const Duration(milliseconds: 500));
      mark('soda-prepare-tap');
      await tap('production-primary');
      await until(
        () => shown('production-remake-script'),
        'soda-plan-ready',
        timeout: 240,
      );
      final ready = await api.get(sid);
      expect(ready.remakePrepared, isTrue);
      expect(ready.template, 'soda_story');
      expect(ready.voiceId, isEmpty);
      expect(ready.videoTier, ProductionVideoTier.medium);
      expect(ready.assets, ids);
      expect(ready.referenceCopy, copy);
      mark('soda-render-tap');
      await tap('production-primary');
      await saveFilm(ready, 'soda', timeout: 630);
      mark('soda-done');
      return;
    }

    if (only == 'soda-retry' && resumeResult.isNotEmpty) {
      final task = await api.get(resumeResult);
      expect(task.status, 'failed');
      expect(task.remakePrepared, isTrue);
      expect(task.voiceId, isEmpty);
      router.go(Paths.lightProduction(resumeResult));
      await tester.pump(const Duration(milliseconds: 520));
      await until(
        () => find.text('重试制作').evaluate().isNotEmpty,
        'soda-retry-ready',
      );
      mark('soda-retry-tap');
      await tap('production-primary');
      await until(
        () => find.text('重试制作').hitTestable().evaluate().isEmpty,
        'soda-retry-submitted',
      );
      await saveFilm(task, 'soda', timeout: 630);
      mark('soda-done');
      return;
    }

    if (only == 'soda-result' && resumeResult.isNotEmpty) {
      router.go(Paths.lightProduction(resumeResult));
      await tester.pump(const Duration(milliseconds: 520));
      await saveFilm(await api.get(resumeResult), 'soda');
      return;
    }

    if (only == 'resume' && resumeResult.isNotEmpty) {
      router.go(Paths.lightProduction(resumeResult));
      await tester.pump(const Duration(milliseconds: 500));
      await saveFilm(await api.get(resumeResult), 'replacement');
      return;
    }

    if (only.isEmpty || only == 'original') {
      final task = await start('footage_edit');
      mark('original-session ${task.sessionId}');
      await tap('production-primary');
      await until(
        () => find.text('检测到原口播').evaluate().isNotEmpty,
        'source-detected',
      );
      final toggle = tester.widget<SwitchListTile>(
        key('production-redo-narration'),
      );
      expect(toggle.value, isFalse);
      await tester.ensureVisible(key('production-redo-narration'));
      await tester.pump(const Duration(milliseconds: 370));
      await screenshot('original-default');
      mark('original-submit');
      await tap('production-primary');
      await saveFilm(task, 'original');
    }

    if (only.isEmpty || only == 'replacement') {
      final task = await start('footage_edit');
      mark('replacement-session ${task.sessionId}');
      await tap('production-primary');
      await until(
        () => find.text('检测到原口播').evaluate().isNotEmpty,
        'replacement-detected',
      );
      await tap('production-redo-narration');
      await tap('production-primary');
      await until(
        () => shown('production-narration-emphasis'),
        'source-script',
      );
      await tester.enterText(
        key('production-narration-emphasis'),
        '突出招牌羊腿的实拍细节，保留原稿的到店邀请。',
      );
      FocusManager.instance.primaryFocus?.unfocus();
      await tester.pump();
      await tap('production-voice-primary');
      await until(
        () => shown('production-narration-line-0'),
        'shot-aligned-script',
        timeout: 90,
      );
      await screenshot('shot-script');
      await tap('production-narration-line-0');
      final input = key('production-narration-line-input');
      final existing = tester.widget<TextFormField>(input).initialValue!;
      await tester.enterText(
        input,
        existing.endsWith('。')
            ? existing.replaceFirst(RegExp(r'。$'), '！')
            : '$existing。',
      );
      await text('保存');
      await tap('production-voice-primary');
      await tap('production-voice-system:documentary');
      await tap('production-voice-primary');
      await until(
        () =>
            find.text('随画面试听').evaluate().isNotEmpty ||
            find.textContaining('缩短后再试听').evaluate().isNotEmpty ||
            find.textContaining('与画面不符').evaluate().isNotEmpty,
        'timed-preview',
        timeout: 140,
      );
      expect(find.text('随画面试听'), findsOneWidget);
      await until(playingVideo, 'timed-preview-playing');
      await screenshot('timed-preview');
      final speech = await api.speech(task.sessionId);
      final preview = speech['preview'] as Map;
      await Dio().download(
        preview['url'] as String,
        '$folder/replacement-preview.mp4',
      );
      expect((preview['lines'] as List).first, isNot(existing));
      mark('replacement-submit');
      await tap('production-voice-primary');
      await saveFilm(task, 'replacement');
    }

    if (only.isEmpty || only == 'settings' || only == 'microphone') {
      final task = await start('footage_edit', count: 1);
      await tap('production-voice-settings');
      await until(
        () => shown('production-voice-system:documentary'),
        'system-voices',
      );
      expect(find.text('自然纪实'), findsOneWidget);
      expect(find.text('亲切明亮'), findsOneWidget);
      expect(find.text('活力促销'), findsOneWidget);
      expect(find.text('沉稳质感'), findsOneWidget);
      await screenshot('system-voices');
      final previous = FilePickerPlatform.instance;
      FilePickerPlatform.instance = _RecordingFilePicker('$folder/sample.wav');
      addTearDown(() => FilePickerPlatform.instance = previous);
      await tap('production-my-voices');
      if (substituteMicrophone) {
        final recorder = RecordPlatform.instance;
        RecordPlatform.instance = _FixtureMicrophone('$folder/sample.wav');
        addTearDown(() => RecordPlatform.instance = recorder);
        mark('microphone input supplied by test fixture');
      }
      Future<String?> clone(bool recording) async {
        final name =
            'devtest ${recording ? '录音' : '上传'} ${DateTime.now().millisecondsSinceEpoch % 1000000}';
        await tap('production-voice-primary');
        await tap(
          recording ? 'production-record-voice' : 'production-upload-voice',
        );
        if (recording) {
          await tap('production-voice-primary');
          await until(
            () => shown('production-recording'),
            'native-microphone-recording',
          );
          await screenshot('native-recording');
          await Future<void>.delayed(
            Duration(seconds: substituteMicrophone ? 7 : 20),
          );
          await tester.pump(const Duration(seconds: 1));
          await tap('production-voice-primary');
        }
        await until(() => shown('production-clone-name'), 'sample-review');
        await tester.enterText(key('production-clone-name'), name);
        FocusManager.instance.primaryFocus?.unfocus();
        await tester.pump(const Duration(milliseconds: 300));
        await tap('production-voice-consent');
        await screenshot(recording ? 'recorded-sample' : 'uploaded-sample');
        await tap('production-sample-play');
        await until(
          () => find.byTooltip('暂停').evaluate().isNotEmpty,
          'sample-playing',
          timeout: 20,
        );
        await tap('production-voice-primary');
        final item = key('production-clone-$name');
        await until(
          () =>
              item.evaluate().isNotEmpty ||
              find.text('声音服务暂时无法完成请求，请重试。').evaluate().isNotEmpty,
          recording ? 'native-clone-submitted' : 'uploaded-clone-submitted',
          timeout: 55,
        );
        if (item.evaluate().isEmpty) {
          await screenshot('native-clone-failure');
          expect(recording, isTrue);
          return null;
        }
        await until(
          () => find
              .descendant(of: item, matching: find.text('已就绪'))
              .evaluate()
              .isNotEmpty,
          recording ? 'native-clone-ready' : 'uploaded-clone-ready',
          timeout: 65,
        );
        await tapFinder(
          find.descendant(of: item, matching: find.byTooltip('试听')),
        );
        await until(
          () => find
              .descendant(of: item, matching: find.byTooltip('暂停'))
              .evaluate()
              .isNotEmpty,
          recording ? 'native-voice-audible' : 'uploaded-voice-audible',
          timeout: 30,
        );
        await screenshot(
          recording ? 'native-clone-ready' : 'uploaded-clone-ready',
        );
        return name;
      }

      if (only != 'microphone') {
        final uploaded = savedClonedName.isEmpty
            ? (await clone(false))!
            : savedClonedName;
        final selected = substituteMicrophone ? (await clone(true))! : uploaded;
        await tap('production-clone-$selected');
        await tap('production-voice-primary');
        await until(
          () => find.text(selected).evaluate().isNotEmpty,
          'cloned-voice-selected',
        );
        await tap('production-primary');
        expect((await api.get(task.sessionId)).voiceId, startsWith('voice:'));
        final video = await start('video', count: 1);
        expect(key('production-voice-settings'), findsNothing);
        await tap('production-primary');
        await until(
          () => shown('production-video-template-sections'),
          'template-sections',
        );
        await text('模板库');
        final examples = await container
            .read(lightApiProvider)
            .productionExamples('video');
        final example = examples.firstWhere((row) => row.scene == 'dish');
        await tap('production-video-category-dish');
        await tap('production-example-${example.exampleId}');
        await tap('production-voice-primary');
        await until(
          () => find.textContaining(example.title).evaluate().isNotEmpty,
          'grouped-video-template-selected',
        );
        expect(
          (await api.get(video.sessionId)).referenceExampleId,
          example.exampleId,
        );
        await screenshot('video-template-library');
        await text('复刻爆款');
        await tap('production-soda-preview');
        await until(playingVideo, 'soda-reference-played');
        await tap('media-viewer-close');
        await tap('production-silk-road-preview');
        await until(playingVideo, 'silk-reference-played');
        await tap('media-viewer-close');
        await tap('production-template-soda_story');
        await until(
          () => find.text('使用这个爆款案例').evaluate().isNotEmpty,
          'system-remake-selected',
        );
        await screenshot('remake-cases');
        await tap('production-primary');
        await until(
          () =>
              (find.byType(ProductionPage).evaluate().isNotEmpty) &&
              !shown('production-video-template-sections'),
          'system-remake-review',
        );
        final remake = await api.get(video.sessionId);
        expect(remake.template, 'soda_story');
        expect(remake.referenceExampleId, isNull);
        expect(remake.voiceId, isEmpty);
      } else {
        final recorded = await clone(true);
        expect(
          recorded,
          isNotNull,
          reason: 'iOS mic sample enrolled through the live voice service',
        );
        await tap('production-clone-$recorded');
        await tap('production-voice-primary');
        await tap('production-primary');
        expect((await api.get(task.sessionId)).voiceId, startsWith('voice:'));
      }
    }
    if (resumeResult.isNotEmpty) {
      router.go(Paths.lightProduction(resumeResult));
      await tester.pump(const Duration(milliseconds: 520));
      await saveFilm(await api.get(resumeResult), 'replacement');
    }
    mark('done');
  });
}
