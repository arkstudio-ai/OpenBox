import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/notifications/push_controller.dart';
import '../../../shared/notifications/system_notifications.dart';
import '../../../shared/utils/error_text.dart';

class NotificationsSection extends ConsumerStatefulWidget {
  const NotificationsSection({super.key});
  @override
  ConsumerState<NotificationsSection> createState() =>
      _NotificationsSectionState();
}

class _NotificationsSectionState extends ConsumerState<NotificationsSection> {
  bool _testing = false;
  String? _feedback;

  Future<void> _test(bool remote) async {
    setState(() {
      _testing = true;
      _feedback = null;
    });
    final i18n = ref.read(i18nProvider);
    final push = ref.read(pushControllerProvider);
    try {
      final status = remote
          ? await push.testRemote()
          : (await push.testLocal(
                  i18n.t('settings:notifications.localTitle'),
                  i18n.t('settings:notifications.localBody'),
                )
                ? 'local'
                : 'failed');
      if (mounted) {
        setState(
          () => _feedback = i18n.t('settings:notifications.result.$status'),
        );
      }
    } catch (error) {
      if (mounted) setState(() => _feedback = errorText(i18n, error));
    } finally {
      if (mounted) setState(() => _testing = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final push = ref.watch(pushControllerProvider);
    final native = ref.watch(systemNotificationsProvider);
    final i18n = ref.watch(i18nProvider);
    final tokens = context.tokens;
    final problem = native.registrationError ?? push.errorCode;
    final messageKey = problem == 'NETWORK'
        ? 'errors:network'
        : 'errors:$problem';
    final mapped = i18n.t(messageKey);
    final errorMessage = mapped == messageKey
        ? i18n.t('settings:notifications.setupFailed')
        : mapped;
    final status = !push.wanted
        ? 'off'
        : !native.authorized
        ? native.status
        : push.deliveryReady
        ? 'ready'
        : 'connecting';
    return Container(
      decoration: BoxDecoration(
        color: tokens.card,
        border: Border.all(color: tokens.hair),
        borderRadius: BorderRadius.circular(16),
      ),
      padding: const EdgeInsets.all(14),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          SwitchListTile.adaptive(
            contentPadding: EdgeInsets.zero,
            title: Text(i18n.t('settings:notifications.title')),
            subtitle: Text(i18n.t('settings:notifications.status.$status')),
            value: push.wanted && native.authorized,
            onChanged: push.busy
                ? null
                : (value) async {
                    try {
                      await push.setEnabled(value);
                    } catch (error) {
                      if (mounted) {
                        setState(() => _feedback = errorText(i18n, error));
                      }
                    }
                  },
          ),
          Text(
            i18n.t('settings:notifications.description'),
            style: TextStyle(color: tokens.n600),
          ),
          if (problem != null)
            Padding(
              padding: const EdgeInsets.only(top: 8),
              child: Text(errorMessage, style: TextStyle(color: tokens.n600)),
            ),
          Wrap(
            spacing: 8,
            children: [
              TextButton(
                onPressed: native.openSettings,
                child: Text(i18n.t('settings:notifications.openSettings')),
              ),
              TextButton(
                onPressed: push.busy ? null : push.checkSession,
                child: Text(i18n.t('common:action.retry')),
              ),
              TextButton(
                onPressed: _testing || !native.authorized
                    ? null
                    : () => _test(false),
                child: Text(i18n.t('settings:notifications.testLocal')),
              ),
              TextButton(
                onPressed: _testing || !push.deliveryReady
                    ? null
                    : () => _test(true),
                child: Text(i18n.t('settings:notifications.testRemote')),
              ),
            ],
          ),
          if (_testing) const LinearProgressIndicator(),
          if (_feedback != null)
            Text(_feedback!, style: TextStyle(color: tokens.n600)),
        ],
      ),
    );
  }
}
