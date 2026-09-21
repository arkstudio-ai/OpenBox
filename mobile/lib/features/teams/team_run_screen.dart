import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../shared/appearance/tokens.dart';
import '../../shared/appearance/type_scale.dart';
import '../../shared/i18n/i18n.dart';
import '../../shared/models/json.dart';
import '../../shared/models/team.dart';
import '../../shared/utils/error_text.dart';
import '../../shared/utils/format.dart';
import '../../shared/widgets/fold.dart';
import '../../shared/widgets/section_tabs.dart';
import '../../shared/widgets/spinner.dart';
import '../../shared/widgets/task_card_frame.dart';
import 'state/team_providers.dart';
import 'widgets/save_configuration.dart';
import 'widgets/team_bits.dart';
import 'widgets/team_collection.dart';
import 'widgets/team_progress_card.dart';

/// One team run (web's workbench team tab, §13.3). The phone shows the roster
/// as a list with the same detail card the graph opens — §13.6 keeps the link
/// graph off mobile, where it would not survive the width.
class TeamRunScreen extends ConsumerStatefulWidget {
  const TeamRunScreen({
    super.key,
    required this.scope,
    required this.runId,
    required this.onOpenChat,
    required this.onOpenLibrary,
    required this.renderText,
    required this.renderArtifact,
  });

  final TeamScope scope;
  final String runId;
  final ValueChanged<String> onOpenChat;

  /// Where a saved roster or member lands: the Agent team library, on the
  /// given tab.
  final ValueChanged<String> onOpenLibrary;
  final Widget Function(String) renderText;
  final Widget Function(Map<String, dynamic>) renderArtifact;

  @override
  ConsumerState<TeamRunScreen> createState() => _TeamRunScreenState();
}

class _TeamRunScreenState extends ConsumerState<TeamRunScreen> {
  static const _tabs = ['members', 'tasks', 'messages', 'artifacts', 'usage'];
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

    String memberName(Object? id) {
      final member = snapshot?.members
          .where((entry) => entry['id'] == id)
          .firstOrNull;
      return asString(member?['name']) ??
          asString(member?['alias']) ??
          id?.toString() ??
          '';
    }

