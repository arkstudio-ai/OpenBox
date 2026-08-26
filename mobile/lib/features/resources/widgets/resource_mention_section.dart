import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/resource.dart';
import '../../../shared/utils/format.dart';
import '../api/resources_api.dart';
import '../utils/resource_display.dart';
import 'resource_sheets.dart';

/// The resource block of the composer's "@" menu (web `MentionScopeBar` +
/// the menu's resource section).
///
/// It opens on the conversation's own project — that is where the file
/// someone is about to reference almost always lives — and the switcher lets
/// them step out to another project or to everything. Picking a row does not
/// type anything: the file already lives in OSS, so the composer just pins it
/// as an attachment.
class ResourceMentionSection extends ConsumerStatefulWidget {
  const ResourceMentionSection({
    super.key,
    required this.query,
    required this.projectId,
    required this.onPick,
  });

  /// What was typed after the "@".
  final String query;

  /// The conversation's project, or null on a chat that does not exist yet.
  final String? projectId;

  final ValueChanged<Resource> onPick;

  @override
  ConsumerState<ResourceMentionSection> createState() =>
      _ResourceMentionSectionState();
}

/// Rows the menu shows before the query has to narrow things.
const _maxRows = 8;

class _ResourceMentionSectionState
    extends ConsumerState<ResourceMentionSection> {
  /// Set once someone picks another project; reset when the conversation
  /// changes, so the menu goes back to opening on its own project.
  String? _picked;
  String _source = 'all';

  String get _home => widget.projectId ?? allProjects;
  String get _project => _picked ?? _home;

  @override
  void didUpdateWidget(ResourceMentionSection old) {
    super.didUpdateWidget(old);
    if (old.projectId != widget.projectId) _picked = null;
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final page = ref.watch(resourceListProvider(
      ResourceQuery(project: _project, source: _source, limit: 200),
    ));
    final needle = widget.query.trim().toLowerCase();
    final items = [
      for (final resource in page.valueOrNull?.items ?? const <Resource>[])
        if (needle.isEmpty || resource.name.toLowerCase().contains(needle))
          resource,
    ].take(_maxRows).toList();

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        _ScopeBar(
          project: _project,
          source: _source,
          onProject: (value) => setState(() => _picked = value),
          onSource: (value) => setState(() => _source = value),
        ),
        Padding(
          padding: const EdgeInsets.fromLTRB(16, 6, 16, 2),
          child: Text(
            i18n.t('chat:composer.mention.resources'),
            style: TextStyle(
              fontSize: FontSizes.xs2,
              fontWeight: FontWeight.w600,
              letterSpacing: 0.4,
              color: t.n500,
            ),
          ),
        ),
        if (page.isLoading)
          _hint(t, i18n.t('chat:composer.mention.loading'))
        else if (items.isEmpty)
          _hint(t, i18n.t('chat:composer.mention.empty'))
        else
          for (final resource in items)
            InkWell(
              onTap: () => widget.onPick(resource),
              child: Padding(
                padding:
                    const EdgeInsets.symmetric(horizontal: 16, vertical: 6),
                child: Row(
                  children: [
                    _Thumb(resource: resource),
                    const SizedBox(width: 10),
                    Expanded(
                      child: Text(
                        resource.name,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style:
                            TextStyle(fontSize: FontSizes.sm, color: t.ink),
                      ),
                    ),
                    const SizedBox(width: 8),
                    Text(
                      formatBytes(resource.size),
                      style:
                          TextStyle(fontSize: FontSizes.xs2, color: t.n500),
                    ),
                  ],
                ),
              ),
            ),
      ],
    );
  }

  Widget _hint(BossipTokens t, String text) => Padding(
        padding: const EdgeInsets.fromLTRB(16, 4, 16, 8),
        child: Text(text,
            style: TextStyle(fontSize: FontSizes.sm, color: t.n500)),
      );
}

class _ScopeBar extends ConsumerWidget {
  const _ScopeBar({
    required this.project,
    required this.source,
    required this.onProject,
    required this.onSource,
  });

  final String project;
  final String source;
  final ValueChanged<String> onProject;
  final ValueChanged<String> onSource;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final projects = ref.watch(resourceProjectsProvider).valueOrNull ??
        const <(String, String)>[];
    final label = project == allProjects
        ? i18n.t('chat:composer.mention.allProjects')
        : projects
                .where((p) => p.$1 == project)
                .map((p) => p.$2)
                .firstOrNull ??
            i18n.t('chat:composer.mention.allProjects');

    return Container(
      decoration: BoxDecoration(
        border: Border(bottom: BorderSide(color: t.hair)),
      ),
      child: SingleChildScrollView(
        scrollDirection: Axis.horizontal,
        padding: const EdgeInsets.fromLTRB(12, 6, 12, 6),
        child: Row(
          children: [
            _Chip(
              label: label,
              active: true,
              caret: true,
              onTap: () async {
                final picked = await showProjectScopeSheet(context, ref,
                    active: project);
                // "Unfiled" is a listing scope, not a mention scope: the menu
                // offers this conversation's project or everything.
                if (picked != null) {
                  onProject(picked == noProject ? allProjects : picked);
                }
              },
            ),
            const SizedBox(width: 8),
            Container(width: 1, height: 12, color: t.hair),
            const SizedBox(width: 8),
            for (final value in sourceFilters) ...[
              _Chip(
                label: i18n.t(_shortSourceKey(value)),
                active: source == value,
                onTap: () => onSource(value),
              ),
              const SizedBox(width: 6),
            ],
          ],
        ),
      ),
    );
  }
}

/// The menu uses the short labels ("全部 / 我上传的 / 模型产出"), the same
/// copy the web scope bar carries.
String _shortSourceKey(String source) => switch (source) {
      'user' => 'chat:composer.mention.sourceUser',
      'agent' => 'chat:composer.mention.sourceAgent',
      _ => 'chat:composer.mention.sourceAll',
    };

class _Chip extends StatelessWidget {
  const _Chip({
    required this.label,
    required this.active,
    required this.onTap,
    this.caret = false,
  });

  final String label;
  final bool active;
  final bool caret;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Material(
      color: active ? t.n200 : Colors.transparent,
      borderRadius: BorderRadius.circular(Radii.full),
      child: InkWell(
        borderRadius: BorderRadius.circular(Radii.full),
        onTap: onTap,
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 5),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              ConstrainedBox(
                constraints: const BoxConstraints(maxWidth: 120),
                child: Text(
                  label,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(
                    fontSize: FontSizes.xs2,
                    color: active ? t.ink : t.n600,
                  ),
                ),
              ),
              if (caret) ...[
                const SizedBox(width: 2),
                Icon(Icons.expand_more, size: 12, color: t.n500),
              ],
            ],
          ),
        ),
      ),
    );
  }
}

class _Thumb extends StatelessWidget {
  const _Thumb({required this.resource});

  final Resource resource;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return ClipRRect(
      borderRadius: BorderRadius.circular(Radii.sm),
      child: Container(
        width: 22,
        height: 22,
        color: t.n200,
        child: resource.kind == 'image' && resource.url.isNotEmpty
            ? Image.network(
                resource.url,
                fit: BoxFit.cover,
                errorBuilder: (_, _, _) =>
                    Icon(kindIcon(resource.kind), size: 13, color: t.n600),
              )
            : Icon(kindIcon(resource.kind), size: 13, color: t.n600),
      ),
    );
  }
}
