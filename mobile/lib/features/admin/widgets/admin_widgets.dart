import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/widgets/section_tabs.dart';

export 'admin_layout.dart';

class AdminCard extends StatelessWidget {
  const AdminCard({
    super.key,
    this.title,
    this.subtitle,
    required this.child,
    this.onTap,
  });
  final String? title;
  final String? subtitle;
  final Widget child;
  final VoidCallback? onTap;
  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Padding(
      padding: const EdgeInsets.only(bottom: 12),
      child: Material(
        color: t.card,
        shape: RoundedRectangleBorder(
          side: BorderSide(color: t.hair),
          borderRadius: BorderRadius.circular(Radii.lg),
        ),
        clipBehavior: Clip.antiAlias,
        child: InkWell(
          onTap: onTap,
          child: Padding(
            padding: const EdgeInsets.all(16),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                if (title != null)
                  Text(
                    title!,
                    style: TextStyle(
                      color: t.ink,
                      fontSize: FontSizes.lg,
                      fontWeight: FontWeight.w500,
                    ),
                  ),
                if (subtitle?.isNotEmpty ?? false)
                  Padding(
                    padding: const EdgeInsets.only(top: 4),
                    child: Text(
                      subtitle!,
                      style: TextStyle(color: t.n600, fontSize: FontSizes.sm),
                    ),
                  ),
                if (title != null || subtitle != null)
                  const SizedBox(height: 12),
                child,
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class AdminField extends StatelessWidget {
  const AdminField(this.label, this.value, {super.key});
  final String label;
  final String value;
  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.symmetric(vertical: 7),
    child: LayoutBuilder(
      builder: (context, constraints) {
        final caption = Text(
          label,
          style: TextStyle(color: context.tokens.n600, fontSize: FontSizes.sm),
        );
        final content = SelectableText(
          value.isEmpty ? '—' : value,
          style: TextStyle(color: context.tokens.ink, fontSize: FontSizes.md),
        );
        if (constraints.maxWidth < 240 ||
            MediaQuery.textScalerOf(context).scale(14) > 20) {
          return Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [caption, const SizedBox(height: 3), content],
          );
        }
        return Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            SizedBox(width: constraints.maxWidth * .34, child: caption),
            const SizedBox(width: 12),
            Expanded(child: content),
          ],
        );
      },
    ),
  );
}

class AdminPill extends StatelessWidget {
  const AdminPill(this.text, {super.key, this.status = ''});
  final String text;
  final String status;
  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final danger = {
      'critical',
      'down',
      'fail',
      'timeout',
      'rejected',
      'cancelled',
      'deleted',
    }.contains(status.toLowerCase());
    final good = {
      'listed',
      'active',
      'paid',
      'ok',
      'up',
      'running',
      'prewarm',
    }.contains(status.toLowerCase());
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
      decoration: BoxDecoration(
        color: danger
            ? t.dangerSoft
            : good
            ? t.a200
            : t.hairSoft,
        borderRadius: BorderRadius.circular(Radii.sm),
      ),
      child: Text(
        text,
        style: TextStyle(
          color: danger
              ? t.danger
              : good
              ? t.a700
              : t.n700,
          fontSize: FontSizes.xs,
        ),
      ),
    );
  }
}

class AdminList extends StatelessWidget {
  const AdminList({
    super.key,
    required this.children,
    this.onRefresh,
    this.storageKey,
  });
  final List<Widget> children;
  final Future<void> Function()? onRefresh;
  final String? storageKey;
  @override
  Widget build(BuildContext context) {
    final list = ListView(
      key: storageKey == null ? null : PageStorageKey(storageKey),
      keyboardDismissBehavior: ScrollViewKeyboardDismissBehavior.onDrag,
      physics: const AlwaysScrollableScrollPhysics(),
      padding: const EdgeInsets.fromLTRB(16, 12, 16, 28),
      children: children,
    );
    return SafeArea(
      top: false,
      child: onRefresh == null
          ? list
          : RefreshIndicator(onRefresh: onRefresh!, child: list),
    );
  }
}

class AdminTabs extends StatelessWidget {
  const AdminTabs({
    super.key,
    required this.labels,
    required this.value,
    required this.onChanged,
  });
  final Map<String, String> labels;
  final String value;
  final ValueChanged<String>? onChanged;
  @override
  Widget build(BuildContext context) =>
      SectionTabs(labels: labels, value: value, onChanged: onChanged);
}

class AdminPager extends ConsumerWidget {
  const AdminPager({
    super.key,
    required this.total,
    required this.offset,
    required this.limit,
    required this.onChanged,
    this.disabled = false,
  });
  final int total, offset, limit;
  final ValueChanged<int> onChanged;
  final bool disabled;
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final i = ref.watch(i18nProvider);
    if (total == 0) return const SizedBox.shrink();
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 8),
      child: Column(
        children: [
          Text(
            i.t(
              'admin-skills:list.range',
              vars: {
                'from': offset + 1,
                'to': (offset + limit).clamp(0, total),
                'total': total,
              },
            ),
            style: TextStyle(color: context.tokens.n600, fontSize: 12),
          ),
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceBetween,
            children: [
              TextButton.icon(
                onPressed: disabled || offset <= 0
                    ? null
                    : () => onChanged((offset - limit).clamp(0, total)),
                icon: const Icon(Icons.chevron_left),
                label: Text(i.t('admin-skills:list.previous')),
              ),
              TextButton.icon(
                onPressed: disabled || offset + limit >= total
                    ? null
                    : () => onChanged(offset + limit),
                icon: const Icon(Icons.chevron_right),
                label: Text(i.t('admin-skills:list.next')),
              ),
            ],
          ),
        ],
      ),
    );
  }
}

class AdminIcon extends StatelessWidget {
  const AdminIcon(this.value, {super.key});
  final String value;
  @override
  Widget build(BuildContext context) {
    final uri = Uri.tryParse(value.trim());
    final image = uri?.scheme == 'https' && (uri?.host.isNotEmpty ?? false);
    const fallback = Center(child: Text('🧩', style: TextStyle(fontSize: 22)));
    return SizedBox.square(
      dimension: 42,
      child: ClipRRect(
        borderRadius: BorderRadius.circular(12),
        child: image
            ? Image.network(
                value.trim(),
                fit: BoxFit.contain,
                excludeFromSemantics: true,
                errorBuilder: (_, _, _) => fallback,
              )
            : value.isEmpty
            ? fallback
            : Center(
                child: Text(
                  value,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: const TextStyle(fontSize: 22),
                ),
              ),
      ),
    );
  }
}

String adminLabel(I18nState i18n, String prefix, String value) {
  if (value.isEmpty) return '—';
  final key = '$prefix.$value';
  final text = i18n.t(key);
  return text == key ? value : text;
}
