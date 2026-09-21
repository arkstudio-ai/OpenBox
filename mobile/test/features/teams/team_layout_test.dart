import 'dart:io';
import 'dart:ui' as ui;

import 'package:bossip_mobile/features/chat/state/config_providers.dart';
import 'package:bossip_mobile/features/chat/widgets/cards/team_lineup_detail.dart';
import 'package:bossip_mobile/features/chat/widgets/composer/composer.dart';
import 'package:bossip_mobile/features/teams/agent_teams_screen.dart';
import 'package:bossip_mobile/features/teams/api/teams_api.dart';
import 'package:bossip_mobile/features/teams/team_run_screen.dart';
import 'package:bossip_mobile/features/teams/widgets/team_picker.dart';
import 'package:bossip_mobile/features/teams/widgets/team_progress_card.dart';
import 'package:bossip_mobile/shared/api/auth_session.dart';
import 'package:bossip_mobile/shared/api/workspace_scope.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/models/app_config.dart';
import 'package:bossip_mobile/shared/models/interaction.dart';
import 'package:bossip_mobile/shared/models/team.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

import '../chat/suggestion_fixtures.dart';

/// Layout regression for the team surfaces: every screen has to survive a
/// phone's width in both colour modes, in both languages, at a larger type
/// size. Optional mock-only PNGs for design review are written when a font is
/// supplied (same opt-in as the admin layout test); ordinary runs need none.
const _previewFont = String.fromEnvironment('TEAM_UI_PREVIEW_FONT');
// A fresh key per mount: one key shared by two trees makes Flutter throw
// on every frame if either outlives the other.
GlobalKey _canvas = GlobalKey();

Future<void> _preview(WidgetTester tester, String name) async {
  if (_previewFont.isEmpty) return;
  final boundary =
      _canvas.currentContext!.findRenderObject()! as RenderRepaintBoundary;
  await tester.runAsync(() async {
    final image = await boundary.toImage(pixelRatio: 2);
    final bytes = await image.toByteData(format: ui.ImageByteFormat.png);
    final file = File('build/team-ui/$name.png');
    await file.parent.create(recursive: true);
    await file.writeAsBytes(bytes!.buffer.asUint8List());
    image.dispose();
  });
}

const _scope = (userId: 'owner', workspaceId: 'workspace');

Map<String, dynamic> _member(
  String id,
  String name, {
  String role = 'member',
  String execution = 'running',
  String source = 'coordinator',
  String icon = 'bot',
  String color = 'blue',
  String responsibility = '',
}) => {
  'id': id,
  'alias': name,
  'name': name,
  'role': role,
  'source': source,
  'model': 'openai/qwen3.8-flash',
  'membership_state': 'active',
  'execution_state': execution,
  'responsibility': responsibility,
  'display': {'icon': icon, 'color': color},
  'tool_ids': ['read_file', 'write_file', 'web_search'],
  'skill_refs': [
    {'name': '资料核对'},
  ],
};

/// A run partway through: one accepted task, one running, one still blocked,
/// a member queued behind a busy peer, and a notice worth surfacing.
class _Api extends TeamsApi {
  _Api() : super(Dio(), AuthSession(), WorkspaceScope());

  String state = 'running';

