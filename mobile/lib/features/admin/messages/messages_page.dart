import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/i18n/i18n.dart';
import '../widgets/admin_widgets.dart';
import 'announcements_page.dart';
import 'topics_page.dart';

/// 消息通知 (web `AdminMessagesRoute`): announcements and topic pages.
class AdminMessagesPage extends ConsumerStatefulWidget {
  const AdminMessagesPage({super.key, this.active = true});
  final bool active;
  @override
  ConsumerState<AdminMessagesPage> createState() => _MessagesState();
}

class _MessagesState extends ConsumerState<AdminMessagesPage> {
  static const _tabs = ['announcements', 'topics'];
  String _tab = 'announcements';
  final _visited = <String>{'announcements'};
  @override
  Widget build(BuildContext context) {
    final i = ref.watch(i18nProvider);
    return Column(
      children: [
        AdminTabs(
          labels: {
            for (final tab in _tabs) tab: i.t('admin-messages:tab.$tab'),
          },
          value: _tab,
          onChanged: (tab) => setState(() {
            _tab = tab;
            _visited.add(tab);
          }),
        ),
        Expanded(
          child: IndexedStack(
            index: _tabs.indexOf(_tab),
            children: [
              AdminAnnouncementsPage(
                active: widget.active && _tab == 'announcements',
              ),
              _visited.contains('topics')
                  ? AdminTopicsPage(active: widget.active && _tab == 'topics')
                  : const SizedBox.shrink(),
            ],
          ),
        ),
      ],
    );
  }
}
