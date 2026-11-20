import 'dart:io';

import 'package:bossip_mobile/app/app.dart';
import 'package:bossip_mobile/app/router.dart';
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
// Provide the selected real files at the simulator's system-picker boundary.
// ignore: depend_on_referenced_packages
import 'package:image_picker_platform_interface/image_picker_platform_interface.dart';
import 'package:integration_test/integration_test.dart';

class _MenuPhotoPicker extends ImagePickerPlatform {
  _MenuPhotoPicker(this.files);

  final List<XFile> files;

  @override
  Future<List<XFile>> getMultiImageWithOptions({
    MultiImagePickerOptions options = const MultiImagePickerOptions(),
  }) async => files;
}

void main() {
  final binding = IntegrationTestWidgetsFlutterBinding.ensureInitialized();
  const output = String.fromEnvironment('MENU_QA_OUT');
  const resume = String.fromEnvironment('MENU_QA_RESUME');
  const photos = String.fromEnvironment('MENU_QA_PHOTOS');

  testWidgets(
    'The App previews menu templates and renders confirmed dish information',
    (tester) async {
      expect(Uri.parse(Env.apiBase).host, isIn(['localhost', '127.0.0.1']));
      final evidence = Directory(output)..createSync(recursive: true);
      final watch = Stopwatch()..start();

      Finder key(String name) => find.byKey(Key(name));
      bool shown(String name) => key(name).hitTestable().evaluate().isNotEmpty;

      Future<void> capture(String name) async {
        await tester.pump(const Duration(milliseconds: 350));
        File(
          '${evidence.path}/$name.png',
        ).writeAsBytesSync(await binding.takeScreenshot(name));
      }

      Future<void> until(
        bool Function() ready,
        String label, {
        int seconds = 30,
      }) async {
        final end = DateTime.now().add(Duration(seconds: seconds));
        while (!ready() && DateTime.now().isBefore(end)) {
          await tester.pump(const Duration(milliseconds: 150));
          await Future<void>.delayed(const Duration(milliseconds: 150));
        }
        if (!ready()) await capture('failure-$label');
        expect(ready(), isTrue, reason: label);
        debugPrint('MENU_QA ${watch.elapsed.inSeconds}s $label');
      }

      Future<void> tap(String name) async {
        await until(() => key(name).evaluate().isNotEmpty, name);
        await tester.ensureVisible(key(name).first);
        await tester.pump(const Duration(milliseconds: 180));
        await until(() => shown(name), 'visible-$name');
        await tester.tap(key(name).hitTestable().first);
        await tester.pump(const Duration(milliseconds: 350));
      }

      await app.main();
      await tester.pump();
      final container = ProviderScope.containerOf(
        tester.element(find.byType(BossipApp)),
        listen: false,
      );
      await until(() => !container.read(authProvider).isLoading, 'signed-in');
      expect(container.read(authProvider).user?.username, 'devtest');
      final withPhotos = photos.isNotEmpty;
      final shop = withPhotos ? '塞外朵莱娜' : '乡味小馆';
      final selectedTemplate = withPhotos ? 'bilingual_table' : 'price_columns';
      final dishes = withPhotos
          ? const [('丝路香馕烤羊腿', '')]
          : const [
              ('香菇滑鸡', '¥48'),
              ('糖醋里脊', '¥42'),
              ('地三鲜', '¥28'),
              ('清炒时蔬', '¥18'),
              ('凉拌黄瓜', '¥16'),
              ('白灼菜心', '¥22'),
            ];
      late final String session;
      if (resume.isNotEmpty) {
        session = resume;
        container.read(routerProvider).go(Paths.lightProduction(session));
        await until(() => shown('production-primary'), 'resume');
      } else {
        container.read(routerProvider).go(Paths.home);
        await tap('light-entry-menu');
        await until(() => shown('production-dish-add'), 'menu-draft');
        session = tester
            .widget<ProductionPage>(find.byType(ProductionPage))
            .sessionId;
        File('${evidence.path}/session.txt').writeAsStringSync(session);
        await tester.enterText(key('production-shop'), shop);

        for (final (name, price) in dishes) {
          await tap('production-dish-add');
          await tester.enterText(key('production-dish-name'), name);
          await tester.enterText(key('production-dish-price'), price);
          await tap('production-dish-save');
        }
        FocusManager.instance.primaryFocus?.unfocus();
        if (withPhotos) {
          final original = ImagePickerPlatform.instance;
          final files = [
            for (final label in ['羊腿', '香馕'])
              XFile('$photos/${label}实拍关键帧.png'),
          ];
          expect(files.every((file) => File(file.path).existsSync()), isTrue);
          ImagePickerPlatform.instance = _MenuPhotoPicker(files);
          addTearDown(() => ImagePickerPlatform.instance = original);
          await tap('production-source-upload');
          await until(
            () => find.text('已选 2 个素材').evaluate().isNotEmpty,
            'two-source-photos',
            seconds: 90,
          );
          await capture('materials');
        }
        await tap('production-primary');

        for (final template
            in withPhotos
                ? [selectedTemplate]
                : ['signature_plate', 'bilingual_table', selectedTemplate]) {
          await tap('production-preview-$template');
          await until(() => shown('media-viewer-close'), '${template}_open');
          await capture(template);
          await tap('media-viewer-close');
          await tap('production-template-$template');
        }
        await capture('templates');
        await tap('production-primary');
        await until(
          () =>
              find.text('核对菜品').evaluate().isNotEmpty &&
              !shown('production-progress'),
          'confirm',
          seconds: 105,
        );
        await capture('review');
      }
      final api = container.read(lightProductionApiProvider);
      final draft = await api.get(session);
      expect(draft.template, selectedTemplate);
      expect(draft.shop, shop);
      if (withPhotos) expect(draft.materials.length, 2);
      expect(draft.dishes.map((dish) => (dish.name, dish.price)), dishes);

      watch.reset();
      await tap('production-primary');
      await until(
        () =>
            shown('production-result-preview') ||
            find.text('重试制作').hitTestable().evaluate().isNotEmpty,
        'generated',
        seconds: 180,
      );
      final result = await api.get(session);
      if (result.status != 'completed') await capture('render-failed');
      expect(result.status, 'completed');
      expect(result.output?.mime, startsWith('image/'));
      await tap('production-result-preview');
      await until(() => shown('media-viewer-close'), 'played-preview');
      await capture('result');
      final object =
          (await container
                  .read(apiDioProvider)
                  .get<Map<String, dynamic>>(
                    '/api/assets/${result.output!.id}/url',
                  ))
              .data!;
      await Dio().download(
        object['url'] as String,
        '${evidence.path}/generated-menu.png',
      );
      await tap('media-viewer-close');
    },
    skip: output.isEmpty,
  );
}
