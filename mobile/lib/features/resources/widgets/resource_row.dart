import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/resource.dart';
import '../../../shared/utils/format.dart';
import '../utils/resource_display.dart';

/// One row in the listing (web `ResourceRow`), re-flowed for a phone: the
/// hover actions become a long-press sheet, and the meta line carries what
/// the web keeps in a third column.
class ResourceRow extends ConsumerWidget {
  const ResourceRow({
    super.key,
    required this.resource,
    required this.selecting,
    required this.checked,
    required this.onTap,
    required this.onLongPress,
    required this.onToggle,
  });

  final Resource resource;
  final bool selecting;
  final bool checked;
  final VoidCallback onTap;
  final VoidCallback onLongPress;
  final ValueChanged<bool> onToggle;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);

    return InkWell(
      onTap: selecting ? () => onToggle(!checked) : onTap,
      onLongPress: onLongPress,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(14, 9, 10, 9),
        child: Row(
          children: [
            if (selecting)
              Padding(
                padding: const EdgeInsets.only(right: 4),
                child: SizedBox(
                  width: 24,
                  height: 24,
                  child: Checkbox(
                    value: checked,
                    visualDensity: VisualDensity.compact,
                    materialTapTargetSize: MaterialTapTargetSize.shrinkWrap,
                    onChanged: (value) => onToggle(value ?? false),
                  ),
                ),
              ),
            _Thumb(resource: resource),
            const SizedBox(width: 10),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    resource.name,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(fontSize: FontSizes.sm, color: t.ink),
                  ),
                  const SizedBox(height: 2),
                  Text(
                    _meta(i18n),
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(fontSize: FontSizes.xs2, color: t.n600),
                  ),
                ],
              ),
            ),
            if (resource.isAgent)
              Padding(
                padding: const EdgeInsets.only(left: 8),
                child: Icon(Icons.smart_toy_outlined, size: 15, color: t.n500),
              ),
          ],
        ),
      ),
    );
  }

  String _meta(I18nState i18n) {
    final parts = <String>[formatBytes(resource.size)];
    final created = resource.createdAt;
    if (created != null) {
      parts.add(formatRelative(created.toLocal(), i18n.language));
    }
    parts.add(i18n.t(sourceLabelKey(resource.source)));
    return parts.join(' · ');
  }
}

/// Image resources show their own thumbnail; everything else gets its kind
/// icon on a tinted square, the same split the web list makes.
class _Thumb extends StatelessWidget {
  const _Thumb({required this.resource});

  final Resource resource;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return ClipRRect(
      borderRadius: BorderRadius.circular(Radii.md),
      child: Container(
        width: 34,
        height: 34,
        color: t.n200,
        child: resource.kind == 'image' && resource.url.isNotEmpty
            ? Image.network(
                resource.url,
                fit: BoxFit.cover,
                errorBuilder: (_, _, _) => Icon(
                  kindIcon(resource.kind),
                  size: 17,
                  color: t.n600,
                ),
              )
            : Icon(kindIcon(resource.kind), size: 17, color: t.n600),
      ),
    );
  }
}
