import 'package:bossip_mobile/features/chat/state/task_card_fold.dart';
import 'package:bossip_mobile/features/chat/utils/task_status.dart';
import 'package:bossip_mobile/features/chat/widgets/assistant_task_card.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'assistant_fixture.dart';

/// Finished task cards fold to one line once they are no longer fresh; a tap
/// opens them, a chevron folds them again, and the choice survives a rebuild
/// (web `AssistantTaskCard.test.tsx` "folding finished cards").
void main() {
  test(
    'the default folds finished work that is not fresh, never active work',
    () {
      final now = DateTime(2026, 11, 9, 12);
      final old = now.subtract(const Duration(hours: 1));
      expect(defaultFolded(TaskStatus.done, old, now: now), isTrue);
      expect(defaultFolded(TaskStatus.failed, old, now: now), isTrue);
      expect(defaultFolded(TaskStatus.stopped, null, now: now), isTrue);
      // Just finished: shown in full once.
      expect(
        defaultFolded(
          TaskStatus.done,
          now.subtract(const Duration(minutes: 3)),
          now: now,
        ),
        isFalse,
      );
      expect(defaultFolded(TaskStatus.running, old, now: now), isFalse);
      expect(defaultFolded(TaskStatus.waiting, old, now: now), isFalse);
      expect(defaultFolded(TaskStatus.paused, old, now: now), isFalse);
      // A hand choice wins either way.
      const state = TaskCardFoldState(overrides: {'a': false, 'b': true});
      expect(state.isFolded('a', TaskStatus.done, old, now: now), isFalse);
      expect(state.isFolded('b', TaskStatus.running, old, now: now), isTrue);
    },
  );

  testWidgets(
    'a finished card is one line, opens on tap and folds again by hand',
    (tester) async {
      final f = (await tester.runAsync(() async {
        SharedPreferences.setMockInitialValues({});
        final prefs = await SharedPreferences.getInstance();
        final bundle = await I18nBundle.load();
        final fixture = Fixture(
          prefs,
          i18n: I18nState(language: 'en-US', bundle: bundle),
          foldCards: true,
        );
        // Finished two hours ago (the fixture's updated_at): folds by default.
        fixture.api.taskObserved = 'completed';
        await fixture.ready();
        return fixture;
      }))!;
      addTearDown(f.close);
      await tester.pumpWidget(
        UncontrolledProviderScope(
          container: f.container,
          child: MaterialApp(
            theme: ThemeData(
              extensions: [
                BossipTokens.resolve(
                  BossipThemeName.default_,
                  Brightness.light,
                ),
              ],
            ),
            home: Scaffold(
              body: SingleChildScrollView(
                child: AssistantTaskById(scope: scope, taskId: 'task'),
              ),
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();
      // Folded: the title is there, the menu and the primary button are not.
      expect(find.text('Original task'), findsOneWidget);
      expect(find.byKey(const Key('task-primary')), findsNothing);
      expect(find.byKey(const Key('task-expand')), findsOneWidget);

      await tester.tap(find.byKey(const Key('task-expand')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('task-primary')), findsOneWidget);
      expect(
        f.prefs.getString('openbox.taskCardFold'),
        contains('"task":"open"'),
      );

      await tester.tap(find.byKey(const Key('task-fold')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('task-primary')), findsNothing);
      expect(
        f.prefs.getString('openbox.taskCardFold'),
        contains('"task":"folded"'),
      );

      await tester.pumpWidget(const SizedBox.shrink());
      f.close();
      await tester.pump(const Duration(milliseconds: 1));
    },
  );
}
