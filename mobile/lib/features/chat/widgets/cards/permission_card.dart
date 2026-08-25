import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../../shared/appearance/tokens.dart';
import '../../../../shared/appearance/type_scale.dart';
import '../../../../shared/i18n/i18n.dart';
import '../../../../shared/models/interaction.dart';
import '../../api/chat_api.dart';
import '../../utils/tool_map.dart';

/// Tool-permission prompt (web `PermissionCard`). Actions use the
/// backend-native values `once` / `always` / `reject`.
class PermissionCard extends ConsumerWidget {
  const PermissionCard({super.key, required this.request});

  final PermissionRequest request;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final api = ref.read(chatApiProvider);
    final subject = request.title ??
        '${i18n.t('chat:kind.${toolKindKey(request.tool)}')} · ${request.tool}';
    return Container(
      margin: const EdgeInsets.symmetric(vertical: 8),
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: t.card,
        borderRadius: BorderRadius.circular(Radii.xl),
        border: Border.all(color: t.hair),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            i18n.t('chat:permission.title'),
            style: TextStyle(
              fontSize: FontSizes.sm,
              fontWeight: FontWeight.w600,
              color: t.n800,
            ),
          ),
          const SizedBox(height: 6),
          Text(
            i18n.t('chat:permission.body'),
            style: TextStyle(fontSize: FontSizes.sm, color: t.n600),
          ),
          const SizedBox(height: 6),
          Text(
            subject,
            style: TextStyle(
              fontSize: FontSizes.sm,
              color: t.ink,
              fontFamily: 'Menlo',
              fontFamilyFallback: const ['monospace'],
            ),
          ),
          const SizedBox(height: 12),
          Row(
            children: [
              FilledButton(
                onPressed: () => api.replyPermission(request.id, 'once'),
                style: FilledButton.styleFrom(
                  backgroundColor: t.ink,
                  foregroundColor: t.bg,
                  padding:
                      const EdgeInsets.symmetric(horizontal: 14, vertical: 6),
                  shape: RoundedRectangleBorder(
                    borderRadius: BorderRadius.circular(Radii.full),
                  ),
                ),
                child: Text(i18n.t('chat:permission.allow'),
                    style: const TextStyle(fontSize: FontSizes.sm)),
              ),
              const SizedBox(width: 8),
              OutlinedButton(
                onPressed: () => api.replyPermission(request.id, 'always'),
                style: OutlinedButton.styleFrom(
                  side: BorderSide(color: t.hair),
                  foregroundColor: t.ink,
                  padding:
                      const EdgeInsets.symmetric(horizontal: 14, vertical: 6),
                  shape: RoundedRectangleBorder(
                    borderRadius: BorderRadius.circular(Radii.full),
                  ),
                ),
                child: Text(i18n.t('chat:permission.allowAlways'),
                    style: const TextStyle(fontSize: FontSizes.sm)),
              ),
              const SizedBox(width: 8),
              TextButton(
                onPressed: () => api.replyPermission(request.id, 'reject'),
                child: Text(
                  i18n.t('chat:permission.deny'),
                  style: TextStyle(fontSize: FontSizes.sm, color: t.danger),
                ),
              ),
            ],
          ),
        ],
      ),
    );
  }
}
