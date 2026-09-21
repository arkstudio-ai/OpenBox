import 'dart:async';

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/json.dart';
import '../../../shared/models/team.dart';
import '../../../shared/utils/error_text.dart';
import '../../../shared/utils/format.dart';
import '../../../shared/widgets/spinner.dart';
import '../api/teams_api.dart';
import '../state/team_providers.dart';
import 'team_bits.dart';

const _statuses = ['running', 'paused', 'completed', 'canceled', 'failed'];

/// Every team run in this workspace, newest first (§13.8, web `RunHistory`).
/// Reads `team_runs` rows only — no event replay — and opens a run by opening
/// its root conversation.
class RunHistory extends ConsumerStatefulWidget {
  const RunHistory({
    super.key,
    required this.scope,
    required this.projectName,
    required this.projects,
    required this.onOpen,
    required this.onRunAgain,
  });

  final TeamScope scope;

  /// Project id → display name, for the row's "where it ran" line.
  final String Function(String id) projectName;
  final List<({String id, String name})> projects;
  final ValueChanged<TeamRunInfo> onOpen;
  final ValueChanged<TeamRunInfo> onRunAgain;

  @override
  ConsumerState<RunHistory> createState() => _RunHistoryState();
}

class _RunHistoryState extends ConsumerState<RunHistory> {
  String _status = '';
  String _project = '';
  String _template = '';
  final _rows = <TeamRunInfo>[];
  String? _cursor;
  bool _busy = false;
  Object? _error;
  int _epoch = 0;
  CancelToken? _cancel;

  @override
  void initState() {
    super.initState();
    unawaited(_load(reset: true));
  }

  @override
  void dispose() {
    _cancel?.cancel();
    super.dispose();
  }

  void _filter(void Function() change) {
    setState(change);
    unawaited(_load(reset: true));
  }

