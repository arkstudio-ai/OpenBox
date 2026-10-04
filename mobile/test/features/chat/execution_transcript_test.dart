import 'dart:async';

import 'package:bossip_mobile/features/chat/api/assistant_api.dart';
import 'package:bossip_mobile/features/chat/state/execution_transcript.dart';
import 'package:bossip_mobile/features/chat/state/stream_store.dart';
import 'package:bossip_mobile/shared/events/app_lifecycle.dart';
import 'package:bossip_mobile/shared/models/message.dart';
import 'package:bossip_mobile/shared/models/message_part.dart';
import 'package:bossip_mobile/shared/ws/ws_client.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'assistant_fixture.dart';

final actorProvider = StateProvider<AssistantScope?>((ref) => scope);
const target = (sessionId: 'main', actor: scope);

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  late ProviderContainer container;
  late TestApi api;
  late TestWs ws;
  late ChatStreamStore stream;
  late ProviderSubscription<ExecutionTranscriptState> listener;
  ExecutionTranscript getController() =>
      container.read(executionTranscriptProvider(target).notifier);
  ExecutionTranscriptState getState() =>
      container.read(executionTranscriptProvider(target));
  Future<void> ready() async {
    await getController().refresh();
    await Future<void>.delayed(Duration.zero);
  }

  setUp(() {
    api = TestApi();
    ws = TestWs();
    container = ProviderContainer(
      overrides: [
        wsClientProvider.overrideWithValue(ws),
        assistantScopeProvider.overrideWith((ref) => ref.watch(actorProvider)),
        assistantApiProvider(scope).overrideWithValue(api),
      ],
    );
    stream = container.read(chatStreamProvider.notifier);
    stream.mergeHistory('main', [message('m02')]);
    listener = container.listen(executionTranscriptProvider(target), (_, _) {});
  });
  tearDown(() async {
    listener.close();
    container.dispose();
    await ws.close();
  });

  test(
    'every retained page is checked in bounded batches and failure hides all bodies',
    () async {
      final originals = [for (var i = 0; i < 205; i++) message('m$i')];
      api.stored
        ..clear()
        ..addEntries(originals.map((m) => MapEntry(m.id, m)));
      stream.mergeHistory('main', originals);
      await ready();
      expect(api.validated.every((ids) => ids.length <= 100), isTrue);
      expect(getState().messages.length, 205);
      expect(getState().project(originals.first).parts, isNotEmpty);
      api.failValidation = true;
      await getController().refresh();
      expect(getState().project(originals.first).parts, isEmpty);
      expect(getState().project(originals.last).parts, isEmpty);
    },
  );

  testWidgets('idle periodic validation hides an older revoked page', (
    tester,
  ) async {
    // Create the timer inside the widget test's clock, not setUp's real clock.
    listener.close();
    container.invalidate(executionTranscriptProvider(target));
    listener = container.listen(executionTranscriptProvider(target), (_, _) {});
    stream.mergeHistory('main', [message('m01'), message('m02')]);
    api.stored['m01'] = message('m01');
    await tester.pump();
    expect(getState().project(message('m01')).parts, isNotEmpty);
    api.stored['m01'] = message('m01', status: 'unavailable', checked: 2);
    await tester.pump(const Duration(seconds: 15));
    await tester.pump();
    expect(getState().project(message('m01')).parts, isEmpty);
    expect(getState().project(message('m02')).parts, isNotEmpty);
    listener.close();
    container.invalidate(executionTranscriptProvider(target));
    await tester.pump(const Duration(milliseconds: 1));
  });

  test(
    'foreground return hides cached text until a fresh read succeeds',
    () async {
      await ready();
      container.read(appVisibleProvider.notifier).state = false;
      expect(getState().project(message('m02')).parts, isEmpty);
      api.validationGate = Completer<void>();
      container.read(appVisibleProvider.notifier).state = true;
      expect(getState().project(message('m02')).parts, isEmpty);
      api.stored['m02'] = message('m02', status: 'unavailable', checked: 2);
      api.validationGate!.complete();
      await ready();
      expect(getState().project(message('m02')).sourceStatus, 'unavailable');
    },
  );

  test(
    'copy cannot use a response older than the latest source refusal',
    () async {
      await ready();
      final late = Completer<List<ChatMessage>>();
      api.onRead = (_) => late.future;
      final copying = getController().canCopy(['m02'], 'verified answer');
      api.onRead = (_) async => [
        message('m02', status: 'unavailable', checked: 2),
      ];
      await getController().refresh();
      late.complete([message('m02')]);
      expect(await copying, isFalse);
      expect(getState().project(message('m02')).parts, isEmpty);
    },
  );

  test(
    'copy and delayed reads are fenced by current actor and workspace',
    () async {
      await ready();
      final late = Completer<List<ChatMessage>>();
      api.onRead = (_) => late.future;
      final copying = getController().canCopy(['m02'], 'verified answer');
      container.read(actorProvider.notifier).state = (
        userId: 'other',
        workspaceId: 'other',
      );
      late.complete([message('m02')]);
      expect(await copying, isFalse);
    },
  );

  test(
    'missing server rows cannot leave a retained original available',
    () async {
      await ready();
      api.onRead = (_) async => [];
      expect(
        await getController().canCopy(['m02'], 'verified answer'),
        isFalse,
      );
      expect(getState().project(message('m02')).parts, isEmpty);
    },
  );

  test(
    'whole-message redaction defeats late history, creation and text delta frames',
    () {
      final secret = message('m02');
      stream.mergeHistory('main', [
        message('m02', status: 'unavailable', checked: 2),
      ]);
      stream.mergeHistory('main', [secret]);
      stream.addMessage('main', secret);
      stream.updateMessage('main', {
        'id': 'm02',
        'error': {'message': 'OLD_ERROR'},
      });
      stream.appendPartDelta('main', 'm02', 'm02-text', 'OLD_SECRET');
      final held = container.read(chatStreamProvider).messagesOf('main').single;
      expect(held.parts, isEmpty);
      expect(held.error, isNull);
      expect(held.sourceStatus, 'unavailable');
      const ordinary = ChatMessage(
        id: 'ordinary',
        sessionId: 'ordinary',
        role: 'assistant',
        parts: [TextPart(id: 'p', text: 'normal')],
      );
      stream.mergeHistory('ordinary', [ordinary]);
      stream.appendPartDelta('ordinary', 'ordinary', 'p', ' stream');
      expect(
        (container
                    .read(chatStreamProvider)
                    .messagesOf('ordinary')
                    .single
                    .parts
                    .single
                as TextPart)
            .text,
        'normal stream',
      );
    },
  );
}