  @override
  Future<TeamSnapshot> snapshot(
    TeamScope scope,
    String runId, {
    CancelToken? cancel,
  }) async => TeamSnapshot.fromJson({
    'id': runId,
    'seq': 12,
    'task_count': 3,
    'completed_task_count': 1,
    'run': {
      'id': runId,
      'root_session_id': 'root',
      'title': 'Gemini 与 Qwen 独立验算 17+29 并交叉复核结论',
      'state': state,
      'revision': 4,
      'pause_reason': state == 'paused' ? 'stalled' : null,
      'final_summary': state == 'completed'
          ? '17+29=46，Gemini 与 Qwen 独立验算一致。两项交付任务均已验收成功。'
          : '',
    },
    'members': [
      _member(
        'coordinator',
        '协调者',
        role: 'coordinator',
        source: 'builtin',
        icon: 'clipboardcheck',
        color: 'gray',
        responsibility: '拆分任务、分派成员并验收结果',
      ),
      _member(
        'member-1',
        'Gemini 验算员',
        icon: 'calculator',
        responsibility: '独立完成算式并给出结构化结果，不参考同伴答案',
      ),
      {
        ..._member(
          'member-2',
          'Qwen 复核员',
          execution: 'queued',
          source: 'library',
          icon: 'shieldcheck',
          color: 'green',
          responsibility: '独立复核并比对两份结果是否一致',
        ),
        'current_attempt': 'attempt-2',
      },
      _member(
        'member-3',
        '备用成员',
        execution: 'idle',
        source: 'library',
        icon: 'shieldcheck',
        color: 'green',
        responsibility: '独立复核并比对两份结果是否一致',
      ),
    ],
    'tasks': [
      {
        'id': 'task-1',
        'title': 'Gemini 独立验算 17+29',
        'state': 'succeeded',
        'owner_member_id': 'member-1',
      },
      {
        'id': 'task-2',
        'title': 'Qwen 独立复核并比对结论是否一致',
        'state': 'running',
        'owner_member_id': 'member-2',
        'current_attempt': 'attempt-2',
      },
      {
        'id': 'task-3',
        'title': '汇总两份结果并给出最终答复',
        'state': 'blocked',
        'owner_member_id': 'coordinator',
      },
    ],
    'links': [
      {'from': 'root', 'to': 'member-2', 'kind': 'task', 'count': 1},
      {'from': 'member-1', 'to': 'member-2', 'kind': 'message', 'count': 3},
    ],
    'notices': [
      {'id': 'n1', 'code': 'TEAM_DEPENDENCY_BLOCKED'},
    ],
  });

