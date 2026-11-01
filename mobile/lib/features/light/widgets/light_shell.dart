import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/i18n/i18n.dart';

/// The four tabs of Light 模式 (docs `LIGHT_MODE_PLAN.md` §4.2): 首页 / 对话 /
/// 消息 / 我的 on a `StatefulShellRoute`, each branch keeping its own state.
/// The app layer supplies the badge counts; this widget only draws the bar.
///
/// System back on any other tab returns to 首页 first; on 首页 it leaves the
/// app, as a home screen does.
class LightShell extends ConsumerWidget {
  const LightShell({
    super.key,
    required this.shell,
    this.inboxUnread = 0,
    this.assistantUnread = 0,
  });

  final StatefulNavigationShell shell;
  final int inboxUnread;
  final int assistantUnread;

  static const tabKeys = ['home', 'chat', 'inbox', 'me'];

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final keyboard =
        MediaQuery.viewInsetsOf(context).bottom > 0 ||
        View.of(context).viewInsets.bottom > 0;
    Widget badged(Widget icon, int count) => count > 0
        ? Badge.count(
            count: count,
            backgroundColor: t.a700,
            textColor: t.bg,
            child: icon,
          )
        : icon;
    return PopScope<Object?>(
      canPop: shell.currentIndex == 0,
      onPopInvokedWithResult: (didPop, _) {
        if (!didPop) shell.goBranch(0);
      },
      child: Scaffold(
        backgroundColor: t.bg,
        body: shell,
        bottomNavigationBar: keyboard
            ? null
            : NavigationBar(
                key: const Key('light-tabs'),
                selectedIndex: shell.currentIndex,
                onDestinationSelected: (index) {
                  FocusManager.instance.primaryFocus?.unfocus();
                  shell.goBranch(
                    index,
                    initialLocation: index == shell.currentIndex,
                  );
                },
                destinations: [
                  NavigationDestination(
                    icon: const Icon(Icons.home_outlined),
                    selectedIcon: const Icon(Icons.home),
                    label: i18n.t('light:tabs.home'),
                  ),
                  NavigationDestination(
                    icon: badged(
                      const Icon(Icons.chat_bubble_outline),
                      assistantUnread,
                    ),
                    selectedIcon: badged(
                      const Icon(Icons.chat_bubble),
                      assistantUnread,
                    ),
                    label: i18n.t('light:tabs.chat'),
                  ),
                  NavigationDestination(
                    icon: badged(
                      const Icon(Icons.notifications_outlined),
                      inboxUnread,
                    ),
                    selectedIcon: badged(
                      const Icon(Icons.notifications),
                      inboxUnread,
                    ),
                    label: i18n.t('light:tabs.inbox'),
                  ),
                  NavigationDestination(
                    icon: const Icon(Icons.person_outline),
                    selectedIcon: const Icon(Icons.person),
                    label: i18n.t('light:tabs.me'),
                  ),
                ],
              ),
      ),
    );
  }
}
