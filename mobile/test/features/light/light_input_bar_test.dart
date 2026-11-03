import 'package:bossip_mobile/features/chat/chat_screen.dart';
import 'package:bossip_mobile/features/light/api/light_api.dart';
import 'package:bossip_mobile/features/light/widgets/light_input_bar.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

Future<List<(String, List<String>)>> _mount(
  WidgetTester tester, {
  required int userTurns,
  bool busy = false,
  bool pending = false,
  List<String> chips = const ['我拍好了，看这几张', '没照片，用文字描述'],
}) async {
  SharedPreferences.setMockInitialValues({'bossip:lang': 'zh-CN'});
  final prefs = await SharedPreferences.getInstance();
  final bundle = (await tester.runAsync(I18nBundle.load))!;
  final sent = <(String, List<String>)>[];
  await tester.pumpWidget(
    ProviderScope(
      overrides: [
        prefsProvider.overrideWithValue(prefs),
        i18nProvider.overrideWith(() => I18nController(bundle, prefs)),
        lightApiProvider.overrideWithValue(LightApi(Dio())),
      ],
      child: MaterialApp(
        theme: ThemeData(
          extensions: [
            BossipTokens.resolve(BossipThemeName.default_, Brightness.light),
          ],
        ),
        home: Scaffold(
          body: Column(
            children: [
              const Spacer(),
              LightInputBar(
                chips: chips,
                state: ChatInputState(
                  busy: busy,
                  userTurns: userTurns,
                  hasPendingInput: pending,
                  onSend: (text, attachments) async =>
                      sent.add((text, attachments)),
                  onStop: busy ? () {} : null,
                ),
              ),
            ],
          ),
        ),
      ),
    ),
  );
  await tester.pumpAndSettle();
  return sent;
}

void main() {
  testWidgets('the opening chips are the first reply', (tester) async {
    final sent = await _mount(tester, userTurns: 1);
    expect(find.byKey(const Key('light-chip-0')), findsOneWidget);
    expect(find.text('没照片，用文字描述'), findsOneWidget);
    await tester.tap(find.byKey(const Key('light-chip-0')));
    await tester.pumpAndSettle();
    expect(sent.single.$1, '我拍好了，看这几张');
    expect(sent.single.$2, isEmpty);
  });

  testWidgets('chips step aside once the conversation is under way', (
    tester,
  ) async {
    await _mount(tester, userTurns: 2);
    expect(find.byKey(const Key('light-chip-0')), findsNothing);
    await _mount(tester, userTurns: 1, pending: true);
    expect(find.byKey(const Key('light-chip-0')), findsNothing);
    await _mount(tester, userTurns: 1, busy: true);
    expect(find.byKey(const Key('light-chip-0')), findsNothing);
    expect(find.byKey(const Key('light-stop')), findsOneWidget);
  });

  testWidgets('typing sends the text; an empty box sends nothing', (
    tester,
  ) async {
    final sent = await _mount(tester, userTurns: 1);
    final send = find.byKey(const Key('light-send'));
    expect(tester.widget<IconButton>(send).onPressed, isNull);
    await tester.enterText(find.byKey(const Key('light-input')), '门头拍一张');
    await tester.pump();
    expect(find.byKey(const Key('light-chip-0')), findsNothing);
    expect(tester.widget<IconButton>(send).onPressed, isNotNull);
    await tester.tap(send);
    await tester.pumpAndSettle();
    expect(sent.single.$1, '门头拍一张');
    expect(sent.single.$2, isEmpty);
    expect(
      tester
          .widget<TextField>(find.byKey(const Key('light-input')))
          .controller!
          .text,
      isEmpty,
    );
  });
}
