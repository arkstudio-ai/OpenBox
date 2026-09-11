import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/notifications/push_controller.dart';
import '../../../shared/notifications/system_notifications.dart';
import '../../../shared/utils/error_text.dart';
import '../../../shared/widgets/toast.dart';

class NotificationsSection extends ConsumerWidget {
  const NotificationsSection({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final push = ref.watch(pushControllerProvider);
    final native = ref.watch(systemNotificationsProvider);
    final i18n = ref.watch(i18nProvider);
    final tokens = context.tokens;
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
            value: push.wanted && native.authorized,
            onChanged: push.busy
                ? null
                : (value) async {
                    try {
                      await push.setEnabled(value);
                    } catch (error) {
                      if (context.mounted) {
                        ref
                            .read(toastProvider.notifier)
                            .error(errorText(i18n, error));
                      }
                    }
                  },
          ),
          TextButton(
            onPressed: native.openSettings,
            child: Text(i18n.t('settings:notifications.openSettings')),
          ),
        ],
      ),
    );
  }
}
