import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../shared/appearance/tokens.dart';
import '../../shared/appearance/type_scale.dart';
import '../../shared/i18n/i18n.dart';
import 'billing/billing_page.dart';
import 'fleet/fleet_page.dart';
import 'messages/messages_page.dart';
import 'notifications/notifications_page.dart';
import 'skills/skills_page.dart';

/// Five bottom destinations; only a visited section is instantiated.
class AdminConsole extends ConsumerStatefulWidget {
  const AdminConsole({
    super.key,
    required this.onExit,
    this.initialSection = 'fleet',
  });
  final VoidCallback onExit;
  final String initialSection;
  @override
  ConsumerState<AdminConsole> createState() => _AdminConsoleState();
}

class _AdminConsoleState extends ConsumerState<AdminConsole> {
  static const _sections = [
    'fleet',
    'skills',
    'billing',
    'notifications',
    'messages',
  ];
  late int _index = _sections.contains(widget.initialSection)
      ? _sections.indexOf(widget.initialSection)
      : 0;
  late final _visited = <int>{_index};
  @override
  Widget build(BuildContext context) {
    final i = ref.watch(i18nProvider);
    final t = context.tokens;
    // Subscribe to keyboard metrics even when the root constraints do not change.
    final keyboardInset = MediaQuery.viewInsetsOf(context).bottom;
    return LayoutBuilder(
      builder: (context, constraints) => Scaffold(
        backgroundColor: t.bg,
        appBar: AppBar(
          leading: BackButton(onPressed: widget.onExit),
          title: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text(
                i.t('admin:section.${_sections[_index]}.title'),
                style: const TextStyle(
                  fontSize: FontSizes.lg,
                  fontWeight: FontWeight.w500,
                ),
              ),
              Text(
                _index == 2
                    ? '${i.t('admin:console.title')} · ${i.t('admin:mobile.readOnly')}'
                    : i.t('admin:console.title'),
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: TextStyle(color: t.n600, fontSize: FontSizes.xs),
              ),
            ],
          ),
        ),
        body: IndexedStack(
          index: _index,
          children: [
            _visited.contains(0)
                ? AdminFleetPage(active: _index == 0)
                : const SizedBox.shrink(),
            _visited.contains(1)
                ? const AdminSkillsPage()
                : const SizedBox.shrink(),
            _visited.contains(2)
                ? const AdminBillingPage()
                : const SizedBox.shrink(),
            _visited.contains(3)
                ? AdminNotificationsPage(active: _index == 3)
                : const SizedBox.shrink(),
            _visited.contains(4)
                ? AdminMessagesPage(active: _index == 4)
                : const SizedBox.shrink(),
          ],
        ),
        bottomNavigationBar:
            keyboardInset > 0 || View.of(context).viewInsets.bottom > 0
            ? null
            : NavigationBar(
                height: 64,
                elevation: 0,
                backgroundColor: t.card,
                indicatorColor: t.a200,
                selectedIndex: _index,
                onDestinationSelected: (index) => setState(() {
                  FocusManager.instance.primaryFocus?.unfocus();
                  _index = index;
                  _visited.add(index);
                }),
                destinations: [
                  NavigationDestination(
                    icon: const Icon(Icons.dns_outlined),
                    selectedIcon: const Icon(Icons.dns),
                    label: i.t('admin:nav.fleet'),
                  ),
                  NavigationDestination(
                    icon: const Icon(Icons.extension_outlined),
                    selectedIcon: const Icon(Icons.extension),
                    label: i.t('admin:nav.skills'),
                  ),
                  NavigationDestination(
                    icon: const Icon(Icons.receipt_long_outlined),
                    selectedIcon: const Icon(Icons.receipt_long),
                    label: i.t('admin:nav.billing'),
                  ),
                  NavigationDestination(
                    icon: const Icon(Icons.notifications_outlined),
                    selectedIcon: const Icon(Icons.notifications),
                    label: i.t('admin:nav.notifications'),
                  ),
                  NavigationDestination(
                    icon: const Icon(Icons.campaign_outlined),
                    selectedIcon: const Icon(Icons.campaign),
                    label: i.t('admin:nav.messages'),
                  ),
                ],
              ),
      ),
    );
  }
}
