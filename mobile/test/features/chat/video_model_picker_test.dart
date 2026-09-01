import 'package:bossip_mobile/features/chat/state/chat_session_controller.dart';
import 'package:bossip_mobile/features/chat/state/config_providers.dart';
import 'package:bossip_mobile/features/chat/widgets/composer/picker_sheets.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/models/app_config.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

I18nBundle _bundle() => I18nBundle({
  'en-US': {
    'chat': {
      'videoModel': {'pick': 'Switch video model'},
    },
  },
});

const _config = AppConfig(
  models: [],
  videoModels: [
    VideoModelInfo(
      id: 'wan3.0-video',
      name: 'Wan 3.0',
      tier: '标准',
      resolutions: ['480p', '720p', '1080p'],
    ),
    VideoModelInfo(
      id: 'video-sd-1080p-pro',
      name: 'SD 1080p Pro',
      tier: '高清',
      resolutions: ['1080p'],
    ),
  ],
  defaultVideoModel: 'wan3.0-video',
  defaultVideoResolution: '720p',
);

/// Opens the picker the way the composer pill does, so the test exercises the
/// real sheets rather than a stand-in.
Future<ProviderContainer> _open(WidgetTester tester) async {
  SharedPreferences.setMockInitialValues({});
  final prefs = await SharedPreferences.getInstance();
  late ProviderContainer container;

  await tester.pumpWidget(
    ProviderScope(
      overrides: [
        i18nProvider.overrideWith(() => I18nController(_bundle(), prefs)),
        // A synchronous value, not a Future: the sheet reads
        // `appConfigProvider.valueOrNull` the moment it opens, and an
        // override that is still resolving reads as "no models" and returns
        // without showing anything.
        appConfigProvider.overrideWith((ref) => _config),
      ],
      child: MaterialApp(
        theme: ThemeData(
          extensions: [
            BossipTokens.resolve(BossipThemeName.default_, Brightness.light),
          ],
        ),
        home: Consumer(
          builder: (context, ref, _) {
            container = ProviderScope.containerOf(context);
            return Scaffold(
              body: Center(
                child: ElevatedButton(
                  onPressed: () => showVideoModelPicker(
                    context,
                    ref,
                    sessionKey: 's1',
                    currentModel: null,
                    currentResolution: null,
                  ),
                  child: const Text('open'),
                ),
              ),
            );
          },
        ),
      ),
    ),
  );
  await tester.pumpAndSettle();
  await tester.tap(find.text('open'));
  await tester.pumpAndSettle();
  return container;
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  testWidgets('lists every model with its own tiers', (tester) async {
    await _open(tester);

    expect(find.text('Switch video model'), findsOneWidget);
    expect(find.text('Wan 3.0'), findsOneWidget);
    expect(find.text('SD 1080p Pro'), findsOneWidget);
    // The subtitle carries the tier and the resolutions that model offers, so
    // the tiers are visible before opening anything.
    expect(find.text('标准 · 480p / 720p / 1080p'), findsOneWidget);
    expect(find.text('高清 · 1080p'), findsOneWidget);
  });

  testWidgets('a multi-tier model opens a second sheet and records the pair',
      (tester) async {
    final container = await _open(tester);

    await tester.tap(find.text('Wan 3.0'));
    await tester.pumpAndSettle();

    // Second sheet: the model's own tiers, nothing else.
    expect(find.text('480p'), findsOneWidget);
    expect(find.text('1080p'), findsOneWidget);

    await tester.tap(find.text('1080p'));
    await tester.pumpAndSettle();

    final pick = container.read(pickedVideoProvider('s1'));
    expect(pick?.modelId, 'wan3.0-video');
    expect(pick?.resolution, '1080p');
  });

  testWidgets('the model you are on still advertises its other tiers',
      (tester) async {
    // Found on device: the active row showed only a check, so the one model
    // whose resolution you are most likely to change read as the one model
    // that could not be changed. It always could — the affordance was missing,
    // not the behaviour, which is why the existing tests all passed.
    final container = await _open(tester);

    await tester.tap(find.text('Wan 3.0'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('1080p'));
    await tester.pumpAndSettle();
    expect(container.read(pickedVideoProvider('s1'))?.resolution, '1080p');

    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();

    final activeRow = find.ancestor(
      of: find.text('Wan 3.0'),
      matching: find.byType(ListTile),
    );
    expect(find.descendant(of: activeRow, matching: find.byIcon(Icons.check)),
        findsOneWidget);
    expect(
      find.descendant(
          of: activeRow, matching: find.byIcon(Icons.chevron_right)),
      findsOneWidget,
      reason: 'the active row opens the tiers, so it must look like it does',
    );

    // The single-tier model has nothing behind it, so it keeps no chevron.
    final singleRow = find.ancestor(
      of: find.text('SD 1080p Pro'),
      matching: find.byType(ListTile),
    );
    expect(
      find.descendant(
          of: singleRow, matching: find.byIcon(Icons.chevron_right)),
      findsNothing,
    );
  });

  testWidgets('a single-tier model is chosen outright, with no second sheet',
      (tester) async {
    // Nothing to choose there, so a second step would only be a step.
    final container = await _open(tester);

    await tester.tap(find.text('SD 1080p Pro'));
    await tester.pumpAndSettle();

    expect(find.text('SD 1080p Pro'), findsNothing, reason: 'sheet closed');
    final pick = container.read(pickedVideoProvider('s1'));
    expect(pick?.modelId, 'video-sd-1080p-pro');
    expect(pick?.resolution, '1080p');
  });
}
