import 'dart:math';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/json.dart';
import '../../../shared/models/team.dart';
import '../../../shared/utils/error_text.dart';
import '../../../shared/widgets/spinner.dart';
import '../api/teams_api.dart';
import '../state/team_providers.dart';
import 'team_bits.dart';

/// Keep a finished run (web `SaveTeamConfiguration`): the roster as a team
/// template, or one member as an Agent draft. A phone gets the same two
/// actions as a bottom sheet; the saved definition is then edited on the Web
/// (§13.6), so the sheet closes by pointing at the library.
class SaveConfigurationLink extends ConsumerWidget {
  const SaveConfigurationLink({
    super.key,
    required this.scope,
    required this.snapshot,
    required this.onOpenLibrary,
    this.member,
  });

  final TeamScope scope;
  final TeamSnapshot snapshot;

  /// Null saves the whole roster as a template; otherwise this one member.
  final Map<String, dynamic>? member;

  /// Where the saved definition now lives: `agents` or `templates`.
  final void Function(String tab) onOpenLibrary;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final i18n = ref.watch(i18nProvider);
    return TeamActionLink(
      label: i18n.t(member == null ? 'teams:saveTemplate' : 'teams:saveMember'),
      onTap: () => showModalBottomSheet<void>(
        context: context,
        isScrollControlled: true,
        backgroundColor: context.tokens.card,
        builder: (_) => _SaveSheet(
          scope: scope,
          snapshot: snapshot,
          member: member,
          onOpenLibrary: onOpenLibrary,
        ),
      ),
    );
  }
}

class _SaveSheet extends ConsumerStatefulWidget {
  const _SaveSheet({
    required this.scope,
    required this.snapshot,
    required this.member,
    required this.onOpenLibrary,
  });

  final TeamScope scope;
  final TeamSnapshot snapshot;
  final Map<String, dynamic>? member;
  final void Function(String tab) onOpenLibrary;

  @override
  ConsumerState<_SaveSheet> createState() => _SaveSheetState();
}

class _SaveSheetState extends ConsumerState<_SaveSheet> {
  late final List<Map<String, dynamic>> _workers = widget.snapshot.members
      .where((entry) => asString(entry['role']) == 'member')
      .toList();
  late final TextEditingController _name = TextEditingController(
    text: widget.member != null
        ? asString(widget.member!['name']) ?? ''
        : widget.snapshot.run.title.characters.take(80).toString(),
  );

  /// Which members the template keeps, and what a temporary one is called
  /// once it becomes a saved Agent.
  late final Set<String> _selected = {
    for (final worker in _workers) asString(worker['id']) ?? '',
  };
  late final Map<String, TextEditingController> _aliases = {
    for (final worker in _workers)
      if (asString(worker['definition_id']) == null)
        asString(worker['id']) ?? '': TextEditingController(
          text:
              '${asString(worker['name']) ?? ''} · '
              '${widget.snapshot.id.characters.takeLast(6)}',
        ),
  };

  // One key per distinct payload: a retried save after a lost response must
  // reach the same record, and an edited name must not.
  String? _key;
  String _keyed = '';
  bool _busy = false;
  Object? _error;
  String? _savedTab;

  @override
  void dispose() {
    _name.dispose();
    for (final controller in _aliases.values) {
      controller.dispose();
    }
    super.dispose();
  }

  bool get _valid =>
      _name.text.trim().isNotEmpty &&
      (widget.member != null ||
          (_selected.isNotEmpty &&
              _selected.every(
                (id) =>
                    !_aliases.containsKey(id) ||
                    _aliases[id]!.text.trim().isNotEmpty,
              )));