    return Scaffold(
      backgroundColor: t.bg,
      appBar: AppBar(
        titleSpacing: 0,
        title: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              snapshot?.run.title ?? i18n.t('teams:progress'),
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(
                fontSize: FontSizes.lg,
                fontWeight: FontWeight.w500,
                color: t.ink,
              ),
            ),
            if (snapshot != null)
              Text(
                i18n.t('teams:state.${snapshot.run.state}'),
                style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
              ),
          ],
        ),
      ),
      body: snapshot == null
          ? Center(
              child: value.hasError
                  ? Padding(
                      padding: const EdgeInsets.all(24),
                      child: Column(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          Text(
                            errorText(i18n, value.error!),
                            textAlign: TextAlign.center,
                            style: TextStyle(
                              fontSize: FontSizes.sm,
                              color: t.danger,
                            ),
                          ),
                          const SizedBox(height: 8),
                          TeamActionLink(
                            label: i18n.t('common:action.retry'),
                            onTap: () => ref.invalidate(teamRunProvider(key)),
                          ),
                        ],
                      ),
                    )
                  : const Spinner(size: 20),
            )
          : Column(
              children: [
                Padding(
                  padding: const EdgeInsets.fromLTRB(16, 4, 16, 10),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      Wrap(
                        spacing: 16,
                        crossAxisAlignment: WrapCrossAlignment.center,
                        children: [
                          TeamControls(
                            scope: widget.scope,
                            run: snapshot.run,
                            onDetails: null,
                          ),
                          // A finished run is worth keeping: the same two
                          // saves the web panel offers (§13.2 A6).
                          if (snapshot.run.terminal)
                            SaveConfigurationLink(
                              scope: widget.scope,
                              snapshot: snapshot,
                              onOpenLibrary: widget.onOpenLibrary,
                            ),
                        ],
                      ),
                      TeamAttention(snapshot: snapshot, notices: true),
                    ],
                  ),
                ),
                SectionTabs(
                  labels: {
                    for (final tab in _tabs) tab: i18n.t('teams:tabs.$tab'),
                  },
                  value: _tab,
                  onChanged: (tab) => setState(() => _tab = tab),
                ),
                Expanded(
                  child: RefreshIndicator(
                    color: t.accent,
                    onRefresh: () =>
                        ref.read(teamRunProvider(key).notifier).refresh(),
                    child: ListView(
                      padding: const EdgeInsets.fromLTRB(16, 12, 16, 24),
                      physics: const AlwaysScrollableScrollPhysics(),
                      children: [
                        // A failed refresh keeps the last good snapshot on
                        // screen and says so, rather than emptying the page.
                        if (value.hasError)
                          Padding(
                            padding: const EdgeInsets.only(bottom: 8),
                            child: Text(
                              errorText(i18n, value.error!),
                              style: TextStyle(
                                fontSize: FontSizes.xs,
                                color: t.danger,
                              ),
                            ),
                          ),
                        if (_tab == 'members')
                          for (final member in snapshot.members)
                            _MemberCard(
                              scope: widget.scope,
                              snapshot: snapshot,
                              member: member,
                              isCoordinator:
                                  asString(member['role']) == 'coordinator',
                              onOpen: () => widget.onOpenChat(
                                asString(member['role']) == 'coordinator'
                                    ? snapshot.run.rootSessionId
                                    : asString(member['id']) ?? '',
                              ),
                              onTasks: () => setState(() => _tab = 'tasks'),
                              onMessages: () =>
                                  setState(() => _tab = 'messages'),
                              onOpenLibrary: widget.onOpenLibrary,
                            ),
                        if (_tab == 'tasks')
                          collection(
                            'tasks',
                            (task) => _TaskCard(
                              task: task,
                              members: snapshot.members,
                              memberName: memberName,
                              tasks: snapshot.tasks,
                              attempts: (taskId) => collection(
                                'attempts',
                                (attempt) => _AttemptTile(
                                  attempt: attempt,
                                  renderText: widget.renderText,
                                ),
                                query: {'task_id': taskId},
                              ),
                            ),
                          ),
                        if (_tab == 'messages')
                          collection(
                            'messages',
                            (message) => _MessageCard(
                              message: message,
                              memberName: memberName,
                            ),
                            empty: 'teams:noMessages',
                          ),
                        if (_tab == 'artifacts') ...[
                          if (snapshot.run.finalSummary.isNotEmpty)
                            TaskCardFrame(
                              child: widget.renderText(
                                snapshot.run.finalSummary,
                              ),
                            ),
                          collection(
                            'artifacts',
                            (artifact) => TaskCardFrame(
                              child: widget.renderArtifact(artifact),
                            ),
                            empty: 'teams:noArtifacts',
                          ),
                        ],
                        if (_tab == 'usage')
                          collection(
                            'usage',
                            (usage) => _UsagePanel(
                              usage: usage,
                              memberName: memberName,
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

/// One member of the roster. The web panel draws a link graph and opens this
/// same detail on a node; a phone shows the detail directly and writes the
/// graph's edges as text (§13.3 keeps the graph off narrow screens, but not
/// the information in it).
class _MemberCard extends ConsumerWidget {
  const _MemberCard({
    required this.scope,
    required this.snapshot,
    required this.member,
    required this.isCoordinator,
    required this.onOpen,
    required this.onTasks,
    required this.onMessages,
    required this.onOpenLibrary,
  });

  final TeamScope scope;
  final TeamSnapshot snapshot;
  final Map<String, dynamic> member;
  final bool isCoordinator;
  final VoidCallback onOpen, onTasks, onMessages;
  final ValueChanged<String> onOpenLibrary;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final state = teamMemberState(member);
    final skills = asList(member['skill_refs'])
        .map((skill) => asString(asMap(skill)['name']) ?? '')
        .where((name) => name.isNotEmpty);
    final tools = asList(member['tool_ids']).map((tool) => '$tool');
    final source = asString(member['source']);
    final sourceLabel = source == null
        ? null
        : () {
            final label = i18n.t('teams:source.$source');
            return label == 'teams:source.$source' ? null : label;
          }();

    return TaskCardFrame(
      child: InkWell(
        onTap: onOpen,
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Row(
              children: [
                TeamAvatar(
                  display: asMap(member['display']),
                  dotColor: teamStateColor(t, state),
                ),
                const SizedBox(width: 10),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        asString(member['name']) ??
                            asString(member['alias']) ??
                            '',
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: TextStyle(
                          fontSize: FontSizes.base,
                          fontWeight: FontWeight.w500,
                          color: t.ink,
                        ),
                      ),
                      const SizedBox(height: 2),
                      Text(
                        [
                          if (isCoordinator) i18n.t('teams:coordinator'),
                          ?sourceLabel,
                          i18n.t('teams:state.$state'),
                          asString(member['model']) ?? '',
                        ].where((part) => part.isNotEmpty).join(' · '),
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
                      ),
                    ],
                  ),
                ),
                Icon(Icons.chevron_right, size: 18, color: t.n500),
              ],
            ),
            if (asString(member['responsibility'])?.isNotEmpty == true ||
                asString(member['description'])?.isNotEmpty == true)
              Padding(
                padding: const EdgeInsets.only(top: 8),
                child: Text(
                  asString(member['responsibility']) ??
                      asString(member['description'])!,
                  style: TextStyle(
                    fontSize: FontSizes.sm,
                    color: t.n700,
                    height: 1.6,
                  ),
                ),
              ),
            if (asString(member['error'])?.isNotEmpty == true)
              Padding(
                padding: const EdgeInsets.only(top: 6),
                child: Text(
                  asString(member['error'])!,
                  style: TextStyle(fontSize: FontSizes.xs, color: t.danger),
                ),
              ),
            if (skills.isNotEmpty)
              TeamMetaLine(
                label: i18n.t('teams:skills'),
                value: skills.join(' · '),
              ),
            if (tools.isNotEmpty)
              TeamMetaLine(
                label: i18n.t('teams:tools'),
                value: tools.join(', '),
              ),
            // What the graph's edges say: who this member was given work by,
            // and who it has been talking to.
            for (final line in _exchanges(i18n))
              Padding(
                padding: const EdgeInsets.only(top: 4),
                child: Text(
                  line,
                  style: TextStyle(
                    fontSize: FontSizes.xs,
                    color: t.n600,
                    height: 1.6,
                  ),
                ),
              ),
            Padding(
              padding: const EdgeInsets.only(top: 8),
              child: Wrap(
                spacing: 16,
                runSpacing: 2,
                children: [
                  TeamActionLink(
                    label: i18n.t(
                      isCoordinator ? 'teams:openChat' : 'teams:openMember',
                    ),
                    onTap: onOpen,
                  ),
                  if (_currentTask != null)
                    TeamActionLink(
                      label:
                          '${i18n.t('teams:currentTask')}: '
                          '${asString(_currentTask!['title']) ?? ''}',
                      onTap: onTasks,
                    ),
                  if (_hasMessages)
                    TeamActionLink(
                      label: i18n.t('teams:tabs.messages'),
                      onTap: onMessages,
                    ),
                  if (!isCoordinator)
                    SaveConfigurationLink(
                      scope: scope,
                      snapshot: snapshot,
                      member: member,
                      onOpenLibrary: onOpenLibrary,
                    ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }

  /// The task this member is working on right now — matched through the
  /// attempt it holds, as the web detail card does.
  Map<String, dynamic>? get _currentTask {
    final attempt = asString(member['current_attempt']);
    if (attempt == null) return null;
    return snapshot.tasks
        .where((task) => asString(task['current_attempt']) == attempt)
        .firstOrNull;
  }

  bool get _hasMessages => snapshot.links.any(
    (link) =>
        link['kind'] == 'message' &&
        (link['from'] == member['id'] || link['to'] == member['id']),
  );

  List<String> _exchanges(I18nState i18n) {
    String name(Object? id) {
      if (id == snapshot.run.rootSessionId) return i18n.t('teams:coordinator');
      final entry = snapshot.members
          .where((member) => member['id'] == id)
          .firstOrNull;
      return asString(entry?['name']) ?? asString(entry?['alias']) ?? '';
    }

    return [
      for (final link in snapshot.links)
        if (link['from'] == member['id'] || link['to'] == member['id'])
          i18n.t(
            link['kind'] == 'task' ? 'teams:link.task' : 'teams:link.message',
            count: asInt(link['count']) ?? 0,
            vars: {'from': name(link['from']), 'to': name(link['to'])},
          ),
    ];
  }
}

/// One task, folding open to its dependencies, acceptance and attempts
/// (web `TeamTaskRow`).
class _TaskCard extends ConsumerStatefulWidget {
  const _TaskCard({
    required this.task,
    required this.members,
    required this.tasks,
    required this.memberName,
    required this.attempts,
  });

  final Map<String, dynamic> task;
  final List<Map<String, dynamic>> members, tasks;
  final String Function(Object?) memberName;
  final Widget Function(String taskId) attempts;

  @override
  ConsumerState<_TaskCard> createState() => _TaskCardState();
}

class _TaskCardState extends ConsumerState<_TaskCard> {
  bool _open = false;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final task = widget.task;
    final state = teamTaskState(task, widget.members);
    final id = asString(task['id']) ?? '';
    final dependencies = asList(task['dependencies'])
        .map(
          (dependency) =>
              asString(
                widget.tasks
                    .where((entry) => entry['id'] == dependency)
                    .firstOrNull?['title'],
              ) ??
              '$dependency',
        )
        .join(' · ');

    return TaskCardFrame(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          InkWell(
            onTap: () => setState(() => _open = !_open),
            child: Row(
              children: [
                TeamStatusMark(state: state),
                const SizedBox(width: 11),
                Expanded(
                  child: Text(
                    asString(task['title']) ?? '',
                    style: TextStyle(fontSize: FontSizes.base, color: t.ink),
                  ),
                ),
                const SizedBox(width: 8),
                ConstrainedBox(
                  constraints: const BoxConstraints(maxWidth: 72),
                  child: Text(
                    i18n.t('teams:state.$state'),
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
                  ),
                ),
                const SizedBox(width: 4),
                AnimatedRotation(
                  turns: _open ? 0.5 : 0,
                  duration: const Duration(milliseconds: 200),
                  child: Icon(Icons.expand_more, size: 15, color: t.n500),
                ),
              ],
            ),
          ),
          Fold(
            open: _open,
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                if (asString(task['description'])?.isNotEmpty == true)
                  Padding(
                    padding: const EdgeInsets.only(top: 10),
                    child: SelectableText(
                      asString(task['description'])!,
                      style: TextStyle(
                        fontSize: FontSizes.sm,
                        color: t.n700,
                        height: 1.6,
                      ),
                    ),
                  ),
                TeamMetaLine(
                  label: i18n.t('teams:owner'),
                  value: widget.memberName(task['owner_member_id']),
                ),
                TeamMetaLine(
                  label: i18n.t('teams:expectedOutput'),
                  value: asString(task['expected_output']) ?? '',
                  selectable: true,
                ),
                TeamMetaLine(
                  label: i18n.t('teams:dependencies'),
                  value: dependencies,
                ),
                if (asString(task['blocked_reason'])?.isNotEmpty == true)
                  Padding(
                    padding: const EdgeInsets.only(top: 6),
                    child: Text(
                      asString(task['blocked_reason'])!,
                      style: TextStyle(fontSize: FontSizes.xs, color: t.a700),
                    ),
                  ),
                if (id.isNotEmpty)
                  Padding(
                    padding: const EdgeInsets.only(top: 4),
                    child: widget.attempts(id),
                  ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

/// One attempt at a task: how it went, and what it produced.
class _AttemptTile extends ConsumerWidget {
  const _AttemptTile({required this.attempt, required this.renderText});

  final Map<String, dynamic> attempt;
  final Widget Function(String) renderText;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return Container(
      margin: const EdgeInsets.only(top: 8),
      padding: const EdgeInsets.only(top: 8),
      decoration: BoxDecoration(
        border: Border(top: BorderSide(color: t.hair)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            children: [
              Text(
                i18n.t('teams:attempt', count: asInt(attempt['number']) ?? 0),
                style: TextStyle(
                  fontSize: FontSizes.xs,
                  fontWeight: FontWeight.w500,
                  color: t.n700,
                ),
              ),
              const SizedBox(width: 8),
              Text(
                i18n.t('teams:state.${attempt['state']}'),
                style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
              ),
              if (attempt['implicit'] == true) ...[
                const SizedBox(width: 8),
                Text(
                  i18n.t('teams:implicit'),
                  style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
                ),
              ],
            ],
          ),
          if (asString(attempt['summary'])?.isNotEmpty == true)
            Padding(
              padding: const EdgeInsets.only(top: 4),
              child: renderText(asString(attempt['summary'])!),
            ),
          if (attempt['output'] != null)
            Padding(
              padding: const EdgeInsets.only(top: 6),
              child: Container(
                width: double.infinity,
                padding: const EdgeInsets.all(10),
                decoration: BoxDecoration(
                  color: t.n100,
                  borderRadius: BorderRadius.circular(Radii.md),
                ),
                child: SelectableText(
                  const JsonEncoder.withIndent('  ').convert(attempt['output']),
                  style: TextStyle(
                    fontSize: FontSizes.xs,
                    color: t.n800,
                    fontFamily: 'Menlo',
                    fontFamilyFallback: const ['monospace'],
                  ),
                ),
              ),
            ),
          if (asString(attempt['error'])?.isNotEmpty == true)
            Padding(
              padding: const EdgeInsets.only(top: 4),
              child: SelectableText(
                asString(attempt['error'])!,
                style: TextStyle(fontSize: FontSizes.xs, color: t.danger),
              ),
            ),
        ],
      ),
    );
  }
}

class _MessageCard extends ConsumerWidget {
  const _MessageCard({required this.message, required this.memberName});

  final Map<String, dynamic> message;
  final String Function(Object?) memberName;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return TaskCardFrame(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Wrap(
            spacing: 8,
            children: [
              Text(
                i18n.t(
                  'teams:messageFromTo',
                  vars: {
                    'from': memberName(message['from_member_id']),
                    'to': memberName(message['to_member_id']),
                  },
                ),
                style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
              ),
              Text(
                i18n.t('teams:messageKind.${message['kind']}'),
                style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
              ),
              Text(
                i18n.t('teams:state.${message['state']}'),
                style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
              ),
            ],
          ),
          const SizedBox(height: 6),
          SelectableText(
            asString(message['body']) ?? '',
            style: TextStyle(fontSize: FontSizes.sm, color: t.ink, height: 1.6),
          ),
        ],
      ),
    );
  }
}

/// Credits for this run, by category and by member (web `TeamUsagePanel`).
/// Unknown prices stay visible instead of being counted as free.
class _UsagePanel extends ConsumerWidget {
  const _UsagePanel({required this.usage, required this.memberName});

  final Map<String, dynamic> usage;
  final String Function(Object?) memberName;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);

    Widget summary(Map<String, dynamic> entry) {
      final incomplete =
          (asInt(entry['pending']) ?? 0) + (asInt(entry['unpriced']) ?? 0);
      return Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            i18n.t(
              'teams:usageValue',
              vars: {
                // The same exact presentation the billing page uses: the
                // backend sends a Decimal string, and rounding it through a
                // double would make a small model charge read as zero.
                'credits': formatCredits('${entry['credits'] ?? '0'}'),
                'tokens': formatTokens(asInt(entry['tokens']) ?? 0),
              },
            ),
            style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
          ),
          if (incomplete > 0)
            Padding(
              padding: const EdgeInsets.only(top: 2),
              child: Text(
                i18n.t('teams:usageIncomplete', count: incomplete),
                style: TextStyle(fontSize: FontSizes.xs, color: t.a700),
              ),
            ),
        ],
      );
    }

    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        TaskCardFrame(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              summary(usage),
              const SizedBox(height: 6),
              Text(
                i18n.t('teams:usageSource'),
                style: TextStyle(
                  fontSize: FontSizes.xs,
                  color: t.n600,
                  height: 1.6,
                ),
              ),
            ],
          ),
        ),
        for (final category in asList(usage['categories']).map(asMap))
          if (asString(category['category']) != 'unattributed' ||
              (asInt(category['calls']) ?? 0) > 0)
            TaskCardFrame(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  Text(
                    i18n.t('teams:costCategory.${category['category']}'),
                    style: TextStyle(
                      fontSize: FontSizes.sm,
                      fontWeight: FontWeight.w500,
                      color: t.ink,
                    ),
                  ),
                  const SizedBox(height: 4),
                  summary(category),
                ],
              ),
            ),
        Padding(
          padding: const EdgeInsets.fromLTRB(2, 2, 2, 10),
          child: Text(
            i18n.t('teams:costCategoryHint'),
            style: TextStyle(
              fontSize: FontSizes.xs,
              color: t.n600,
              height: 1.6,
            ),
          ),
        ),
        for (final item in asList(usage['items']).map(asMap))
          TaskCardFrame(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Text(
                  memberName(item['member_id']),
                  style: TextStyle(
                    fontSize: FontSizes.sm,
                    fontWeight: FontWeight.w500,
                    color: t.ink,
                  ),
                ),
                const SizedBox(height: 2),
                Text(
                  [
                    asString(item['model']) ?? '',
                    asString(item['kind']) ?? '',
                    i18n.t('teams:costCategory.${item['category']}'),
                  ].where((part) => part.isNotEmpty).join(' · '),
                  style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
                ),
                const SizedBox(height: 4),
                summary(item),
              ],
            ),
          ),
      ],
    );
  }
}
