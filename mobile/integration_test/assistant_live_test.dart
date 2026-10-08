import 'dart:convert';
import 'dart:io';

import 'package:bossip_mobile/app/app.dart';
import 'package:bossip_mobile/app/router.dart';
import 'package:bossip_mobile/features/auth/login_page.dart';
import 'package:bossip_mobile/features/chat/api/assistant_api.dart';
import 'package:bossip_mobile/features/chat/assistant_screen.dart';
import 'package:bossip_mobile/features/chat/chat_screen.dart';
import 'package:bossip_mobile/features/chat/state/assistant_controller.dart';
import 'package:bossip_mobile/features/chat/widgets/assistant_request_review.dart';
import 'package:bossip_mobile/features/chat/widgets/assistant_task_card.dart';
import 'package:bossip_mobile/features/chat/widgets/chat_flow.dart';
import 'package:bossip_mobile/features/chat/widgets/composer/composer.dart';
import 'package:bossip_mobile/main.dart' as app;
import 'package:bossip_mobile/shared/api/auth_store.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:bossip_mobile/shared/config/env.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/models/message_part.dart';
import 'package:bossip_mobile/shared/router/paths.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:integration_test/integration_test.dart';
import 'package:path_provider/path_provider.dart';

/// Explicit local-QA opt-in, using the actual app bootstrap, native persistence,
/// UI, authenticated HTTP/WS and configured provider. No provider/API overrides.
/// Credentials belong in an ignored --dart-define-from-file input, never here.
/// Run with --no-uninstall to retain screenshots and exercise process restart.
/// Reuse the fixed marker after failure; inspect unknown submissions before any
/// new send. Fixture tasks and replies are retained for source/SQL verification.
void main() {
  final binding = IntegrationTestWidgetsFlutterBinding.ensureInitialized();
  const username = String.fromEnvironment('PA_QA_USERNAME');
  const password = String.fromEnvironment('PA_QA_PASSWORD');
  const owner = String.fromEnvironment('PA_QA_OWNER');
  const workspace = String.fromEnvironment('PA_QA_WORKSPACE');
  const mainId = String.fromEnvironment('PA_QA_MAIN');
  const project = String.fromEnvironment('PA_QA_PROJECT');
  const marker = String.fromEnvironment('PA_QA_MARKER');
  const resumeOnly = bool.fromEnvironment('PA_QA_RESUME_ONLY');
  const followup = bool.fromEnvironment('PA_QA_ORIGINAL_FOLLOWUP');

  testWidgets(
    'native assistant request, natural reply and durable restore',
    (tester) async {
      expect(Env.apiBase, 'http://127.0.0.1:8081');
      expect(Env.webBase, 'http://127.0.0.1:3001');
      for (final value in [
        username,
        password,
        owner,
        workspace,
        mainId,
        project,
        marker,
      ]) {
        expect(
          value.isNotEmpty,
          isTrue,
          reason: 'Explicit local QA inputs required',
        );
      }
      final evidence = <String, dynamic>{
        'marker': marker,
        'resume_only': resumeOnly,
      };
      final network = <Map<String, dynamic>>[];
      late ProviderContainer container;
      Future<void> save(String name) async {
        final dir = await getApplicationSupportDirectory();
        final root = Directory('${dir.path}/assistant-qa');
        await root.create(recursive: true);
        final bytes = await binding.takeScreenshot(name);
        await File('${root.path}/$name.png').writeAsBytes(bytes);
        await File(
          '${root.path}/evidence.json',
        ).writeAsString(jsonEncode({...evidence, 'network': network}));
        debugPrint('PA_NATIVE_EVIDENCE ${root.path}');
      }

      Future<void> waitFor(
        bool Function() ready,
        String stage, {
        int seconds = 120,
      }) async {
        final limit = DateTime.now().add(Duration(seconds: seconds));
        while (!ready() && DateTime.now().isBefore(limit)) {
          await tester.pump(const Duration(milliseconds: 250));
        }
        if (!ready()) {
          await save('failure-$stage');
          fail('Native stage timed out: $stage');
        }
        debugPrint('PA_NATIVE_STAGE $stage');
      }

      await app.main();
      await tester.pump();
      container = ProviderScope.containerOf(
        tester.element(find.byType(BossipApp)),
        listen: false,
      );
      container
          .read(apiDioProvider)
          .interceptors
          .add(
            InterceptorsWrapper(
              onResponse: (response, handler) {
                final path = response.requestOptions.path;
                // Record only route/status and explicitly selected receipt metadata.
                // Authentication headers, tokens and login bodies never enter evidence.
                network.add({
                  'path': path,
                  'method': response.requestOptions.method,
                  'status': response.statusCode,
                });
                if (path == '/api/assistant/requests/displayed') {
                  evidence['display_receipt'] = response.data;
                }
                handler.next(response);
              },
              onError: (error, handler) {
                network.add({
                  'path': error.requestOptions.path,
                  'method': error.requestOptions.method,
                  'status': error.response?.statusCode,
                });
                handler.next(error);
              },
            ),
          );
      final restored = container.read(authProvider).isAuthenticated;
      if (resumeOnly) {
        expect(
          restored,
          isTrue,
          reason: 'Cold bootstrap must restore the real cookie session',
        );
      }
      container.read(routerProvider).go(Paths.assistant);
      await tester.pump();
      if (!restored) {
        await waitFor(
          () =>
              find.byType(LoginPage).evaluate().isNotEmpty &&
              find.byType(TextField).evaluate().length == 2,
          'login-form',
        );
        final fields = find.descendant(
          of: find.byType(LoginPage),
          matching: find.byType(TextField),
        );
        await tester.enterText(fields.at(0), username);
        await tester.enterText(fields.at(1), password);
        final label = container.read(i18nProvider).t('auth:signInBtn');
        await tester.tap(find.widgetWithText(FilledButton, label));
        await waitFor(
          () => container.read(authProvider).isAuthenticated,
          'authenticated',
        );
      }
      expect(container.read(authProvider).userId, owner);
      await waitFor(
        () =>
            find.byType(AssistantScreen).evaluate().isNotEmpty &&
            container.read(assistantScopeProvider) != null,
        'assistant-entry',
      );
      final scope = container.read(assistantScopeProvider)!;
      expect(scope, (userId: owner, workspaceId: workspace));
      AssistantState state() =>
          container.read(assistantControllerProvider(scope));
      await waitFor(
        () => state().snapshot?.session != null,
        'source-checked-snapshot',
      );
      expect(state().snapshot!.session!.id, mainId);
      evidence['main_id'] = mainId;
      evidence['restored_session'] = restored;
      evidence['initial_task_ids'] = state().tasks.map((t) => t.id).toList();
      evidence['initial_message_ids'] = state().messages
          .map((m) => m.id)
          .toList();
      evidence['initial_last_seen'] = state().snapshot!.lastSeen;
      evidence['cold_entry_routes'] = network
          .map((n) => n['path'])
          .toSet()
          .toList();
      expect(
        network.where((n) => n['path'] == '/api/containers/running'),
        isEmpty,
      );
      String tr(String key) => container.read(i18nProvider).t(key);
      // A new native device may show the existing notification introduction.
      final later = find.text(tr('onboarding:notify.later'));
      if (later.evaluate().isNotEmpty) {
        await tester.tap(later);
        await tester.pump(const Duration(milliseconds: 400));
      }
      FocusManager.instance.primaryFocus?.unfocus();
      await tester.pump(const Duration(milliseconds: 500));
      await save(resumeOnly ? 'restored-entry' : 'native-entry');
      Future<void> send(String text) async {
        final field = find.descendant(
          of: find.byType(Composer),
          matching: find.byType(TextField),
        );
        await tester.enterText(field, text);
        await tester.pump();
        await tester.tap(find.byIcon(Icons.arrow_upward));
        await tester.pump(const Duration(milliseconds: 300));
      }

      AssistantTask? task() =>
          state().tasks.where((t) => t.title == marker).firstOrNull;

      if (task() == null) {
        expect(
          resumeOnly,
          isFalse,
          reason: 'Restore cannot create a replacement task',
        );
        expect(
          state().messages.where(
            (m) =>
                m.isUser &&
                m.parts.any((p) => p is TextPart && p.text.contains(marker)),
          ),
          isEmpty,
          reason: 'Inspect the existing human submission before any resend',
        );
        await send(
          '[$marker] 请在项目“浏览器回归-20261003”（ID：$project）中新建一个私有任务，'
          '标题必须是“$marker”。执行要求：先使用 question 工具提问“是否完成这次原生问答验证？”，'
          '只给一个选项“确认”，关闭自定义回答；收到确认后只输出“原生问答验证完成：确认”。'
          '任务只使用 question 和文字，不使用电脑、shell 或外部服务。请创建任务并等它向我提问。',
        );
        await waitFor(() => task() != null, 'task-created', seconds: 180);
      }
      final originalTask = task()!;
      evidence['task_id'] = originalTask.id;
      evidence['execution_id'] = originalTask.executionId;

      if (originalTask.result['delivery_state'] != 'processed') {
        final review = find.byWidgetPredicate(
          (widget) =>
              widget is AssistantRequestReviewButton &&
              widget.kind == 'question' &&
              widget.binding['task_id'] == originalTask.id,
        );
        await waitFor(
          () => review.evaluate().isNotEmpty,
          'question-visible',
          seconds: 180,
        );
        final button = tester.widget<AssistantRequestReviewButton>(review);
        evidence['request_id'] = button.requestId;
        evidence['request_revision'] = button.binding['request_revision'];
        await tester.ensureVisible(review);
        await tester.tap(review);
        await waitFor(
          () => find.byType(AssistantRequestReview).evaluate().isNotEmpty,
          'full-request-opened',
        );
        final complete = find.text(tr('chat:assistant.requests.reviewed'));
        for (var i = 0; i < 80 && complete.evaluate().isEmpty; i++) {
          await tester.pump(const Duration(milliseconds: 300));
          await tester.drag(
            find.byType(AssistantRequestReview),
            const Offset(0, -220),
          );
        }
        await waitFor(() => complete.evaluate().isNotEmpty, 'display-receipt');
        await tester.ensureVisible(complete);
        await save('native-reviewed-request');
        await tester.tap(find.byType(CloseButton));
        await tester.pump(const Duration(milliseconds: 500));
        await send('可以');
        await waitFor(
          () => task()?.result['delivery_state'] == 'processed',
          'question-applied-and-report-processed',
          seconds: 240,
        );
      }
      expect(task()!.id, originalTask.id);
      expect(task()!.executionId, originalTask.executionId);
      expect(task()!.result['outcome'], 'succeeded');
      if (followup) {
        expect(
          resumeOnly,
          isTrue,
          reason: 'Followup requires the retained native session',
        );
        final oldResultId = task()!.result['result_id'];
        // Followed tasks live in the top bar's "我的任务" sheet.
        final tasksButton = find.byKey(
          const ValueKey('assistant-tasks-button'),
        );
        await waitFor(
          () => tasksButton.evaluate().isNotEmpty,
          'task-list-entry-visible',
        );
        await tester.tap(tasksButton);
        await tester.pump(const Duration(milliseconds: 800));
        final card = find
            .byWidgetPredicate(
              (widget) =>
                  widget is AssistantTaskCard &&
                  widget.task.id == originalTask.id,
            )
            .last;
        await waitFor(() => card.evaluate().isNotEmpty, 'original-task-card');
        final open = find.descendant(
          of: card,
          matching: find.text(tr('chat:assistant.card.open')),
        );
        await tester.ensureVisible(open);
        await tester.pump(const Duration(milliseconds: 300));
        await tester.tap(open.hitTestable());
        await waitFor(
          () => find.byType(ChatScreen).evaluate().isNotEmpty,
          'original-execution-opened',
        );
        expect(
          tester.widget<ChatScreen>(find.byType(ChatScreen)).sessionId,
          originalTask.executionId,
        );
        await tester.pump(const Duration(seconds: 1));
        for (final key in [
          'onboarding:marks.skipAll',
          'onboarding:marks.done',
        ]) {
          final skip = find.text(tr(key)).hitTestable();
          if (skip.evaluate().isNotEmpty) {
            await tester.tap(skip.first);
            await tester.pump(const Duration(milliseconds: 400));
          }
        }
        const followupText = '请在这个原任务中继续，直接只回复“原生原会话续接完成-20261004-A1”，不要调用工具。';
        final history = await container
            .read(assistantApiProvider(scope))
            .history(originalTask.executionId);
        expect(
          history.messages.where(
            (m) =>
                m.isUser &&
                m.parts.any(
                  (p) =>
                      p is TextPart && p.text.contains('原生原会话续接完成-20261004-A1'),
                ),
          ),
          isEmpty,
          reason: 'Never resend an unknown or already accepted followup',
        );
        await send(followupText);
        final field = find.descendant(
          of: find.byType(Composer),
          matching: find.byType(TextField),
        );
        await waitFor(
          () => tester.widget<TextField>(field).controller!.text.isEmpty,
          'original-followup-accepted',
        );
        container.read(routerProvider).pop();
        await waitFor(
          () =>
              find.byType(AssistantScreen).evaluate().isNotEmpty &&
              state().snapshot != null,
          'returned-to-main',
        );
        await waitFor(
          () =>
              task()?.result['result_id'] != oldResultId &&
              task()?.result['delivery_state'] == 'processed',
          'original-followup-reported',
          seconds: 240,
        );
        expect(task()!.id, originalTask.id);
        expect(task()!.executionId, originalTask.executionId);
        expect(task()!.result['outcome'], 'succeeded');
        evidence['original_followup'] = {
          'previous_result_id': oldResultId,
          'result_id': task()!.result['result_id'],
          'execution_id': originalTask.executionId,
        };
      }
      final reportId = task()!.result['processed_message_id'] as String;
      await waitFor(
        () => state().messages.any((m) => m.id == reportId),
        'report-loaded',
      );
      FocusManager.instance.primaryFocus?.unfocus();
      await tester.pump(const Duration(milliseconds: 500));
      final flow = tester.widget<ChatFlow>(find.byType(ChatFlow));
      if (flow.controller!.hasClients) {
        flow.controller!.jumpTo(flow.controller!.position.maxScrollExtent);
      }
      await tester.pump(const Duration(seconds: 1));
      final displayedReport = find.byKey(ValueKey(reportId));
      for (var i = 0; i < 20 && displayedReport.evaluate().isEmpty; i++) {
        final position = flow.controller!.position;
        flow.controller!.jumpTo(
          (position.pixels - position.viewportDimension * .6).clamp(
            position.minScrollExtent,
            position.maxScrollExtent,
          ),
        );
        await tester.pump(const Duration(milliseconds: 300));
      }
      await waitFor(
        () => displayedReport.evaluate().isNotEmpty,
        'report-in-render-tree',
      );
      await tester.ensureVisible(displayedReport);
      await tester.pump(const Duration(milliseconds: 500));
      final sequence =
          state().snapshot!.answers.firstWhere(
                (answer) => answer['message_id'] == reportId,
              )['sequence']
              as int;
      await waitFor(
        () => state().snapshot!.lastSeen >= sequence,
        'visible-report-read-receipt',
        seconds: 30,
      );
      evidence['report_sequence'] = sequence;
      evidence['result'] = task()!.result;
      evidence['final_task_ids'] = state().tasks.map((t) => t.id).toList();
      evidence['final_last_seen'] = state().snapshot!.lastSeen;
      await waitFor(
        () => binding.lifecycleState == AppLifecycleState.resumed,
        'native-foreground',
        seconds: 30,
      );
      evidence['native_lifecycle'] = binding.lifecycleState!.name;
      evidence['passed'] = true;
      await save(
        followup
            ? 'native-original-followup'
            : resumeOnly
            ? 'restored-result'
            : 'native-result',
      );
      expect(tester.takeException(), isNull);
    },
    skip: username.isEmpty,
    timeout: const Timeout(Duration(minutes: 15)),
  );
}