  Future<void> _load({bool reset = false}) async {
    if (_busy && !reset) return;
    _cancel?.cancel();
    final cancel = _cancel = CancelToken();
    final epoch = ++_epoch;
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final page = await ref
          .read(teamsApiProvider)
          .runs(
            widget.scope,
            status: _status.isEmpty ? null : _status,
            projectId: _project.isEmpty ? null : _project,
            templateId: _template.isEmpty ? null : _template,
            cursor: reset ? null : _cursor,
            cancel: cancel,
          );
      // A late page from a filter the user already changed must not append.
      if (!mounted || epoch != _epoch) return;
      setState(() {
        if (reset) _rows.clear();
        _rows.addAll(
          asList(page['items']).map(asMap).map(TeamRunInfo.fromJson),
        );
        _cursor = asString(page['next_cursor']);
      });
    } catch (error) {
      if (mounted && epoch == _epoch) setState(() => _error = error);
    } finally {
      if (mounted && epoch == _epoch) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final templates =
        ref.watch(teamTemplatesProvider(widget.scope)).valueOrNull ??
        const <TeamDefinition>[];

    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        SingleChildScrollView(
          scrollDirection: Axis.horizontal,
          child: Row(
            children: [
              _FilterChip(
                label: i18n.t('teams:allStates'),
                selected: _status.isEmpty,
                onTap: () => _filter(() => _status = ''),
              ),
              for (final status in _statuses)
                _FilterChip(
                  label: i18n.t('teams:state.$status'),
                  selected: _status == status,
                  onTap: () => _filter(() => _status = status),
                ),
            ],
          ),
        ),
        const SizedBox(height: 8),
        Row(
          children: [
            Expanded(
              child: _PickerChip(
                label: _project.isEmpty
                    ? i18n.t('teams:allProjects')
                    : widget.projectName(_project),
                onTap: () => _pick(
                  title: i18n.t('teams:project'),
                  allLabel: i18n.t('teams:allProjects'),
                  current: _project,
                  options: [
                    for (final project in widget.projects)
                      (id: project.id, name: project.name),
                  ],
                  onPick: (value) => _filter(() => _project = value),
                ),
              ),
            ),
            const SizedBox(width: 8),
            Expanded(
              child: _PickerChip(
                label: _template.isEmpty
                    ? i18n.t('teams:allTemplates')
                    : templates
                              .where((entry) => entry.id == _template)
                              .firstOrNull
                              ?.name ??
                          i18n.t('teams:template'),
                onTap: () => _pick(
                  title: i18n.t('teams:template'),
                  allLabel: i18n.t('teams:allTemplates'),
                  current: _template,
                  options: [
                    for (final entry in templates)
                      (id: entry.id, name: entry.name),
                  ],
                  onPick: (value) => _filter(() => _template = value),
                ),
              ),
            ),
          ],
        ),
        const SizedBox(height: 12),
        for (final run in _rows)
          _RunRow(
            run: run,
            projectName: widget.projectName,
            onOpen: () => widget.onOpen(run),
            onRunAgain: () => widget.onRunAgain(run),
          ),
        if (_busy)
          const Padding(
            padding: EdgeInsets.symmetric(vertical: 20),
            child: Center(child: Spinner()),
          ),
        if (_error != null)
          Padding(
            padding: const EdgeInsets.symmetric(vertical: 8),
            child: Row(
              children: [
                Expanded(
                  child: Text(
                    errorText(i18n, _error!),
                    style: TextStyle(fontSize: FontSizes.xs, color: t.danger),
                  ),
                ),
                TeamActionLink(
                  label: i18n.t('common:action.retry'),
                  onTap: () => _load(reset: _rows.isEmpty),
                ),
              ],
            ),
          ),
        if (!_busy && _error == null && _rows.isEmpty)
          Padding(
            padding: const EdgeInsets.symmetric(vertical: 28),
            child: Text(
              i18n.t('teams:emptyRuns'),
              textAlign: TextAlign.center,
              style: TextStyle(
                fontSize: FontSizes.sm,
                color: t.n600,
                height: 1.6,
              ),
            ),
          ),
        if (_cursor != null && _cursor!.isNotEmpty)
          Align(
            alignment: AlignmentDirectional.centerStart,
            child: TeamActionLink(
              label: i18n.t('teams:loadMore'),
              onTap: _busy ? null : _load,
            ),
          ),
      ],
    );
  }

  Future<void> _pick({
    required String title,
    required String allLabel,
    required String current,
    required List<({String id, String name})> options,
    required ValueChanged<String> onPick,
  }) {
    final t = context.tokens;
    return showModalBottomSheet<void>(
      context: context,
      backgroundColor: t.card,
      isScrollControlled: true,
      builder: (sheetContext) => SafeArea(
        child: ConstrainedBox(
          constraints: BoxConstraints(
            maxHeight: MediaQuery.sizeOf(context).height * 0.6,
          ),
          child: ListView(
            shrinkWrap: true,
            padding: const EdgeInsets.symmetric(vertical: 8),
            children: [
              Padding(
                padding: const EdgeInsets.fromLTRB(20, 8, 20, 8),
                child: Text(
                  title,
                  style: TextStyle(
                    fontSize: FontSizes.sm,
                    fontWeight: FontWeight.w600,
                    color: t.n600,
                  ),
                ),
              ),
              for (final option in [(id: '', name: allLabel), ...options])
                ListTile(
                  dense: true,
                  title: Text(
                    option.name,
                    style: TextStyle(fontSize: FontSizes.base, color: t.ink),
                  ),
                  trailing: option.id == current
                      ? Icon(Icons.check, size: 18, color: t.a700)
                      : null,
                  onTap: () {
                    Navigator.pop(sheetContext);
                    onPick(option.id);
                  },
                ),
            ],
          ),
        ),
      ),
    );
  }
}

class _RunRow extends ConsumerWidget {
  const _RunRow({
    required this.run,
    required this.projectName,
    required this.onOpen,
    required this.onRunAgain,
  });

  final TeamRunInfo run;
  final String Function(String id) projectName;
  final VoidCallback onOpen, onRunAgain;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final usage = run.usage;
    final incomplete = usage == null
        ? 0
        : (asInt(usage['pending']) ?? 0) + (asInt(usage['unpriced']) ?? 0);

