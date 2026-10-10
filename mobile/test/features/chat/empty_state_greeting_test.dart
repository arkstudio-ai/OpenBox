import 'package:bossip_mobile/features/chat/widgets/empty_state.dart';
import 'package:bossip_mobile/shared/api/assistant_profile.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import '../voice/voice_fakes.dart';

class _Profile extends AssistantProfileNotifier {
  _Profile(this.profile);
  final AssistantProfile profile;
  @override
  Future<AssistantProfile> build() async => profile;
}

Future<void> _greet(WidgetTester tester, AssistantProfile profile) async {
  await tester.pumpWidget(
    ProviderScope(
      key: ValueKey(profile),
      overrides: [
        await zhI18n(tester),
        assistantProfileProvider.overrideWith(() => _Profile(profile)),
      ],
      child: MaterialApp(
        theme: testTheme(),
        home: Scaffold(body: ChatEmptyState(onPick: (_) {})),
      ),
    ),
  );
  await tester.pumpAndSettle();
}

void main() {
  testWidgets('a new chat greets the way the person asked to be called, '
      'never with a sign-in name', (tester) async {
    await _greet(tester, const AssistantProfile());
    expect(
      find.byWidgetPredicate(
        (widget) =>
            widget is Text &&
            RegExp(r'^(早上好|下午好|晚上好)。$').hasMatch(widget.data ?? ''),
      ),
      findsOneWidget,
    );
    await _greet(tester, const AssistantProfile(address: '老王'));
    expect(
      find.byWidgetPredicate(
        (widget) =>
            widget is Text &&
            RegExp(r'^(早上好|下午好|晚上好)，老王。$').hasMatch(widget.data ?? ''),
      ),
      findsOneWidget,
    );
  });
}
