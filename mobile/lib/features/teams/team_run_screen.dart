import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../shared/appearance/tokens.dart';
import '../../shared/i18n/i18n.dart';
import '../../shared/models/json.dart';
import '../../shared/models/team.dart';
import '../../shared/utils/error_text.dart';
import '../../shared/widgets/section_tabs.dart';
import '../../shared/widgets/task_card_frame.dart';
import 'state/team_providers.dart';
import 'widgets/team_collection.dart';
import 'widgets/team_progress_card.dart';

class TeamRunScreen extends ConsumerStatefulWidget {
  const TeamRunScreen({
    super.key,
    required this.scope,
    required this.runId,
    required this.onOpenChat,
    required this.renderText,
    required this.renderArtifact,
  });
  final TeamScope scope;
  final String runId;
  final ValueChanged<String> onOpenChat;
  final Widget Function(String) renderText;
  final Widget Function(Map<String, dynamic>) renderArtifact;
  @override
  ConsumerState<TeamRunScreen> createState() => _TeamRunScreenState();
}

class _TeamRunScreenState extends ConsumerState<TeamRunScreen> {
  String _tab = 'members';
  @override
  Widget build(BuildContext context) {
    final key = (scope: widget.scope, runId: widget.runId);
    final value = ref.watch(teamRunProvider(key));
    final snapshot = value.valueOrNull;
    final i18n = ref.watch(i18nProvider);
    final t = context.tokens;
    Widget collection(
      String kind,
      Widget Function(Map<String, dynamic>) builder, {
      Map<String, String> query = const {},
      String empty = 'teams:noTasks',
    }) => TeamCollection(
      key: ValueKey((widget.runId, kind, query.toString())),
      scope: widget.scope,
      runId: widget.runId,
      collection: kind,
      seq: snapshot?.seq ?? 0,
      query: query,
      itemBuilder: builder,
      emptyLabel: empty,
    );
    String memberName(Object? id) =>
        asString(
          snapshot?.members.where((m) => m['id'] == id).firstOrNull?['alias'],
        ) ??
        id?.toString() ??
        '';
    Widget label(String state) =>
        Text(i18n.t('teams:state.$state'), style: TextStyle(color: t.n600));
    return Scaffold(
      backgroundColor: t.bg,
      appBar: AppBar(
        title: Text(snapshot?.run.title ?? i18n.t('teams:progress')),
      ),
      body: snapshot == null
          ? Center(
              child: value.hasError
                  ? TextButton(
                      onPressed: () => ref.invalidate(teamRunProvider(key)),
                      child: Text(errorText(i18n, value.error!)),
                    )
                  : const CircularProgressIndicator(),
            )
          : Column(
              children: [
                Padding(
                  padding: const EdgeInsets.symmetric(horizontal: 16),
                  child: Row(
                    children: [
                      Expanded(child: label(snapshot.run.state)),
                      TextButton(
                        onPressed: () =>
                            widget.onOpenChat(snapshot.run.rootSessionId),
                        child: Text(i18n.t('teams:openChat')),
                      ),
                    ],
                  ),
                ),
                TeamControls(scope: widget.scope, run: snapshot.run),
                SectionTabs(
                  labels: {
                    for (final tab in [
                      'members',
                      'tasks',
                      'messages',
                      'artifacts',
                      'usage',
                    ])
                      tab: i18n.t('teams:tabs.$tab'),
                  },
                  value: _tab,
                  onChanged: (tab) => setState(() => _tab = tab),
                ),
                Expanded(
                  child: RefreshIndicator(
                    onRefresh: () =>
                        ref.read(teamRunProvider(key).notifier).refresh(),
                    child: ListView(
                      padding: const EdgeInsets.all(16),
                      physics: const AlwaysScrollableScrollPhysics(),
                      children: [
                        if (value.hasError)
                          Text(
                            errorText(i18n, value.error!),
                            style: TextStyle(color: t.danger),
                          ),
                        if (snapshot.run.pauseReason != null)
                          Text(
                            i18n.t('teams:reason.${snapshot.run.pauseReason}'),
                          ),
                        if (snapshot.run.failureReason != null)
                          Text(snapshot.run.failureReason!),
                        if (_tab == 'members') ...[
                          for (final member in snapshot.members)
                            TaskCardFrame(
                              child: Column(
                                crossAxisAlignment: CrossAxisAlignment.stretch,
                                children: [
                                  ListTile(
                                    contentPadding: EdgeInsets.zero,
                                    title: Text(
                                      asString(member['name']) ??
                                          memberName(member['id']),
                                    ),
                                    subtitle: Text(
                                      '${memberName(member['id'])} · ${member['model'] ?? ''}',
                                    ),
                                    trailing: const Icon(Icons.chevron_right),
                                    onTap: () => widget.onOpenChat(
                                      asString(member['id']) ?? '',
                                    ),
                                  ),
                                  label(
                                    asString(member['membership_state']) ==
                                            'active'
                                        ? asString(member['execution_state']) ??
                                              'idle'
                                        : asString(
                                                member['membership_state'],
                                              ) ??
                                              'idle',
                                  ),
                                  if (member['responsibility']
                                      case final String text)
                                    Text(text),
                                  if (member['error'] case final String error)
                                    Text(
                                      error,
                                      style: TextStyle(color: t.danger),
                                    ),
                                  Text(
                                    '${i18n.t('teams:skills')}: ${asList(member['skill_refs']).map((s) => asMap(s)['name']).join(', ')}',
                                  ),
                                  Text(
                                    '${i18n.t('teams:tools')}: ${asList(member['tool_ids']).join(', ')}',
                                  ),
                                ],
                              ),
                            ),
                          for (final notice in snapshot.notices.where(
                            (item) =>
                                (asString(item['message']) ??
                                        asString(item['reason']) ??
                                        '')
                                    .isNotEmpty,
                          ))
                            ListTile(
                              leading: Icon(Icons.info_outline, color: t.n600),
                              title: Text(
                                asString(notice['message']) ??
                                    asString(notice['reason']) ??
                                    '',
                              ),
                            ),
                        ],
                        if (_tab == 'tasks')
                          collection(
                            'tasks',
                            (task) => ExpansionTile(
                              key: PageStorageKey(task['id']),
                              title: Text(asString(task['title']) ?? ''),
                              subtitle: Text(
                                '${memberName(task['owner_member_id'])} · ${i18n.t('teams:state.${task['state']}')}',
                              ),
                              children: [
                                _TaskBody(
                                  children: [
                                    if (task['description']
                                        case final String description)
                                      SelectableText(description),
                                    if (task['blocked_reason']
                                        case final String reason)
                                      SelectableText(reason),
                                    if (task['expected_output']
                                        case final String output)
                                      SelectableText(
                                        '${i18n.t('teams:expectedOutput')}: $output',
                                      ),
                                    collection(
                                      'attempts',
                                      (attempt) => ListTile(
                                        title: Text(
                                          '${i18n.t('teams:attempt', count: asInt(attempt['number']) ?? 0)} · ${i18n.t('teams:state.${attempt['state']}')}',
                                        ),
                                        subtitle: Column(
                                          crossAxisAlignment:
                                              CrossAxisAlignment.start,
                                          children: [
                                            if (attempt['summary']
                                                case final String text)
                                              widget.renderText(text),
                                            if (attempt['output'] != null)
                                              SelectableText(
                                                const JsonEncoder.withIndent(
                                                  '  ',
                                                ).convert(attempt['output']),
                                              ),
                                            if (attempt['error']
                                                case final String error)
                                              SelectableText(error),
                                          ],
                                        ),
                                      ),
                                      query: {
                                        'task_id': asString(task['id']) ?? '',
                                      },
                                    ),
                                  ],
                                ),
                              ],
                            ),
                          ),
                        if (_tab == 'messages')
                          collection(
                            'messages',
                            (message) => ListTile(
                              title: Text(
                                i18n.t(
                                  'teams:messageFromTo',
                                  vars: {
                                    'from': memberName(
                                      message['from_member_id'],
                                    ),
                                    'to': memberName(message['to_member_id']),
                                  },
                                ),
                              ),
                              subtitle: SelectableText(
                                asString(message['body']) ?? '',
                              ),
                            ),
                            empty: 'teams:noMessages',
                          ),
                        if (_tab == 'artifacts') ...[
                          if (snapshot.run.finalSummary.isNotEmpty)
                            widget.renderText(snapshot.run.finalSummary),
                          collection(
                            'artifacts',
                            widget.renderArtifact,
                            empty: 'teams:noArtifacts',
                          ),
                        ],
                        if (_tab == 'usage')
                          collection(
                            'usage',
                            (usage) => Column(
                              crossAxisAlignment: CrossAxisAlignment.stretch,
                              children: [
                                Text(
                                  i18n.t(
                                    'teams:usageValue',
                                    vars: {
                                      'credits': usage['credits'] ?? '0',
                                      'tokens': usage['tokens'] ?? 0,
                                    },
                                  ),
                                ),
                                if ((asInt(usage['pending']) ?? 0) +
                                        (asInt(usage['unpriced']) ?? 0) >
                                    0)
                                  Text(
                                    i18n.t(
                                      'teams:usageIncomplete',
                                      count:
                                          (asInt(usage['pending']) ?? 0) +
                                          (asInt(usage['unpriced']) ?? 0),
                                    ),
                                  ),
                                Text(i18n.t('teams:usageSource')),
                                for (final category in asList(
                                  usage['categories'],
                                ).map(asMap))
                                  ListTile(
                                    title: Text(
                                      i18n.t(
                                        'teams:costCategory.${category['category']}',
                                      ),
                                    ),
                                    subtitle: Text(
                                      i18n.t(
                                        'teams:usageValue',
                                        vars: {
                                          'credits': category['credits'] ?? '0',
                                          'tokens': category['tokens'] ?? 0,
                                        },
                                      ),
                                    ),
                                  ),
                              ],
                            ),
                          ),
                      ],
                    ),
                  ),
                ),
              ],
            ),
    );
  }
}

/// ExpansionTile stores a boolean in its PageStorage entry. Nested text
/// scrollables must not read that entry as a scroll offset (a double).
class _TaskBody extends StatefulWidget {
  const _TaskBody({required this.children});
  final List<Widget> children;
  @override
  State<_TaskBody> createState() => _TaskBodyState();
}

class _TaskBodyState extends State<_TaskBody> {
  final _bucket = PageStorageBucket();
  @override
  Widget build(BuildContext context) => PageStorage(
    bucket: _bucket,
    child: Column(children: widget.children),
  );
}
