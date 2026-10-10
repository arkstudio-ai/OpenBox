import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../../shared/api/api_error.dart';
import '../../../../shared/api/providers.dart';
import '../../../../shared/appearance/tokens.dart';
import '../../../../shared/appearance/type_scale.dart';
import '../../../../shared/events/bus.dart';
import '../../../../shared/i18n/i18n.dart';
import '../../../../shared/models/interaction.dart';
import '../../../../shared/utils/error_text.dart';
import '../../../../shared/widgets/toast.dart';
import '../../../onboarding/state/onboarding_store.dart';
import '../../../onboarding/widgets/first_seen_hint.dart';
import '../../api/assistant_reply.dart';
import '../../api/chat_api.dart';
import '../../state/pending_store.dart';
import '../../utils/tool_map.dart';

/// Tool-permission prompt (web `PermissionCard`). Actions use the
/// backend-native values `once` / `always` / `reject`.
class PermissionCard extends ConsumerStatefulWidget {
  const PermissionCard({super.key, required this.request});

  final PermissionRequest request;

  @override
  ConsumerState<PermissionCard> createState() => _PermissionCardState();
}

class _PermissionCardState extends ConsumerState<PermissionCard> {
  bool _submitting = false;
  PermissionRequest get request => widget.request;

  Future<void> _reply(String action) async {
    if (_submitting) return;
    setState(() => _submitting = true);
    final request = widget.request;
    final auth = ref.read(authSessionProvider);
    final workspace = ref.read(workspaceScopeProvider);
    final userId = auth.userId;
    final workspaceId = workspace.currentId;
    bool current() =>
        auth.userId == userId && workspace.currentId == workspaceId;
    final api = ref.read(chatApiProvider);
    final service = request.assistant == null
        ? null
        : ref.read(assistantReplyProvider);
    final pending = ref.read(pendingProvider.notifier);
    final toast = ref.read(toastProvider.notifier);
    final i18n = ref.read(i18nProvider);
    final events = ref.read(appEventBusProvider);
    try {
      if (service == null) {
        await api.replyPermission(request.id, action);
      } else {
        final receipt = await service.reply(
          kind: 'permission',
          id: request.id,
          binding: request.assistant!,
          answer: {'action': action},
        );
        if (!current()) return;
        toast.info(i18n.t(replyStateKey(receipt['state'])));
      }
      if (!current()) return;
      pending.removePermission(request.id);
      events.emit('assistant.request.changed', {'requestId': request.id});
      await pending.refreshAll();
    } catch (error) {
      if (!current()) return;
      toast.error(
        apiErrorOf(error)?.code == 'ASSISTANT_SEND_UNCERTAIN'
            ? i18n.t('chat:assistant.requests.replyUncertain')
            : errorText(i18n, error),
      );
    } finally {
      if (mounted) setState(() => _submitting = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final subject =
        request.title ??
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
          FirstSeenHint(
            guide: Guides.cardPermission,
            title: i18n.t('onboarding:m2.cards.permission.title'),
            body: i18n.t('onboarding:m2.cards.permission.body'),
            compact: true,
          ),
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
          if (request.assistant != null) ...[
            Text(
              i18n.t(
                'chat:permission.alwaysScope',
                vars: {
                  'tool': request.tool,
                  'patterns':
                      (request.always.isNotEmpty
                              ? request.always
                              : request.patterns)
                          .join(', '),
                },
              ),
            ),
            Material(
              color: Colors.transparent,
              child: ExpansionTile(
                title: Text(i18n.t('chat:permission.details')),
                children: [
                  Text(
                    const JsonEncoder.withIndent('  ').convert(request.input),
                  ),
                ],
              ),
            ),
          ],
          Wrap(
            spacing: 8,
            children: [
              FilledButton(
                onPressed: _submitting ? null : () => _reply('once'),
                style: FilledButton.styleFrom(
                  backgroundColor: t.ink,
                  foregroundColor: t.bg,
                  padding: const EdgeInsets.symmetric(
                    horizontal: 14,
                    vertical: 6,
                  ),
                  shape: RoundedRectangleBorder(
                    borderRadius: BorderRadius.circular(Radii.full),
                  ),
                ),
                child: Text(
                  i18n.t('chat:permission.allow'),
                  style: const TextStyle(fontSize: FontSizes.sm),
                ),
              ),
              const SizedBox(width: 8),
              OutlinedButton(
                onPressed: _submitting ? null : () => _reply('always'),
                style: OutlinedButton.styleFrom(
                  side: BorderSide(color: t.hair),
                  foregroundColor: t.ink,
                  padding: const EdgeInsets.symmetric(
                    horizontal: 14,
                    vertical: 6,
                  ),
                  shape: RoundedRectangleBorder(
                    borderRadius: BorderRadius.circular(Radii.full),
                  ),
                ),
                child: Text(
                  i18n.t('chat:permission.allowAlways'),
                  style: const TextStyle(fontSize: FontSizes.sm),
                ),
              ),
              const SizedBox(width: 8),
              TextButton(
                onPressed: _submitting ? null : () => _reply('reject'),
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
