import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_timezone/flutter_timezone.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/cron.dart';
import '../api/cron_api.dart';
import '../utils/schedule.dart';

/// Create / edit sheet (web `CronJobForm` dialog): name, task, project (on
/// create), schedule mode pills + per-mode fields. Editing keeps the job's
/// project/session binding.
Future<void> showCronJobForm(BuildContext context, {CronJob? job}) {
  return showModalBottomSheet<void>(
    context: context,
    isScrollControlled: true,
    useSafeArea: true,
    builder: (_) => Padding(
      padding: EdgeInsets.only(
        bottom: MediaQuery.of(context).viewInsets.bottom,
      ),
      child: _CronJobFormBody(job: job),
    ),
  );
}

class _CronJobFormBody extends ConsumerStatefulWidget {
  const _CronJobFormBody({this.job});

  final CronJob? job;

  @override
  ConsumerState<_CronJobFormBody> createState() => _CronJobFormBodyState();
}

class _CronJobFormBodyState extends ConsumerState<_CronJobFormBody> {
  late final _name = TextEditingController(text: widget.job?.name ?? '');
  late final _task = TextEditingController(text: widget.job?.taskPrompt ?? '');
  late final _expr = TextEditingController();
  late final _every = TextEditingController();
  late ScheduleForm _form = widget.job != null
      ? scheduleToForm(widget.job!.schedule)
      : const ScheduleForm();
  String _projectId = '';
  String _tz = 'UTC';
  bool _saving = false;
  bool _failed = false;

  @override
  void initState() {
    super.initState();
    _expr.text = _form.expr;
    _every.text = '${_form.every}';
    final storedTimezone = _form.timezone?.trim();
    if (storedTimezone != null && storedTimezone.isNotEmpty) {
      _tz = storedTimezone;
    } else {
      FlutterTimezone.getLocalTimezone().then((tz) {
        if (mounted) setState(() => _tz = tz.identifier);
      });
    }
  }

  @override
  void dispose() {
    _name.dispose();
    _task.dispose();
    _expr.dispose();
    _every.dispose();
    super.dispose();
  }

  void _patch(ScheduleForm next) => setState(() => _form = next);

