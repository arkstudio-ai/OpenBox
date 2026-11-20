import 'dart:io';

import 'package:bossip_mobile/features/light/production/production_media.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/widgets/media_viewer.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:integration_test/integration_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:video_player/video_player.dart';

void main() {
  final binding = IntegrationTestWidgetsFlutterBinding.ensureInitialized();
  const screenshotDirectory = String.fromEnvironment('MEDIA_VIEWER_QA_OUT');

  testWidgets('the same full-screen player fits wide and portrait samples', (
    tester,
  ) async {
    final bundle = await I18nBundle.load();
    final prefs = await SharedPreferences.getInstance();
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          i18nProvider.overrideWith(() => I18nController(bundle, prefs)),
        ],
        child: MaterialApp(
          theme: ThemeData(
            extensions: [
              BossipTokens.resolve(BossipThemeName.default_, Brightness.light),
            ],
          ),
          home: Scaffold(
            body: Builder(
              builder: (context) => Column(
                children: [
                  for (final (name, asset) in [
                    ('wide', silkRoadVideo),
                    ('portrait', crawfishCaseVideo),
                  ])
                    TextButton(
                      onPressed: () =>
                          showProductionVideo(context, asset, bundled: true),
                      child: Text(name),
                    ),
                ],
              ),
            ),
          ),
        ),
      ),
    );

    Future<void> waitFor(bool Function() ready) async {
      for (var i = 0; i < 60 && !ready(); i++) {
        await tester.pump(const Duration(milliseconds: 100));
        await Future<void>.delayed(const Duration(milliseconds: 100));
      }
      expect(ready(), isTrue);
    }

    for (final name in ['wide', 'portrait']) {
      await tester.tap(find.text(name));
      final video = find.byType(VideoPlayer);
      final pause = find.byIcon(Icons.pause_rounded).hitTestable();
      await waitFor(() => pause.evaluate().isNotEmpty);
      await tester.pumpAndSettle();
      expect(video, findsOneWidget);
      expect(find.byType(MediaViewer), findsOneWidget);
      final size = tester.getSize(video);
      expect(
        size.aspectRatio,
        closeTo(name == 'wide' ? 960 / 378 : 9 / 16, .01),
      );
      final controller = tester.widget<VideoPlayer>(video).controller;
      expect(controller.value.isPlaying, isTrue);
      await tester.tap(pause);
      await tester.pump();
      expect(controller.value.isPlaying, isFalse);
      final progress = tester.getRect(find.byType(VideoProgressIndicator));
      await tester.tapAt(progress.center);
      await waitFor(() => controller.value.position.inSeconds > 0);
      if (screenshotDirectory.isNotEmpty) {
        final bytes = await binding.takeScreenshot('media-viewer-$name');
        await File(
          '$screenshotDirectory/media-viewer-$name.png',
        ).writeAsBytes(bytes);
      }
      await tester.tap(find.byKey(const Key('media-viewer-close')));
      await tester.pumpAndSettle();
      expect(find.byType(MediaViewer), findsNothing);
      expect(tester.takeException(), isNull);
    }
  });
}
