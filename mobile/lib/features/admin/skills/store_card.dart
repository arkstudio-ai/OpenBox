import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../models/admin_data.dart';
import '../widgets/admin_widgets.dart';

class AdminStoreCard extends ConsumerWidget {
  const AdminStoreCard({
    super.key,
    required this.row,
    required this.onAction,
    this.selected = false,
    this.disabled = false,
    this.onSelect,
  });
  final AdminRecord row;
  final ValueChanged<String> onAction;
  final ValueChanged<bool>? onSelect;
  final bool selected, disabled;

  void _details(BuildContext context, I18nState i) {
    showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      useSafeArea: true,
      builder: (sheetContext) => AdminSheet(
        content: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Row(
              children: [
                AdminIcon(row.string('icon')),
                const SizedBox(width: 12),
                Expanded(
                  child: Text(
                    row.string('title', row.string('name')),
                    style: const TextStyle(
                      fontSize: FontSizes.xl,
                      fontWeight: FontWeight.w500,
                    ),
                  ),
                ),
                IconButton(
                  tooltip: i.t('common:action.close'),
                  onPressed: () => Navigator.pop(sheetContext),
                  icon: const Icon(Icons.close, size: 20),
                ),
              ],
            ),
            const SizedBox(height: 12),
            if (row.string('description').isNotEmpty)
              SelectableText(row.string('description')),
            const SizedBox(height: 12),
            AdminField(
              i.t('admin:mobile.fieldTarget'),
              row.string('catalog_id'),
            ),
            AdminField(
              i.t('admin-skills:store.column.author'),
              row.record('author').string('username', row.string('publisher')),
            ),
            AdminField(
              i.t('admin-skills:store.column.version'),
              row.string('version'),
            ),
            AdminField(
              i.t('admin-skills:store.column.publishedAt'),
              adminDate(row.string('published_at'), i.language),
            ),
            if (row.string('listing_note').isNotEmpty)
              AdminField(
                i.t('admin-skills:dialog.note'),
                row.string('listing_note'),
              ),
          ],
        ),
        footer: AdminActionBar(
          primary: FilledButton(
            onPressed: disabled
                ? null
                : () {
                    Navigator.pop(sheetContext);
                    onAction(row.flag('deleted') ? 'restore' : 'edit');
                  },
            child: Text(
              i.t(
                row.flag('deleted')
                    ? 'admin-skills:manage.restore'
                    : 'admin-skills:manage.edit',
              ),
            ),
          ),
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final i = ref.watch(i18nProvider);
    final t = context.tokens;
    final listed = row.string('listing') == 'listed';
    final community = row.string('catalog_id').startsWith('community:');
    final actions = row.flag('deleted')
        ? ['restore']
        : [
            'edit',
            listed ? 'delist' : 'list',
            if (listed) row.flag('featured') ? 'unfeature' : 'feature',
            if (community)
              row.flag('is_official') ? 'unmarkOfficial' : 'markOfficial',
            if (community) 'view',
            'delete',
          ];
    final menu = PopupMenuButton<String>(
      enabled: !disabled,
      tooltip: i.t('admin:mobile.operations'),
      onSelected: onAction,
      itemBuilder: (_) => [
        for (final action in actions)
          PopupMenuItem(
            value: action,
            child: Text(
              i.t(
                {'edit', 'delete', 'restore'}.contains(action)
                    ? 'admin-skills:manage.$action'
                    : 'admin-skills:action.$action',
              ),
              style: action == 'delete' ? TextStyle(color: t.danger) : null,
            ),
          ),
      ],
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 14),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            Text(
              i.t('admin:mobile.manageEntry'),
              style: const TextStyle(fontSize: FontSizes.sm),
            ),
            const Icon(Icons.expand_more, size: 18),
          ],
        ),
      ),
    );
    return AdminRecordTile(
      title: row.string('title', row.string('name')),
      leading: AdminIcon(row.string('icon')),
      trailing: onSelect == null
          ? menu
          : Semantics(
              label: i.t(
                'admin-skills:manage.select',
                vars: {'title': row.string('title')},
              ),
              child: Checkbox(
                value: selected,
                onChanged: disabled ? null : (value) => onSelect!(value!),
              ),
            ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Wrap(
            spacing: 8,
            runSpacing: 6,
            crossAxisAlignment: WrapCrossAlignment.center,
            children: [
              AdminPill(
                row.flag('deleted')
                    ? i.t('admin-skills:manage.deleted')
                    : adminLabel(
                        i,
                        'admin-skills:status',
                        row.string('listing'),
                      ),
                status: row.flag('deleted') ? 'deleted' : row.string('listing'),
              ),
              Text(
                [
                  adminLabel(
                    i,
                    'admin-skills:store.origin',
                    row.string('origin'),
                  ),
                  row.string('kind').toUpperCase(),
                  if (row.flag('featured')) i.t('admin-skills:store.featured'),
                ].join(' · '),
                style: TextStyle(color: t.n600, fontSize: FontSizes.xs),
              ),
            ],
          ),
          if (row.string('description').isNotEmpty)
            Padding(
              padding: const EdgeInsets.only(top: 8),
              child: Text(
                row.string('description'),
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
                style: TextStyle(
                  color: t.n600,
                  fontSize: FontSizes.sm,
                  height: 1.5,
                ),
              ),
            ),
          const SizedBox(height: 4),
          Wrap(
            spacing: 4,
            crossAxisAlignment: WrapCrossAlignment.center,
            children: [
              TextButton(
                onPressed: disabled ? null : () => _details(context, i),
                child: Text(i.t('admin:mobile.details')),
              ),
              TextButton.icon(
                onPressed: disabled ? null : () => onAction('installs'),
                icon: const Icon(Icons.download_done, size: 16),
                label: Text(
                  '${i.t('admin-skills:store.column.installs')} · ${row.integer('installs_count')}',
                ),
              ),
            ],
          ),
        ],
      ),
    );
  }
}
