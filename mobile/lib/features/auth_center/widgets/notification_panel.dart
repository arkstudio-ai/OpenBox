import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/platform_accounts_api.dart';
import '../../../shared/appearance/tokens.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/platform_account.dart';
import '../state/auth_center_providers.dart';
import 'auth_widgets.dart';

class NotificationPanel extends ConsumerStatefulWidget {
  const NotificationPanel({super.key, required this.scope});
  final PlatformScope scope;
  @override
  ConsumerState<NotificationPanel> createState() => _NotificationPanelState();
}

class _NotificationPanelState extends ConsumerState<NotificationPanel> {
  final _cancel = CancelToken();
  final _pending = <String>{};
  Object? _error;

  @override
  void dispose() {
    _cancel.cancel();
    super.dispose();
  }

  Future<void> _read(String id) async {
    if (_pending.contains(id)) return;
    setState(() {
      _pending.add(id);
      _error = null;
    });
    try {
      await ref
          .read(platformAccountsApiProvider)
          .markNotificationRead(widget.scope, id, cancel: _cancel);
      if (!mounted || _cancel.isCancelled) return;
      ref.invalidate(platformNotificationsProvider(widget.scope));
    } catch (error) {
      if (mounted && !_cancel.isCancelled) setState(() => _error = error);
    } finally {
      if (mounted) setState(() => _pending.remove(id));
    }
  }

  @override
  Widget build(BuildContext context) {
    final data = ref.watch(platformNotificationsProvider(widget.scope));
    final i18n = ref.watch(i18nProvider);
    final t = context.tokens;
    final page = data.valueOrNull;
    if (data.hasError && page == null) {
      return AuthError(
        error: data.error!,
        retry: () =>
            ref.invalidate(platformNotificationsProvider(widget.scope)),
      );
    }
    if (page == null || page.items.isEmpty) return const SizedBox.shrink();
    return AuthCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            i18n.t('auth-center:notifications.title', count: page.unread),
            style: TextStyle(color: t.ink, fontWeight: FontWeight.w600),
          ),
          for (final item in page.items)
            Padding(
              padding: const EdgeInsets.only(top: 10),
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(item.title, style: TextStyle(color: t.ink)),
                        if (item.body.isNotEmpty)
                          Text(
                            item.body,
                            style: TextStyle(color: t.n600, fontSize: 12),
                          ),
                      ],
                    ),
                  ),
                  IconButton(
                    key: ValueKey('read-notification-${item.id}'),
                    tooltip: i18n.t('auth-center:notifications.markRead'),
                    onPressed: _pending.contains(item.id)
                        ? null
                        : () => _read(item.id),
                    icon: const Icon(Icons.close, size: 18),
                  ),
                ],
              ),
            ),
          if (_error != null || data.hasError)
            Text(
              platformErrorText(i18n, _error ?? data.error!),
              style: TextStyle(color: t.danger),
            ),
        ],
      ),
    );
  }
}
