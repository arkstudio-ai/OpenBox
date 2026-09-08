import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../utils/group_skills.dart';
import '../utils/listing.dart';
import 'sheet_scaffold.dart';

/// Starts a normal conversation; the agent, not this form, designs the skill
/// (web `CreateSkillDialog`).
class CreateSkillSheet extends ConsumerStatefulWidget {
  const CreateSkillSheet({
    super.key,
    required this.projects,
    required this.loading,
    required this.busy,
    required this.error,
    required this.onConfirm,
  });

  /// `(id, name)` pairs, in the order the workspace lists them.
  final List<(String, String)> projects;
  final bool loading;
  final bool busy;
  final String? error;
  final void Function(String projectId, String brief) onConfirm;

  @override
  ConsumerState<CreateSkillSheet> createState() => _CreateSkillSheetState();
}

class _CreateSkillSheetState extends ConsumerState<CreateSkillSheet> {
  final _brief = TextEditingController();
  String? _projectId;

  @override
  void dispose() {
    _brief.dispose();
    super.dispose();
  }

  String get _selectedProject =>
      _projectId ??
      (widget.projects.isEmpty ? '' : widget.projects.first.$1);

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final canSubmit = _selectedProject.isNotEmpty &&
        _brief.text.trim().isNotEmpty &&
        !widget.loading;

    return SkillSheet(
      title: i18n.t('skills:create.title'),
      subtitle: i18n.t('skills:create.subtitle'),
      busy: widget.busy,
      error: widget.error,
      canConfirm: canSubmit,
      confirmLabel: i18n.t(
        widget.busy ? 'skills:create.starting' : 'skills:create.confirm',
      ),
      onConfirm: () => widget.onConfirm(_selectedProject, _brief.text.trim()),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Padding(
            padding: const EdgeInsets.only(top: 12),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  i18n.t('skills:create.projectLabel'),
                  style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
                ),
                const SizedBox(height: 4),
                Container(
                  padding: const EdgeInsets.symmetric(horizontal: 10),
                  decoration: BoxDecoration(
                    border: Border.all(color: t.hair),
                    borderRadius: BorderRadius.circular(Radii.md),
                    color: t.bg,
                  ),
                  child: DropdownButtonHideUnderline(
                    child: DropdownButton<String>(
                      isExpanded: true,
                      value: _selectedProject.isEmpty ? null : _selectedProject,
                      hint: Text(
                        i18n.t('skills:create.noProjects'),
                        style:
                            TextStyle(fontSize: FontSizes.sm, color: t.n600),
                      ),
                      items: [
                        for (final (id, name) in widget.projects)
                          DropdownMenuItem(
                            value: id,
                            child: Text(
                              name,
                              maxLines: 1,
                              overflow: TextOverflow.ellipsis,
                              style: TextStyle(
                                  fontSize: FontSizes.sm, color: t.ink),
                            ),
                          ),
                      ],
                      onChanged: widget.busy || widget.projects.isEmpty
                          ? null
                          : (value) => setState(() => _projectId = value),
                    ),
                  ),
                ),
              ],
            ),
          ),
          SheetField(
            label: i18n.t('skills:create.briefLabel'),
            placeholder: i18n.t('skills:create.briefPlaceholder'),
            controller: _brief,
            lines: 4,
            prose: true,
            onChanged: (_) => setState(() {}),
          ),
          Padding(
            padding: const EdgeInsets.only(top: 6),
            child: Text(
              i18n.t('skills:create.chatHint'),
              style:
                  TextStyle(fontSize: FontSizes.xs, height: 1.6, color: t.n600),
            ),
          ),
        ],
      ),
    );
  }
}

/// Publishing is an explicit public action, so it always gets a confirmation
/// (web `PublishSkillDialog`).
///
/// What the confirmation promises depends on the deployment: with review on,
/// tapping submits to a queue and nothing is public yet; with it off, the tap
/// is the publication. Both sentences live in the locale and are chosen from
/// `skill_store_review` — a sheet that guesses makes the product lie about
/// whether strangers can already read what was just uploaded.
class PublishSkillSheet extends ConsumerWidget {
  const PublishSkillSheet({
    super.key,
    required this.group,
    required this.reviewRequired,
    required this.busy,
    required this.error,
    required this.onConfirm,
  });

  final SkillGroup group;

