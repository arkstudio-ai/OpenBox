import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/i18n/i18n.dart';
import '../api/admin_api.dart';
import '../widgets/admin_confirm.dart';
import '../widgets/admin_widgets.dart';

class AdminAdoptPage extends ConsumerStatefulWidget {
  const AdminAdoptPage({super.key});
  @override
  ConsumerState<AdminAdoptPage> createState() => _AdoptState();
}

class _AdoptState extends ConsumerState<AdminAdoptPage> {
  final _id = TextEditingController();
  String _state = 'reserve';
  bool _rebuild = false, _verified = false;
  @override
  void dispose() {
    _id.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    final i = ref.read(i18nProvider);
    final id = _id.text.trim();
    if (id.isEmpty) return;
    final changed = await confirmAdminAction(
      context,
      title: i.t('admin:pool.adopt'),
      body: _rebuild
          ? i.t('admin:pool.confirmAdoptRebuild', vars: {'id': id})
          : '$id\n$_state\n${i.t('admin:mobile.adoptHint')}',
      target: _rebuild ? id : null,
      confirm: i.t('admin:pool.adopt'),
      run: (_, cancel) => ref
          .read(adminApiProvider)
          .adoptDesktop(
            id,
            poolState: _state,
            rebuild: _rebuild,
            gatewayReleaseVerified: _verified,
            cancel: cancel,
          ),
    );
    if (changed && mounted) Navigator.pop(context, true);
  }

  @override
  Widget build(BuildContext context) {
    final i = ref.watch(i18nProvider);
    return Scaffold(
      backgroundColor: context.tokens.bg,
      appBar: AppBar(title: Text(i.t('admin:pool.adopt'))),
      body: Column(
        children: [
          Expanded(
            child: AdminList(
              children: [
                AdminCard(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      Text(i.t('admin:mobile.adoptHint')),
                      const SizedBox(height: 20),
                      TextField(
                        controller: _id,
                        autocorrect: false,
                        smartQuotesType: SmartQuotesType.disabled,
                        smartDashesType: SmartDashesType.disabled,
                        decoration: InputDecoration(
                          labelText: i.t('admin:pool.adoptId'),
                        ),
                        onChanged: (_) => setState(() {}),
                      ),
                      const SizedBox(height: 20),
                      DropdownButtonFormField<String>(
                        initialValue: _state,
                        isExpanded: true,
                        decoration: InputDecoration(
                          labelText: i.t('admin:desktops.state'),
                        ),
                        items: [
                          DropdownMenuItem(
                            value: 'reserve',
                            child: Text(i.t('admin:pool.reserveState')),
                          ),
                          DropdownMenuItem(
                            value: 'prewarm',
                            child: Text(i.t('admin:pool.prewarmState')),
                          ),
                        ],
                        onChanged: (value) => setState(() => _state = value!),
                      ),
                      CheckboxListTile(
                        contentPadding: EdgeInsets.zero,
                        value: _rebuild,
                        title: Text(i.t('admin:pool.rebuildOnAdopt')),
                        onChanged: (value) => setState(() => _rebuild = value!),
                      ),
                      CheckboxListTile(
                        contentPadding: EdgeInsets.zero,
                        value: _verified,
                        title: Text(i.t('admin:pool.gatewayReleaseVerified')),
                        onChanged: (value) =>
                            setState(() => _verified = value!),
                      ),
                    ],
                  ),
                ),
              ],
            ),
          ),
          AdminActionBar(
            primary: FilledButton(
              onPressed: _id.text.trim().isEmpty ? null : _submit,
              child: Text(i.t('admin:pool.adopt')),
            ),
          ),
        ],
      ),
    );
  }
}