  @override
  Future<Map<String, dynamic>> read(
    TeamScope scope,
    String path, {
    Map<String, dynamic>? query,
    CancelToken? cancel,
  }) async {
    if (path.endsWith('/events')) return {'last_seq': 12};
    if (path.endsWith('-definitions')) {
      final team = path.contains('team-');
      return {
        'items': [
          {
            'id': team ? 'team-1' : 'agent-1',
            'name': team ? '本地分析审校团队' : 'Gemini 验算员',
            'status': 'active',
            'source': team ? 'team' : 'ai',
            'run_count': 4,
            'provenance': {'session_id': team ? null : 'session-1'},
            'member_previews': team
                ? [
                    {
                      'alias': 'a',
                      'display': {'icon': 'calculator', 'color': 'blue'},
                    },
                    {
                      'alias': 'b',
                      'display': {'icon': 'shieldcheck', 'color': 'green'},
                    },
                  ]
                : const <dynamic>[],
            'version': {
              'capability_summary': {
                'model': 'openai/qwen3.8-flash',
                'tool_tiers': {'image_gen': 'T2'},
              },
              'spec': team
                  ? {
                      'description': '独立验算并交叉复核，最终由协调者汇总结论。',
                      'preset_members': [
                        {'alias': 'Gemini 验算员'},
                        {'alias': 'Qwen 复核员'},
                      ],
                      'policy': {'member_selection': 'coordinator_select'},
                    }
                  : {
                      'when_to_use': '需要独立复核一段计算或结论时交给它。',
                      'display': {'icon': 'calculator', 'color': 'blue'},
                      'skill_refs': [
                        {'name': '资料核对'},
                      ],
                      'tool_allowlist': ['read_file', 'web_search'],
                    },
            },
          },
        ],
        'next_cursor': null,
      };
    }
    if (path.endsWith('/team-runs')) {
      return {
        'items': [
          {
            'id': 'run',
            'title': 'Gemini 与 Qwen 独立验算 17+29 并交叉复核结论',
            'root_session_id': 'root',
            'project_id': 'project-1',
            'template_id': 'team-1',
            'state': 'paused',
            'created_at': '2026-09-21T03:00:00+00:00',
            'summary': {
              'needs_attention': true,
              'pause_reason': 'stalled',
              'task_count': 3,
            },
            'usage': {
              'credits': '12.4062',
              'tokens': 48213,
              'pending': 1,
              'unpriced': 0,
            },
          },
        ],
        'next_cursor': null,
      };
    }
    if (path.endsWith('/usage')) {
      return {
        'credits': '12.4062',
        'tokens': 48213,
        'pending': 1,
        'unpriced': 0,
        'categories': [
          {'category': 'coordinator', 'credits': '4.1', 'tokens': 15200},
          {'category': 'member_work', 'credits': '7.9', 'tokens': 31013},
          {'category': 'rework', 'credits': '0.4', 'tokens': 2000},
          {'category': 'unattributed', 'credits': '0', 'tokens': 0, 'calls': 0},
        ],
        'items': [
          {
            'member_id': 'member-1',
            'model': 'openai/qwen3.8-flash',
            'kind': 'chat',
            'category': 'member_work',
            'credits': '4.2',
            'tokens': 16000,
          },
        ],
      };
    }
    if (path.endsWith('/tasks')) {
      return {
        'items': [
          {
            'id': 'task-2',
            'title': 'Qwen 独立复核并比对结论是否一致',
            'state': 'running',
            'owner_member_id': 'member-2',
            'description': '独立计算 17+29，不要参考同伴的答案，并说明是否与对方结论一致。',
            'expected_output': '一个包含 result 字段的对象',
            'dependencies': ['task-1'],
          },
        ],
        'next_offset': null,
      };
    }
    if (path.endsWith('/attempts')) {
      return {
        'items': [
          {
            'id': 'attempt-1',
            'number': 1,
            'state': 'succeeded',
            'summary': '独立验算得到 46，与同伴结论一致。',
            'output': {'result': '17+29=46'},
          },
        ],
        'next_offset': null,
      };
    }
    if (path.endsWith('/messages')) {
      return {
        'items': [
          {
            'id': 'msg-1',
            'from_member_id': 'member-1',
            'to_member_id': 'member-2',
            'kind': 'result',
            'state': 'delivered',
            'body': '我这边算出 46，请你独立复核后告诉我结论是否一致。',
          },
        ],
        'next_offset': null,
      };
    }
    return {'items': const <dynamic>[], 'next_offset': null};
  }
}

Future<SuggestionFixture> _fixture(String language, _Api api) =>
    SuggestionFixture.create(
      language: language,
      overrides: [
        teamsApiProvider.overrideWithValue(api),
        chatAgentsProvider.overrideWith(
          (ref) async => const [
            AgentInfo(name: 'build'),
            AgentInfo(name: 'plan'),
            AgentInfo(name: 'team'),
          ],
        ),
      ],
    );