    return Container(
      margin: const EdgeInsets.only(bottom: 8),
      padding: const EdgeInsets.fromLTRB(14, 12, 14, 10),
      decoration: BoxDecoration(
        color: t.hairSoft,
        borderRadius: BorderRadius.circular(Radii.xl),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            children: [
              Container(
                width: 6,
                height: 6,
                decoration: BoxDecoration(
                  color: teamStateColor(t, run.state),
                  shape: BoxShape.circle,
                ),
              ),
              const SizedBox(width: 8),
              Expanded(
                child: Text(
                  run.title,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(
                    fontSize: FontSizes.base,
                    fontWeight: FontWeight.w500,
                    color: t.ink,
                  ),
                ),
              ),
              const SizedBox(width: 8),
              Text(
                i18n.t('teams:state.${run.state}'),
                style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
              ),
            ],
          ),
          // The one thing that decides whether this row is the user's problem.
          if (run.needsAttention)
            Padding(
              padding: const EdgeInsets.only(top: 6),
              child: Text(
                i18n.t('teams:needsAttention'),
                style: TextStyle(
                  fontSize: FontSizes.xs,
                  color: t.a700,
                  height: 1.6,
                ),
              ),
            ),
          if (run.pauseReason != null)
            Padding(
              padding: const EdgeInsets.only(top: 4),
              child: Text(
                () {
                  final key = 'teams:reason.${run.pauseReason}';
                  final line = i18n.t(key);
                  return line == key ? run.pauseReason! : line;
                }(),
                style: TextStyle(
                  fontSize: FontSizes.xs,
                  color: t.a700,
                  height: 1.6,
                ),
              ),
            ),
          Padding(
            padding: const EdgeInsets.only(top: 6),
            child: Text(
              [
                projectName(run.projectId),
                if (run.createdAt != null)
                  formatRelative(run.createdAt!, i18n.language),
                if (run.taskCount != null)
                  i18n.t('teams:totalTasks', count: run.taskCount!),
              ].where((part) => part.isNotEmpty).join(' · '),
              style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
            ),
          ),
          if (usage != null)
            Padding(
              padding: const EdgeInsets.only(top: 4),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    i18n.t(
                      'teams:usageValue',
                      vars: {
                        'credits': formatCredits('${usage['credits'] ?? '0'}'),
                        'tokens': formatTokens(asInt(usage['tokens']) ?? 0),
                      },
                    ),
                    style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
                  ),
                  if (incomplete > 0)
                    Text(
                      i18n.t('teams:usageIncomplete', count: incomplete),
                      style: TextStyle(fontSize: FontSizes.xs, color: t.a700),
                    ),
                ],
              ),
            ),
          Padding(
            padding: const EdgeInsets.only(top: 4),
            child: Wrap(
              spacing: 16,
              children: [
                TeamActionLink(label: i18n.t('teams:openChat'), onTap: onOpen),
                TeamActionLink(
                  label: i18n.t('teams:runAgain'),
                  onTap: onRunAgain,
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _FilterChip extends StatelessWidget {
  const _FilterChip({
    required this.label,
    required this.selected,
    required this.onTap,
  });

  final String label;
  final bool selected;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Padding(
      padding: const EdgeInsetsDirectional.only(end: 6),
      child: Material(
        color: selected ? t.ink : Colors.transparent,
        borderRadius: BorderRadius.circular(Radii.full),
        child: InkWell(
          borderRadius: BorderRadius.circular(Radii.full),
          onTap: onTap,
          child: Container(
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 7),
            decoration: BoxDecoration(
              border: Border.all(color: selected ? t.ink : t.hair),
              borderRadius: BorderRadius.circular(Radii.full),
            ),
            child: Text(
              label,
              style: TextStyle(
                fontSize: FontSizes.xs,
                color: selected ? t.bg : t.n700,
              ),
            ),
          ),
        ),
      ),
    );
  }
}

class _PickerChip extends StatelessWidget {
  const _PickerChip({required this.label, required this.onTap});

  final String label;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Material(
      color: Colors.transparent,
      child: InkWell(
        borderRadius: BorderRadius.circular(Radii.full),
        onTap: onTap,
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
          decoration: BoxDecoration(
            border: Border.all(color: t.hair),
            borderRadius: BorderRadius.circular(Radii.full),
          ),
          child: Row(
            children: [
              Expanded(
                child: Text(
                  label,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(fontSize: FontSizes.xs, color: t.n700),
                ),
              ),
              Icon(Icons.expand_more, size: 14, color: t.n500),
            ],
          ),
        ),
      ),
    );
  }
}