  Future<void> _save() async {
    if (_busy || !_valid) return;
    final api = ref.read(teamsApiProvider);
    final name = _name.text.trim();
    final names = {
      for (final id in _selected)
        if (_aliases.containsKey(id)) id: _aliases[id]!.text.trim(),
    };
    final payload = '$name|${_selected.join(',')}|$names';
    if (_keyed != payload) {
      _keyed = payload;
      _key =
          'mobile-${DateTime.now().microsecondsSinceEpoch}-'
          '${Random.secure().nextInt(1 << 32)}';
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final member = widget.member;
      if (member == null) {
        await api.saveTemplate(
          widget.scope,
          widget.snapshot.id,
          _key!,
          name: name,
          memberIds: _selected.toList(),
          memberNames: names,
        );
      } else {
        await api.saveMember(
          widget.scope,
          widget.snapshot.id,
          asString(member['id']) ?? '',
          _key!,
          name: name,
        );
      }
      ref.invalidate(teamCatalogProvider);
      ref.invalidate(teamTemplatesProvider);
      if (mounted) {
        setState(() => _savedTab = member == null ? 'templates' : 'agents');
      }
    } catch (error) {
      if (mounted) setState(() => _error = error);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final member = widget.member;
    final title = i18n.t(
      member == null ? 'teams:saveTemplate' : 'teams:saveMember',
    );

    return SafeArea(
      child: Padding(
        padding: EdgeInsets.only(
          bottom: MediaQuery.viewInsetsOf(context).bottom,
        ),
        child: ConstrainedBox(
          constraints: BoxConstraints(
            maxHeight: MediaQuery.sizeOf(context).height * 0.8,
          ),
          child: ListView(
            shrinkWrap: true,
            padding: const EdgeInsets.fromLTRB(20, 16, 20, 16),
            children: [
              Text(
                title,
                style: TextStyle(
                  fontSize: FontSizes.lg,
                  fontWeight: FontWeight.w500,
                  color: t.ink,
                ),
              ),
              const SizedBox(height: 6),
              Text(
                i18n.t(
                  member == null
                      ? 'teams:saveTemplateHint'
                      : 'teams:saveMemberHint',
                ),
                style: TextStyle(
                  fontSize: FontSizes.sm,
                  color: t.n600,
                  height: 1.6,
                ),
              ),
              if (_savedTab != null) ...[
                const SizedBox(height: 16),
                Align(
                  alignment: AlignmentDirectional.centerStart,
                  child: TeamActionLink(
                    label: i18n.t('teams:openSavedConfiguration'),
                    onTap: () {
                      Navigator.pop(context);
                      widget.onOpenLibrary(_savedTab!);
                    },
                  ),
                ),
              ] else ...[
                const SizedBox(height: 16),
                _Field(label: i18n.t('teams:name'), controller: _name),
                if (member == null)
                  for (final worker in _workers)
                    _WorkerRow(
                      worker: worker,
                      selected: _selected.contains(asString(worker['id'])),
                      alias: _aliases[asString(worker['id'])],
                      onToggle: (on) => setState(() {
                        final id = asString(worker['id']) ?? '';
                        on ? _selected.add(id) : _selected.remove(id);
                      }),
                    ),
                if (_error != null)
                  Padding(
                    padding: const EdgeInsets.only(top: 10),
                    child: Text(
                      errorText(i18n, _error!),
                      style: TextStyle(fontSize: FontSizes.xs, color: t.danger),
                    ),
                  ),
              ],
              const SizedBox(height: 18),
              Row(
                mainAxisAlignment: MainAxisAlignment.end,
                children: [
                  TeamActionLink(
                    label: i18n.t(
                      _savedTab != null ? 'teams:close' : 'teams:cancel',
                    ),
                    muted: true,
                    onTap: () => Navigator.pop(context),
                  ),
                  if (_savedTab == null) ...[
                    const SizedBox(width: 16),
                    FilledButton(
                      onPressed: _busy || !_valid ? null : _save,
                      style: FilledButton.styleFrom(
                        backgroundColor: t.ink,
                        foregroundColor: t.bg,
                        minimumSize: const Size(0, 36),
                        padding: const EdgeInsets.symmetric(horizontal: 18),
                        shape: RoundedRectangleBorder(
                          borderRadius: BorderRadius.circular(Radii.full),
                        ),
                      ),
                      child: _busy
                          ? const Spinner(size: 14)
                          : Text(
                              i18n.t(
                                member == null
                                    ? 'teams:saveAndEnable'
                                    : 'teams:saveDraft',
                              ),
                              style: const TextStyle(fontSize: FontSizes.sm),
                            ),
                    ),
                  ],
                ],
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _Field extends StatelessWidget {
  const _Field({required this.label, required this.controller, this.maxLength});

  final String label;
  final TextEditingController controller;
  final int? maxLength;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Padding(
      padding: const EdgeInsets.only(bottom: 12),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            label,
            style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
          ),
          const SizedBox(height: 4),
          TextField(
            controller: controller,
            maxLength: maxLength,
            style: TextStyle(fontSize: FontSizes.base, color: t.ink),
            decoration: InputDecoration(
              isDense: true,
              counterText: '',
              contentPadding: const EdgeInsets.symmetric(
                horizontal: 10,
                vertical: 10,
              ),
              enabledBorder: OutlineInputBorder(
                borderRadius: BorderRadius.circular(Radii.md),
                borderSide: BorderSide(color: t.hair),
              ),
              focusedBorder: OutlineInputBorder(
                borderRadius: BorderRadius.circular(Radii.md),
                borderSide: BorderSide(color: t.ink),
              ),
            ),
          ),
        ],
      ),
    );
  }
}

/// One member the template may keep. A temporary member also needs the name
/// it will carry in the Agent library.
class _WorkerRow extends ConsumerWidget {
  const _WorkerRow({
    required this.worker,
    required this.selected,
    required this.alias,
    required this.onToggle,
  });

  final Map<String, dynamic> worker;
  final bool selected;
  final TextEditingController? alias;
  final ValueChanged<bool> onToggle;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return Container(
      margin: const EdgeInsets.only(bottom: 8),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        borderRadius: BorderRadius.circular(Radii.md),
        border: Border.all(color: t.hair),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          InkWell(
            onTap: () => onToggle(!selected),
            child: Row(
              children: [
                TeamCheckMark(value: selected),
                const SizedBox(width: 10),
                Expanded(
                  child: Text(
                    asString(worker['name']) ?? asString(worker['alias']) ?? '',
                    style: TextStyle(fontSize: FontSizes.base, color: t.ink),
                  ),
                ),
              ],
            ),
          ),
          if (selected && alias != null)
            Padding(
              padding: const EdgeInsets.only(top: 10),
              child: _Field(
                label: i18n.t('teams:savedMemberName'),
                controller: alias!,
                maxLength: 40,
              ),
            ),
          Padding(
            padding: const EdgeInsets.only(top: 8),
            child: Text(
              [
                asString(worker['model']) ?? '',
                asString(worker['responsibility']) ??
                    asString(worker['description']) ??
                    '',
              ].where((part) => part.isNotEmpty).join(' · '),
              style: TextStyle(
                fontSize: FontSizes.xs,
                color: t.n600,
                height: 1.6,
              ),
            ),
          ),
        ],
      ),
    );
  }
}
