import 'package:bossip_mobile/app/workspace_shell.dart';
import 'package:bossip_mobile/features/workspace/state/workspace_store.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/ws/ws_client.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

class _EmptyWorkspace extends WorkspaceController {
  @override
  Future<WorkspaceData> build() async => const WorkspaceData();
}

class _OfflineWsClient extends AgentWsClient {
  _OfflineWsClient() : super(Dio());

  @override
  Future<void> connect() async {}
}

void main() {
  for (final swipe in [false, true]) {
    testWidgets('opening drawer dismisses composer keyboard (swipe=$swipe)', (
      tester,
    ) async {
      SharedPreferences.setMockInitialValues({'bossip:lang': 'zh-CN'});
      final prefs = await SharedPreferences.getInstance();
      final bundle = (await tester.runAsync(I18nBundle.load))!;
      final focus = FocusNode();
      addTearDown(focus.dispose);
      await tester.pumpWidget(
        ProviderScope(
          overrides: [
            i18nProvider.overrideWith(
              () => I18nController(bundle, prefs),
            ),
            workspaceProvider.overrideWith(_EmptyWorkspace.new),
            wsClientProvider.overrideWithValue(_OfflineWsClient()),
          ],
          child: MaterialApp(
            theme: ThemeData(
              extensions: [
                BossipTokens.resolve(
                  BossipThemeName.default_,
                  Brightness.light,
                ),
              ],
            ),
            home: WorkspaceShell(
              child: TextField(key: const Key('composer'), focusNode: focus),
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('composer')));
      await tester.pumpAndSettle();
      expect(focus.hasFocus, isTrue);
      expect(tester.testTextInput.isVisible, isTrue);

      if (swipe) {
        await tester.dragFrom(const Offset(0, 300), const Offset(400, 0));
      } else {
        await tester.tap(find.byType(DrawerButton));
      }
      await tester.pumpAndSettle();
      expect(
        tester.state<ScaffoldState>(find.byType(Scaffold)).isDrawerOpen,
        isTrue,
      );
      expect(focus.hasFocus, isFalse);
      expect(tester.testTextInput.isVisible, isFalse);

      // Search within the open drawer must still be able to show the keyboard.
      await tester.tap(find.byType(TextField).last);
      await tester.pumpAndSettle();
      expect(tester.testTextInput.isVisible, isTrue);
    });
  }
}