  /// Whether this deployment queues submissions for an admin to look at.
  final bool reviewRequired;
  final bool busy;
  final String? error;
  final VoidCallback onConfirm;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final chip = listingChipFor(group.publicationStatus, group.listing);
    final resubmit = isResubmission(chip);
    final updating = group.isPublished;
    final titleKey = resubmit
        ? 'skills:publish.resubmitTitle'
        : updating
            ? 'skills:publish.updateTitle'
            : 'skills:publish.title';
    final bodyKey = resubmit
        ? 'skills:publish.resubmitBody'
        : updating
            ? 'skills:publish.updateBody'
            : 'skills:publish.body';
    final confirmKey = reviewRequired
        ? 'skills:publish.confirmSubmit'
        : resubmit
            ? 'skills:publish.confirmResubmit'
            : updating
                ? 'skills:publish.confirmUpdate'
                : 'skills:publish.confirm';

    return SkillSheet(
      title: i18n.t(titleKey),
      header: Container(
        width: 40,
        height: 40,
        alignment: Alignment.center,
        decoration: BoxDecoration(
          color: t.a100,
          borderRadius: BorderRadius.circular(Radii.lg),
        ),
        child: Icon(Icons.cloud_upload_outlined, size: 19, color: t.a800),
      ),
      busy: busy,
      error: error,
      confirmLabel: i18n.t(
        busy
            ? (reviewRequired
                ? 'skills:publish.submitting'
                : 'skills:publish.working')
            : confirmKey,
      ),
      onConfirm: onConfirm,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Padding(
            padding: const EdgeInsets.only(top: 4),
            child: Text(
              i18n.t(bodyKey, vars: {'name': group.name}),
              style: TextStyle(
                fontSize: FontSizes.sm,
                height: 1.7,
                color: t.n700,
              ),
            ),
          ),
          Container(
            margin: const EdgeInsets.only(top: 12),
            padding: const EdgeInsets.symmetric(horizontal: 11, vertical: 8),
            decoration: BoxDecoration(
              color: t.hairSoft.withValues(alpha: 0.6),
              borderRadius: BorderRadius.circular(Radii.md),
            ),
            child: Text(
              i18n.t(reviewRequired
                  ? 'skills:publish.reviewNotice'
                  : 'skills:publish.publicNotice'),
              style: TextStyle(
                fontSize: FontSizes.xs,
                height: 1.6,
                color: t.n600,
              ),
            ),
          ),
          // Re-submitting after a refusal or a delisting: repeat what was said
          // about it, so the fix can be checked against the reason.
          if (resubmit &&
              (group.listingNote?.isNotEmpty ?? false))
            Container(
              margin: const EdgeInsets.only(top: 8),
              padding: const EdgeInsets.symmetric(horizontal: 11, vertical: 8),
              decoration: BoxDecoration(
                color: t.a100,
                borderRadius: BorderRadius.circular(Radii.md),
              ),
              child: Text(
                i18n.t('skills:mine.listingReason',
                    vars: {'note': group.listingNote!}),
                style: TextStyle(
                  fontSize: FontSizes.xs,
                  height: 1.6,
                  color: t.n700,
                ),
              ),
            ),
        ],
      ),
    );
  }
}

/// Withdrawing is the author's own undo: it takes the release off the shelf
/// without destroying it, and without touching the copies people installed.
class WithdrawSkillSheet extends ConsumerWidget {
  const WithdrawSkillSheet({
    super.key,
    required this.group,
    required this.busy,
    required this.error,
    required this.onConfirm,
  });

  final SkillGroup group;
  final bool busy;
  final String? error;
  final VoidCallback onConfirm;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);

    return SkillSheet(
      title: i18n.t('skills:withdraw.title'),
      header: Container(
        width: 40,
        height: 40,
        alignment: Alignment.center,
        decoration: BoxDecoration(
          color: t.a100,
          borderRadius: BorderRadius.circular(Radii.lg),
        ),
        child: Icon(Icons.cloud_off_outlined, size: 19, color: t.a800),
      ),
      busy: busy,
      error: error,
      confirmLabel: i18n.t(
        busy ? 'skills:withdraw.working' : 'skills:withdraw.confirm',
      ),
      onConfirm: onConfirm,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Padding(
            padding: const EdgeInsets.only(top: 4),
            child: Text(
              i18n.t('skills:withdraw.body', vars: {'name': group.name}),
              style: TextStyle(
                fontSize: FontSizes.sm,
                height: 1.7,
                color: t.n700,
              ),
            ),
          ),
          Container(
            margin: const EdgeInsets.only(top: 12),
            padding: const EdgeInsets.symmetric(horizontal: 11, vertical: 8),
            decoration: BoxDecoration(
              color: t.hairSoft.withValues(alpha: 0.6),
              borderRadius: BorderRadius.circular(Radii.md),
            ),
            child: Text(
              i18n.t('skills:withdraw.notice'),
              style: TextStyle(
                fontSize: FontSizes.xs,
                height: 1.6,
                color: t.n600,
              ),
            ),
          ),
        ],
      ),
    );
  }
}
