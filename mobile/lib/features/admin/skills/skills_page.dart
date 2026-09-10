import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/i18n/i18n.dart';
import '../widgets/admin_widgets.dart';
import 'installs_page.dart';
import 'review_page.dart';
import 'store_page.dart';

class AdminSkillsPage extends ConsumerStatefulWidget {
  const AdminSkillsPage({super.key});
  @override
  ConsumerState<AdminSkillsPage> createState() => _SkillsState();
}

class _SkillsState extends ConsumerState<AdminSkillsPage> {
  String _tab = 'store';
  final _visited = <String>{'store'};
  @override
  Widget build(BuildContext context) {
    final i = ref.watch(i18nProvider);
    return Column(
      children: [
        AdminTabs(
          labels: {
            for (final tab in ['store', 'review', 'installs'])
              tab: i.t('admin-skills:tab.$tab'),
          },
          value: _tab,
          onChanged: (tab) => setState(() {
            _tab = tab;
            _visited.add(tab);
          }),
        ),
        Expanded(
          child: IndexedStack(
            index: ['store', 'review', 'installs'].indexOf(_tab),
            children: [
              AdminStorePage(active: _tab == 'store'),
              _visited.contains('review')
                  ? AdminReviewPage(active: _tab == 'review')
                  : const SizedBox.shrink(),
              _visited.contains('installs')
                  ? const AdminInstallsPage()
                  : const SizedBox.shrink(),
            ],
          ),
        ),
      ],
    );
  }
}
