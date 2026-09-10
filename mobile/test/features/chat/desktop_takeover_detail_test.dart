import 'package:bossip_mobile/features/chat/widgets/cards/desktop_takeover_detail.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/events/bus.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/models/interaction.dart';
import 'package:bossip_mobile/shared/router/paths.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

I18nBundle _bundle() => I18nBundle({
  'en-US': {
    'chat': {
      'takeover': {
        'reasonLabel': 'Blocked by',
        'site': 'Site',
        'open': 'Open the cloud desktop',
        'openHint': 'Control switches on.',
        'ownBrowser': 'Finish it in your own browser.',
        'reason': {'captcha_slider': 'slider captcha', 'other': 'human check'},
      },
    },
  },
});

const _local = {
  'kind': 'desktop_takeover',
  'reason': 'captcha_slider',
  'url': 'https://login.taobao.com/member/login.jhtml',
  'host': 'login.taobao.com',
  'browser': 'local',
};

/// Records emits synchronously: awaiting a real stream subscription inside
/// the widget test's fake async zone never completes.
class _RecordingBus extends AppEventBus {
  final seen = <AppEvent>[];

  @override
  void emit(String type, [Map<String, Object?> payload = const {}]) {
    seen.add(AppEvent(type, payload));
  }
}

Future<_RecordingBus> _pump(WidgetTester tester, Map<String, dynamic> detail) async {
  SharedPreferences.setMockInitialValues({'bossip:lang': 'en-US'});
  final prefs = await SharedPreferences.getInstance();
  final bus = _RecordingBus();
  await tester.pumpWidget(
    ProviderScope(
      overrides: [
        i18nProvider.overrideWith(() => I18nController(_bundle(), prefs)),
        appEventBusProvider.overrideWithValue(bus),
      ],
      child: MaterialApp(
        theme: ThemeData(
          extensions: [
            BossipTokens.resolve(BossipThemeName.default_, Brightness.light),
          ],
        ),
        home: Scaffold(
          body: DesktopTakeoverDetail(
            item: QuestionItem.fromJson({'question': 'q', 'detail': detail}),
            sessionId: 's1',
          ),
        ),
      ),
    ),
  );
  // pump, not pumpAndSettle: the i18n controller keeps a listener alive.
  await tester.pump();
  await tester.pump();
  return bus;
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  test('reads the tool detail and ignores every other question kind', () {
    final d = readTakeoverDetail(_local)!;
    expect(d.reason, 'captcha_slider');
    expect(d.host, 'login.taobao.com');
    expect(d.onDesktop, isTrue);
    expect(readTakeoverDetail({'kind': 'desktop_takeover'})!.reason, 'other');
    expect(readTakeoverDetail({'kind': 'video_script_approval'}), isNull);
    expect(readTakeoverDetail(null), isNull);
    expect(takeoverReasonKey('nope'), 'chat:takeover.reason.other');
  });

  test('the workbench path carries the control flag only when asked', () {
    expect(Paths.workbench('s1', tab: 'desktop', control: true),
        '/app/w/s1?tab=desktop&control=1');
    expect(Paths.workbench('s1', tab: 'desktop'), '/app/w/s1?tab=desktop');
  });

  testWidgets('a page on the cloud desktop gets a button that asks for control',
      timeout: const Timeout(Duration(seconds: 60)), (tester) async {
    final bus = await _pump(tester, _local);

    expect(find.text('slider captcha'), findsOneWidget);
    expect(find.text('login.taobao.com'), findsOneWidget);
    await tester.tap(find.text('Open the cloud desktop'));
    await tester.pump();

    expect(bus.seen, hasLength(1));
    expect(bus.seen.single.type, 'workbench.open');
    expect(bus.seen.single.payload,
        {'kind': 'desktop', 'sessionId': 's1', 'control': true});
  });

  testWidgets('a page in the user\'s own browser only says where to look',
      timeout: const Timeout(Duration(seconds: 60)), (tester) async {
    await _pump(tester, {..._local, 'browser': 'extension'});
    expect(find.text('Open the cloud desktop'), findsNothing);
    expect(find.text('Finish it in your own browser.'), findsOneWidget);
  });
}
