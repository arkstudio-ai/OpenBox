import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/team.dart';
import '../../../shared/utils/error_text.dart';
import '../../../shared/widgets/composer_pill.dart';
import '../state/team_providers.dart';
import 'team_bits.dart';

/// Team picker (web `TeamPicker`): the pill that sits beside the chat model
/// once "团队" is the mode, and the sheet behind it. Mobile uses the same
/// bottom-sheet gesture as the model and mode pickers rather than a popup
/// menu — one selection language for the whole composer.
class TeamPicker extends ConsumerWidget {
  const TeamPicker({
    super.key,
    required this.scope,
    required this.value,
    required this.onChanged,
    this.disabled = false,
  });

  final TeamScope scope;
  final TeamRequest value;
  final ValueChanged<TeamRequest> onChanged;
  final bool disabled;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final i18n = ref.watch(i18nProvider);
    final templates =
        ref.watch(teamTemplatesProvider(scope)).valueOrNull ??
        const <TeamDefinition>[];
    final selected = templates
        .where((template) => template.id == value.templateId)
        .firstOrNull;
    return ComposerPill(
      label: selected?.name ?? i18n.t('teams:autoTeam'),
      icon: Icons.groups_outlined,
      maxWidth: 96,
      onTap: disabled
          ? null
          : () => showTeamPicker(
              context,
              scope: scope,
              value: value,
              onChanged: onChanged,
            ),
    );
  }
}

Future<void> showTeamPicker(
  BuildContext context, {
  required TeamScope scope,
  required TeamRequest value,
  required ValueChanged<TeamRequest> onChanged,
}) {
  return showModalBottomSheet<void>(
    context: context,
    isScrollControlled: true,
    backgroundColor: context.tokens.card,
    builder: (sheetContext) => SafeArea(
      child: _TeamPickerSheet(scope: scope, value: value, onChanged: onChanged),
    ),
  );
}

class _TeamPickerSheet extends ConsumerStatefulWidget {
  const _TeamPickerSheet({
    required this.scope,
    required this.value,
    required this.onChanged,
  });

  final TeamScope scope;
  final TeamRequest value;
  final ValueChanged<TeamRequest> onChanged;

  @override
  ConsumerState<_TeamPickerSheet> createState() => _TeamPickerSheetState();
}

class _TeamPickerSheetState extends ConsumerState<_TeamPickerSheet> {
  late TeamRequest _value = widget.value;

  void _apply(TeamRequest next) {
    setState(() => _value = next);
    widget.onChanged(next);
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final loaded = ref.watch(teamTemplatesProvider(widget.scope));
    final templates = loaded.valueOrNull ?? const <TeamDefinition>[];
    final selected = templates
        .where((template) => template.id == _value.templateId)
        .firstOrNull;
    // Auto teams supplement by definition; a saved roster follows its own
    // setting until this run overrides it.
    final allow = _value.allowSupplement ?? selected?.allowSupplement ?? true;

    return ConstrainedBox(
      constraints: BoxConstraints(
        maxHeight: MediaQuery.sizeOf(context).height * 0.72,
      ),
      child: ListView(
        shrinkWrap: true,
        padding: const EdgeInsets.symmetric(vertical: 8),
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(20, 8, 20, 8),
            child: Text(
              i18n.t('teams:chooseTeam'),
              style: TextStyle(
                fontSize: FontSizes.sm,
                fontWeight: FontWeight.w600,
                color: t.n600,
              ),
            ),
          ),
          _OptionRow(
            title: i18n.t('teams:autoTeam'),
            subtitle: i18n.t('teams:autoTeamHint'),
            selected: _value.templateId == null,
            onTap: () {
              _apply(const TeamRequest(allowSupplement: true));
              Navigator.pop(context);
            },
          ),
          if (loaded.isLoading)
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 20, vertical: 12),
              child: LinearProgressIndicator(
                color: t.accent,
                backgroundColor: t.hair,
              ),
            ),
          if (loaded.hasError)
            Padding(
              padding: const EdgeInsets.fromLTRB(20, 4, 20, 4),
              child: Row(
                children: [
                  Expanded(
                    child: Text(
                      errorText(i18n, loaded.error!),
                      style: TextStyle(fontSize: FontSizes.xs, color: t.danger),
                    ),
                  ),
                  TextButton(
                    onPressed: () =>
                        ref.invalidate(teamTemplatesProvider(widget.scope)),
                    child: Text(
                      i18n.t('common:action.retry'),
                      style: TextStyle(fontSize: FontSizes.xs, color: t.a700),
                    ),
                  ),
                ],
              ),
            ),
          for (final template in templates)
            _OptionRow(
              title: template.name,
              subtitle: template.description,
              selected: template.id == _value.templateId,
              onTap: () {
                // Clearing the override lets the template's own setting show
                // through, exactly as picking it on the web does.
                _apply(TeamRequest(templateId: template.id));
                Navigator.pop(context);
              },
            ),
          Padding(
            padding: const EdgeInsets.fromLTRB(20, 8, 20, 0),
            child: Divider(color: t.hair, height: 1),
          ),
          _CheckRow(
            label: i18n.t('teams:allowSupplement'),
            hint: i18n.t('teams:oneRunOnly'),
            value: allow,
            // With no roster there is nothing to supplement: the coordinator
            // already decides the whole lineup.
            onChanged: _value.templateId == null
                ? null
                : (next) => _apply(
                    TeamRequest(
                      templateId: _value.templateId,
                      allowSupplement: next,
                    ),
                  ),
          ),
          Padding(
            padding: const EdgeInsets.fromLTRB(20, 4, 20, 12),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  i18n.t('teams:manageTeams'),
                  style: TextStyle(fontSize: FontSizes.sm, color: t.n700),
                ),
                const SizedBox(height: 2),
                Text(
                  i18n.t('teams:manageOnWeb'),
                  style: TextStyle(
                    fontSize: FontSizes.xs,
                    color: t.n600,
                    height: 1.5,
                  ),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class _OptionRow extends StatelessWidget {
  const _OptionRow({
    required this.title,
    required this.subtitle,
    required this.selected,
    required this.onTap,
  });

  final String title, subtitle;
  final bool selected;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return ListTile(
      dense: true,
      title: Text(
        title,
        style: TextStyle(fontSize: FontSizes.base, color: t.ink),
      ),
      subtitle: subtitle.isEmpty
          ? null
          : Text(
              subtitle,
              maxLines: 2,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(fontSize: FontSizes.xs, color: t.n500),
            ),
      trailing: selected ? Icon(Icons.check, size: 18, color: t.a700) : null,
      onTap: onTap,
    );
  }
}

/// The same round tick the todo card and the Skill selector use, so a
/// selection reads the same way everywhere in the app.
class _CheckRow extends StatelessWidget {
  const _CheckRow({
    required this.label,
    required this.hint,
    required this.value,
    required this.onChanged,
  });

  final String label, hint;
  final bool value;
  final ValueChanged<bool>? onChanged;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Opacity(
      opacity: onChanged == null ? 0.5 : 1,
      child: InkWell(
        onTap: onChanged == null ? null : () => onChanged!(!value),
        child: Padding(
          padding: const EdgeInsets.fromLTRB(20, 12, 20, 12),
          child: Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              TeamCheckMark(value: value),
              const SizedBox(width: 11),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      label,
                      style: TextStyle(fontSize: FontSizes.base, color: t.ink),
                    ),
                    const SizedBox(height: 2),
                    Text(
                      hint,
                      style: TextStyle(
                        fontSize: FontSizes.xs,
                        color: t.n600,
                        height: 1.5,
                      ),
                    ),
                  ],
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