  Future<void> _submit() async {
    final schedule = buildSchedule(_form, _tz);
    final projects = ref.read(cronProjectsProvider).valueOrNull ?? const [];
    final targetProject = widget.job != null
        ? (widget.job!.projectId ?? '')
        : (_projectId.isNotEmpty
              ? _projectId
              : (projects.isNotEmpty ? projects.first.$1 : ''));
    if (_name.text.trim().isEmpty ||
        _task.text.trim().isEmpty ||
        schedule == null ||
        targetProject.isEmpty ||
        _saving) {
      return;
    }
    setState(() {
      _saving = true;
      _failed = false;
    });
    try {
      final api = ref.read(cronApiProvider);
      if (widget.job != null) {
        await api.update(widget.job!.id, {
          'name': _name.text.trim(),
          'task_prompt': _task.text.trim(),
          'schedule': schedule.toJson(),
        });
      } else {
        await api.create(
          projectId: targetProject,
          name: _name.text.trim(),
          schedule: schedule,
          taskPrompt: _task.text.trim(),
        );
      }
      ref.invalidate(cronJobsProvider);
      ref.invalidate(cronStatusProvider);
      if (mounted) Navigator.pop(context);
    } catch (_) {
      if (mounted) {
        setState(() {
          _saving = false;
          _failed = true;
        });
      }
      return;
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final editing = widget.job != null;
    final projects = ref.watch(cronProjectsProvider).valueOrNull ?? const [];
    final targetProject = editing
        ? (widget.job!.projectId ?? '')
        : (_projectId.isNotEmpty
              ? _projectId
              : (projects.isNotEmpty ? projects.first.$1 : ''));
    final schedule = buildSchedule(_form, _tz);
    final valid =
        _name.text.trim().isNotEmpty &&
        _task.text.trim().isNotEmpty &&
        schedule != null &&
        targetProject.isNotEmpty;

    return DraggableScrollableSheet(
      expand: false,
      initialChildSize: 0.85,
      maxChildSize: 0.95,
      builder: (context, scrollController) => Padding(
        padding: const EdgeInsets.fromLTRB(18, 14, 18, 12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Text(
              editing
                  ? i18n.t('cron:form.editTitle')
                  : i18n.t('cron:form.createTitle'),
              style: TextStyle(
                fontSize: FontSizes.lg,
                fontWeight: FontWeight.w500,
                color: t.ink,
              ),
            ),
            const SizedBox(height: 12),
            Expanded(
              child: ListView(
                controller: scrollController,
                children: [
                  _field(
                    t,
                    i18n.t('cron:form.name'),
                    TextField(
                      controller: _name,
                      maxLength: 256,
                      onChanged: (_) => setState(() {}),
                      decoration: _decoration(
                        t,
                        i18n.t('cron:form.namePlaceholder'),
                      )..applyDefaults(const InputDecorationTheme()),
                      style: TextStyle(fontSize: FontSizes.sm, color: t.ink),
                    ),
                  ),
                  _field(
                    t,
                    i18n.t('cron:form.task'),
                    TextField(
                      controller: _task,
                      minLines: 3,
                      maxLines: 6,
                      maxLength: 5000,
                      onChanged: (_) => setState(() {}),
                      decoration: _decoration(
                        t,
                        i18n.t('cron:form.taskPlaceholder'),
                      ),
                      style: TextStyle(fontSize: FontSizes.sm, color: t.ink),
                    ),
                  ),
                  if (!editing)
                    _field(
                      t,
                      i18n.t('cron:form.project'),
                      Column(
                        crossAxisAlignment: CrossAxisAlignment.stretch,
                        children: [
                          _selector(
                            t,
                            projects
                                    .where((p) => p.$1 == targetProject)
                                    .map((p) => p.$2)
                                    .firstOrNull ??
                                targetProject,
                            () => _pickOption(
                              options: projects,
                              selected: targetProject,
                              onPick: (id) => setState(() => _projectId = id),
                            ),
                          ),
                          const SizedBox(height: 4),
                          Text(
                            i18n.t('cron:form.projectHint'),
                            style: TextStyle(
                              fontSize: FontSizes.xs2,
                              color: t.n500,
                            ),
                          ),
                        ],
                      ),
                    ),
                  _field(
                    t,
                    i18n.t('cron:form.schedule'),
                    Wrap(
                      spacing: 6,
                      children: [
                        for (final mode in scheduleModes)
                          ChoiceChip(
                            label: Text(
                              i18n.t(scheduleModeKeys[mode]!),
                              style: const TextStyle(fontSize: FontSizes.xs),
                            ),
                            selected: _form.mode == mode,
                            showCheckmark: false,
                            selectedColor: t.n300,
                            backgroundColor: t.card,
                            labelStyle: TextStyle(
                              color: _form.mode == mode ? t.ink : t.n700,
                            ),
                            side: BorderSide(
                              color: _form.mode == mode ? t.ink : t.hair,
                            ),
                            onSelected: (_) =>
                                _patch(_form.copyWith(mode: mode)),
                          ),
                      ],
                    ),
                  ),
                  if (_form.mode == 'daily' || _form.mode == 'weekly')
                    _field(
                      t,
                      i18n.t('cron:form.time'),
                      Row(
                        children: [
                          if (_form.mode == 'weekly') ...[
                            Expanded(
                              child: _selector(
                                t,
                                i18n.t(weekdayKeys[_form.weekday]!),
                                () => _pickOption(
                                  options: [
                                    for (final entry in weekdayKeys.entries)
                                      ('${entry.key}', i18n.t(entry.value)),
                                  ],
                                  selected: '${_form.weekday}',
                                  onPick: (v) => _patch(
                                    _form.copyWith(weekday: int.parse(v)),
                                  ),
                                ),
                              ),
                            ),
                            const SizedBox(width: 8),
                          ],
                          Expanded(child: _selector(t, _form.time, _pickTime)),
                          const SizedBox(width: 8),
                          Text(
                            i18n.t('cron:form.timezone', vars: {'tz': _tz}),
                            style: TextStyle(
                              fontSize: FontSizes.xs2,
                              color: t.n500,
                            ),
                          ),
                        ],
                      ),
                    ),
                  if (_form.mode == 'interval')
                    _field(
                      t,
                      i18n.t('cron:form.every'),
                      Row(
                        children: [
                          SizedBox(
                            width: 90,
                            child: TextField(
                              controller: _every,
                              keyboardType: TextInputType.number,
                              onChanged: (v) => _patch(
                                _form.copyWith(every: int.tryParse(v) ?? 0),
                              ),
                              decoration: _decoration(t, ''),
                              style: TextStyle(
                                fontSize: FontSizes.sm,
                                color: t.ink,
                              ),
                            ),
                          ),
                          const SizedBox(width: 8),
                          Expanded(
                            child: _selector(
                              t,
                              i18n.t(intervalUnitKeys[_form.unit]!),
                              () => _pickOption(
                                options: [
                                  for (final unit in intervalUnits)
                                    (unit, i18n.t(intervalUnitKeys[unit]!)),
                                ],
                                selected: _form.unit,
                                onPick: (v) => _patch(_form.copyWith(unit: v)),
                              ),
                            ),
                          ),
                          const SizedBox(width: 8),
                          Text(
                            i18n.t(
                              'cron:form.minInterval',
                              count: minIntervalMinutes,
                            ),
                            style: TextStyle(
                              fontSize: FontSizes.xs2,
                              color: t.n500,
                            ),
                          ),
                        ],
                      ),
                    ),
                  if (_form.mode == 'custom')
                    _field(
                      t,
                      i18n.t('cron:form.expr'),
                      Column(
                        crossAxisAlignment: CrossAxisAlignment.stretch,
                        children: [
                          TextField(
                            controller: _expr,
                            onChanged: (v) => _patch(_form.copyWith(expr: v)),
                            decoration: _decoration(t, '0 9 * * 1-5'),
                            style: TextStyle(
                              fontSize: FontSizes.sm,
                              color: t.ink,
                              fontFamily: 'Menlo',
                              fontFamilyFallback: const ['monospace'],
                            ),
                          ),
                          const SizedBox(height: 4),
                          Text(
                            i18n.t('cron:form.exprHint', vars: {'tz': _tz}),
                            style: TextStyle(
                              fontSize: FontSizes.xs2,
                              color: t.n500,
                            ),
                          ),
                        ],
                      ),
                    ),
                  if (_form.mode == 'at')
                    _field(
                      t,
                      i18n.t('cron:form.schedule'),
                      Text(
                        i18n.t(
                          'cron:describe.once',
                          vars: {'time': _form.at ?? ''},
                        ),
                        style: TextStyle(fontSize: FontSizes.sm, color: t.n700),
                      ),
                    ),
                  if (_failed)
                    Padding(
                      padding: const EdgeInsets.only(top: 4),
                      child: Text(
                        i18n.t('cron:form.saveFailed'),
                        style: TextStyle(
                          fontSize: FontSizes.xs,
                          color: t.danger,
                        ),
                      ),
                    ),
                ],
              ),
            ),
            Row(
              mainAxisAlignment: MainAxisAlignment.end,
              children: [
                TextButton(
                  onPressed: () => Navigator.pop(context),
                  child: Text(
                    i18n.t('cron:form.cancel'),
                    style: TextStyle(fontSize: FontSizes.sm, color: t.n700),
                  ),
                ),
                const SizedBox(width: 6),
                FilledButton(
                  onPressed: valid && !_saving ? _submit : null,
                  style: FilledButton.styleFrom(
                    backgroundColor: t.ink,
                    foregroundColor: t.bg,
                    disabledBackgroundColor: t.ink.withValues(alpha: 0.5),
                    disabledForegroundColor: t.bg,
                    shape: RoundedRectangleBorder(
                      borderRadius: BorderRadius.circular(Radii.full),
                    ),
                  ),
                  child: Text(
                    _saving
                        ? i18n.t('cron:form.saving')
                        : editing
                        ? i18n.t('cron:form.save')
                        : i18n.t('cron:form.create'),
                    style: const TextStyle(fontSize: FontSizes.sm),
                  ),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }

  Widget _field(BossipTokens t, String label, Widget child) => Padding(
    padding: const EdgeInsets.only(bottom: 14),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          label,
          style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
        ),
        const SizedBox(height: 6),
        child,
      ],
    ),
  );

  InputDecoration _decoration(BossipTokens t, String hint) => InputDecoration(
    hintText: hint.isEmpty ? null : hint,
    hintStyle: TextStyle(fontSize: FontSizes.sm, color: t.n500),
    isDense: true,
    counterText: '',
    filled: true,
    fillColor: t.card,
    contentPadding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
    enabledBorder: OutlineInputBorder(
      borderRadius: BorderRadius.circular(Radii.md),
      borderSide: BorderSide(color: t.hair),
    ),
    focusedBorder: OutlineInputBorder(
      borderRadius: BorderRadius.circular(Radii.md),
      borderSide: BorderSide(color: t.ink),
    ),
  );

  Widget _selector(BossipTokens t, String label, VoidCallback onTap) => InkWell(
    borderRadius: BorderRadius.circular(Radii.md),
    onTap: onTap,
    child: Container(
      padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
      decoration: BoxDecoration(
        border: Border.all(color: t.hair),
        borderRadius: BorderRadius.circular(Radii.md),
        color: t.card,
      ),
      child: Row(
        children: [
          Expanded(
            child: Text(
              label,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(fontSize: FontSizes.sm, color: t.ink),
            ),
          ),
          Icon(Icons.expand_more, size: 15, color: t.n500),
        ],
      ),
    ),
  );

  Future<void> _pickTime() async {
    final parts = _form.time.split(':');
    final picked = await showTimePicker(
      context: context,
      initialTime: TimeOfDay(
        hour: int.tryParse(parts.first) ?? 9,
        minute: int.tryParse(parts.last) ?? 0,
      ),
    );
    if (picked != null) {
      _patch(
        _form.copyWith(
          time:
              '${picked.hour.toString().padLeft(2, '0')}:${picked.minute.toString().padLeft(2, '0')}',
        ),
      );
    }
  }

  Future<void> _pickOption({
    required List<(String, String)> options,
    required String selected,
    required void Function(String) onPick,
  }) {
    final t = context.tokens;
    return showModalBottomSheet<void>(
      context: context,
      builder: (sheetContext) => SafeArea(
        child: ListView(
          shrinkWrap: true,
          children: [
            for (final (id, label) in options)
              ListTile(
                dense: true,
                title: Text(
                  label,
                  style: TextStyle(fontSize: FontSizes.base, color: t.ink),
                ),
                trailing: id == selected
                    ? Icon(Icons.check, size: 18, color: t.a700)
                    : null,
                onTap: () {
                  Navigator.pop(sheetContext);
                  onPick(id);
                },
              ),
          ],
        ),
      ),
    );
  }
}
