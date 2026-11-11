import 'package:bossip_mobile/features/light/widgets/autopilot_sheet.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/models/light_autopilot.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

Future<List<LightAutopilotChoice?>> _mountSheet(
  WidgetTester tester, {
  required bool canPublish,
}) async {
  SharedPreferences.setMockInitialValues({'bossip:lang': 'zh-CN'});
  final prefs = await SharedPreferences.getInstance();
  final bundle = (await tester.runAsync(I18nBundle.load))!;
  final answers = <LightAutopilotChoice?>[];
  await tester.pumpWidget(
    ProviderScope(
      overrides: [
        prefsProvider.overrideWithValue(prefs),
        i18nProvider.overrideWith(() => I18nController(bundle, prefs)),
      ],
      child: MaterialApp(
        theme: ThemeData(
          extensions: [
            BossipTokens.resolve(BossipThemeName.default_, Brightness.light),
          ],
        ),
        home: Builder(
          builder: (context) => Scaffold(
            body: TextButton(
              key: const Key('open'),
              onPressed: () async => answers.add(
                await showLightAutopilotSheet(
                  context,
                  canPublish: canPublish,
                  allowPublish: true,
                ),
              ),
              child: const Text('open'),
            ),
          ),
        ),
      ),
    ),
  );
  await tester.tap(find.byKey(const Key('open')));
  await tester.pumpAndSettle();
  return answers;
}

void main() {
  test('an autopilot reads the server view', () {
    final job = LightAutopilot.fromJson({
      'id': 'cron_1',
      'task': 'video',
      'enabled': true,
      'mode': 'make_only',
      'session_id': 'session_1',
      'source_asset_id': 'asset_1',
      'repeat': 'weekdays',
      'time': '08:00',
      'credits_cap_per_run': '20',
      'shop_name': '巷口面馆',
      'next_run_at': '2026-11-03T02:00:00+00:00',
      'last_status': 'ok',
      'total_runs': 3,
    })!;
    expect(job.repeat, 'weekdays');
    expect(job.time, '08:00');
    expect(job.creditsCap, '20');
    expect(job.shopName, '巷口面馆');
    expect(job.nextRunAt, isNotNull);
    expect(job.totalRuns, 3);
    expect(LightAutopilot.fromJson({'task': 'video'}), isNull);
  });

  testWidgets('the sheet starts on 每天 10:00 · 只出片 · 10 积分', (tester) async {
    final answers = await _mountSheet(tester, canPublish: false);
    expect(find.text('以后省心托管这个'), findsOneWidget);
    // Without a cloud desktop the publish answer shows, locked.
    expect(find.text('直接发（需要云电脑）'), findsOneWidget);
    await tester.tap(find.byKey(const Key('light-autopilot-mode-publish')));
    await tester.tap(find.byKey(const Key('light-autopilot-confirm')));
    await tester.pumpAndSettle();
    final choice = answers.single!;
    expect(
      (choice.repeat, choice.time, choice.mode, choice.creditsCap),
      ('daily', '10:00', 'make_only', '10'),
    );
  });

  testWidgets('with a desktop the answers change', (tester) async {
    final answers = await _mountSheet(tester, canPublish: true);
    await tester.tap(find.byKey(const Key('light-autopilot-repeat-weekdays')));
    await tester.tap(find.byKey(const Key('light-autopilot-time-18:00')));
    await tester.tap(find.byKey(const Key('light-autopilot-mode-publish')));
    await tester.tap(find.byKey(const Key('light-autopilot-cap-20')));
    await tester.pump();
    await tester.tap(find.byKey(const Key('light-autopilot-confirm')));
    await tester.pumpAndSettle();
    final choice = answers.single!;
    expect(
      (choice.repeat, choice.time, choice.mode, choice.creditsCap),
      ('weekdays', '18:00', 'make_and_publish', '20'),
    );
  });
}