Future<void> _mount(
  WidgetTester tester,
  SuggestionFixture fixture,
  Widget child, {
  required Brightness brightness,
  double textScale = 1,
  Size size = const Size(402, 874),
}) async {
  tester.view.devicePixelRatio = 1;
  tester.view.physicalSize = size;
  addTearDown(tester.view.reset);
  _canvas = GlobalKey();
  await tester.pumpWidget(
    fixture.app(
      RepaintBoundary(key: _canvas, child: child),
      brightness: brightness,
      scale: textScale,
      fontFamily: _previewFont.isEmpty ? null : 'TeamPreview',
    ),
  );
  await tester.pumpAndSettle();
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUpAll(() async {
    if (_previewFont.isEmpty) return;
    await (FontLoader('TeamPreview')..addFont(
          Future.value(
            ByteData.sublistView(await File(_previewFont).readAsBytes()),
          ),
        ))
        .load();
    await (FontLoader(
      'MaterialIcons',
    )..addFont(rootBundle.load('fonts/MaterialIcons-Regular.otf'))).load();
  });

  for (final language in ['zh-CN', 'en-US']) {
    for (final brightness in [Brightness.light, Brightness.dark]) {
      final suffix = '$language-${brightness.name}';
      late _Api api;
      late SuggestionFixture fixture;
      setUp(() async {
        api = _Api();
        fixture = await _fixture(language, api);
      });
      tearDown(() => fixture.dispose());

      testWidgets('progress card fits a phone in $suffix', (tester) async {
        await _mount(
          tester,
          fixture,
          Padding(
            padding: const EdgeInsets.all(12),
            child: TeamProgressCard(
              scope: _scope,
              runId: 'run',
              onDetails: () {},
            ),
          ),
          brightness: brightness,
          textScale: 1.2,
        );
        expect(tester.takeException(), isNull);
        // The heading counter, every task with its owner, and the controls.
        expect(find.textContaining('17+29'), findsWidgets);
        expect(find.text('Gemini 验算员'), findsOneWidget);
        await _preview(tester, 'progress-$suffix');
        await tester.pumpWidget(const SizedBox.shrink());
      });

      testWidgets('run screen tabs fit a phone in $suffix', (tester) async {
        await _mount(
          tester,
          fixture,
          TeamRunScreen(
            scope: _scope,
            runId: 'run',
            onOpenChat: (_) {},
            onOpenLibrary: (_) {},
            renderText: Text.new,
            renderArtifact: (_) => const SizedBox.shrink(),
          ),
          brightness: brightness,
        );
        expect(tester.takeException(), isNull);
        // The graph's edges and its detail card, as text: who delegated to
        // whom, who is talking, and what this member is on right now.
        expect(find.textContaining('↔'), findsWidgets);
        expect(
          find.textContaining(language == 'zh-CN' ? '当前任务' : 'Current task'),
          findsOneWidget,
        );
        await _preview(tester, 'run-members-$suffix');
        for (final tab in ['tasks', 'messages', 'usage']) {
          final label = {
            'tasks': {'zh-CN': '任务', 'en-US': 'Tasks'},
            'messages': {'zh-CN': '消息', 'en-US': 'Messages'},
            'usage': {'zh-CN': '用量', 'en-US': 'Usage'},
          }[tab]![language]!;
          await tester.tap(find.text(label));
          await tester.pumpAndSettle();
          if (tab == 'tasks') {
            await tester.tap(find.text('Qwen 独立复核并比对结论是否一致'));
            await tester.pumpAndSettle();
          }
          expect(tester.takeException(), isNull, reason: 'tab $tab');
          await _preview(tester, 'run-$tab-$suffix');
        }
        await tester.pumpWidget(const SizedBox.shrink());
      });

      testWidgets('the Agent team library fits a phone in $suffix', (
        tester,
      ) async {
        await _mount(
          tester,
          fixture,
          AgentTeamsScreen(
            scope: _scope,
            enabled: true,
            projects: const [(id: 'project-1', name: '本地验收')],
            onRunTemplate: (_) {},
            onRunAgain: (_) {},
            onOpenChat: (_) {},
          ),
          brightness: brightness,
        );
        expect(tester.takeException(), isNull);
        expect(find.text('Gemini 验算员'), findsOneWidget);
        await _preview(tester, 'library-agents-$suffix');
        for (final (tab, label) in [
          ('templates', {'zh-CN': '团队模板', 'en-US': 'Team templates'}),
          ('runs', {'zh-CN': '运行记录', 'en-US': 'Run history'}),
        ]) {
          await tester.ensureVisible(find.text(label[language]!));
          await tester.pumpAndSettle();
          await tester.tap(find.text(label[language]!));
          await tester.pumpAndSettle();
          expect(tester.takeException(), isNull, reason: 'tab $tab');
          // Each tab has to carry the thing the phone can act on: a saved
          // roster to start, and a past run to reopen or run again.
          expect(
            find.text(
              tab == 'templates'
                  ? '本地分析审校团队'
                  : 'Gemini 与 Qwen 独立验算 17+29 并交叉复核结论',
            ),
            findsWidgets,
            reason: 'tab $tab',
          );
          await _preview(tester, 'library-$tab-$suffix');
        }
        await tester.pumpWidget(const SizedBox.shrink());
      });

      testWidgets('a finished run offers both saves in $suffix', (
        tester,
      ) async {
        api.state = 'completed';
        await _mount(
          tester,
          fixture,
          TeamRunScreen(
            scope: _scope,
            runId: 'run',
            onOpenChat: (_) {},
            onOpenLibrary: (_) {},
            renderText: Text.new,
            renderArtifact: (_) => const SizedBox.shrink(),
          ),
          brightness: brightness,
        );
        expect(tester.takeException(), isNull);
        final template = find.text(
          language == 'zh-CN' ? '另存为团队模板' : 'Save team as template',
        );
        // A finished run keeps: the roster as a template, a member as an Agent.
        expect(template, findsOneWidget);
        expect(
          find.text(
            language == 'zh-CN' ? '保存到 Agent 库' : 'Save Agent to library',
          ),
          findsWidgets,
        );
        await tester.tap(template);
        await tester.pumpAndSettle();
        expect(tester.takeException(), isNull);
        // The sheet explains what saving does and lists the members the
        // template would keep, each droppable.
        expect(
          find.text(
            fixture.container.read(i18nProvider).t('teams:saveTemplateHint'),
          ),
          findsOneWidget,
        );
        expect(find.byType(TextField), findsWidgets);
        await tester.pumpWidget(const SizedBox.shrink());
      });

      testWidgets('lineup card fits a phone in $suffix', (tester) async {
        await _mount(
          tester,
          fixture,
          Padding(
            padding: const EdgeInsets.all(12),
            child: TeamLineupDetail(
              item: QuestionItem(
                question: '确认这支团队开始工作吗？',
                detail: const {
                  'kind': 'team_lineup',
                  'spec': {
                    'name': '独立验算团队',
                    'policy': {
                      'max_concurrent_members': 2,
                      'max_wall_time_seconds': 600,
                      'member_selection': 'coordinator_may_add',
                    },
                  },
                  'coordinator': {'model': 'openai/qwen3.8-flash'},
                  'members': [
                    {
                      'name': 'Gemini 验算员',
                      'source': 'coordinator',
                      'model': 'google/gemini-3-flash',
                      'responsibility': '独立完成算式并给出结构化结果',
                      'skills': [
                        {'name': '资料核对'},
                      ],
                      'tool_ids': ['read_file', 'web_search'],
                    },
                    {
                      'name': 'Qwen 复核员',
                      'source': 'library',
                      'model': 'openai/qwen3.8-flash',
                      'responsibility': '独立复核并比对两份结果是否一致',
                      'tool_ids': ['read_file'],
                    },
                  ],
                  'grant': {
                    'paid_tools': {'image_gen': true},
                  },
                },
              ),
            ),
          ),
          brightness: brightness,
          textScale: 1.2,
        );
        expect(tester.takeException(), isNull);
        expect(find.text('Gemini 验算员'), findsOneWidget);
        await _preview(tester, 'lineup-$suffix');
        await tester.pumpWidget(const SizedBox.shrink());
      });

      testWidgets('composer keeps the team pill on one row in $suffix', (
        tester,
      ) async {
        fixture.container.read(pickedAgentProvider('draft').notifier).state =
            'team';
        await _mount(
          tester,
          fixture,
          Align(
            alignment: Alignment.bottomCenter,
            child: Composer(
              sessionKey: 'draft',
              busy: false,
              onSend: (_, _) async {},
              controls: TeamPicker(
                scope: _scope,
                value: const TeamRequest(),
                onChanged: (_) {},
              ),
            ),
          ),
          brightness: brightness,
        );
        expect(tester.takeException(), isNull);
        // Mode and team pill sit on the same toolbar row as the model pill.
        expect(find.text(language == 'zh-CN' ? '团队' : 'Team'), findsOneWidget);
        expect(
          find.text(language == 'zh-CN' ? '自动组队' : 'Automatic team'),
          findsOneWidget,
        );
        await _preview(tester, 'composer-$suffix');
        await tester.pumpWidget(const SizedBox.shrink());
      });
    }
  }
}
