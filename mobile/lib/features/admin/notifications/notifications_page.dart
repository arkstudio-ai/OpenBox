import 'dart:async';

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/auth_store.dart';
import '../../../shared/appearance/tokens.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/notifications/push_controller.dart';
import '../../../shared/notifications/system_notifications.dart';
import '../../../shared/utils/error_text.dart';
import '../api/admin_api.dart';
import '../models/admin_data.dart';
import '../state/admin_load_state.dart';

class AdminNotificationsPage extends ConsumerStatefulWidget {
  const AdminNotificationsPage({super.key, this.active = true});
  final bool active;
  @override
  ConsumerState<AdminNotificationsPage> createState() => _NotificationsState();
}

class _NotificationsState
    extends AdminLoadState<AdminRecord, AdminNotificationsPage>
    with WidgetsBindingObserver {
  Timer? _poll;
  bool _visible = true;
  bool _sending = false;
  String _template = 'system_test';
  String? _feedback;
  ({String binding, String template, String id})? _request;

  @override
  bool get loadOnMount => ref.read(authProvider).user?.role == 'admin';

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _poll = Timer.periodic(const Duration(seconds: 2), (_) {
      if (!mounted || !widget.active || !_visible || loading) return;
      if (data
              ?.records('tests')
              .any(
                (item) =>
                    [
                      'pending',
                      'sending',
                      'accepted',
                    ].contains(item.string('status')) &&
                    item.string('receipt').isEmpty &&
                    (DateTime.tryParse(
                          item.string('expiresAt'),
                        )?.isAfter(DateTime.now()) ??
                        false),
              ) ??
          false) {
        unawaited(reload());
      }
    });
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    _visible = state == AppLifecycleState.resumed;
    if (_visible && widget.active) unawaited(reload());
  }

  @override
  void didUpdateWidget(AdminNotificationsPage oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (widget.active && !oldWidget.active) unawaited(reload());
  }

  @override
  Future<AdminRecord> fetch(CancelToken cancel) =>
      api.pushOverview(ref.read(i18nProvider).language, cancel);

  Future<void> _send(bool local) async {
    if (_sending || data == null) return;
    final device = data!.record('device');
    if (!local && !device.flag('ready')) return;
    setState(() {
      _sending = true;
      _feedback = null;
    });
    try {
      api.checkAccess();
      if (local) {
        final selected = data!
            .records('templates')
            .firstWhere((v) => v.string('id') == _template);
        final ok = await ref
            .read(pushControllerProvider)
            .testLocal(selected.string('title'), selected.string('body'));
        if (mounted) {
          setState(
            () => _feedback = i18n.t(
              ok ? 'admin:push.localQueued' : 'admin:push.localFailed',
            ),
          );
        }
      } else {
        final binding = device.string('bindingId');
        if (_request == null ||
            _request!.binding != binding ||
            _request!.template != _template) {
          _request = (
            binding: binding,
            template: _template,
            id: newPushTestRequestId(),
          );
        }
        await api.sendPushTest(
          template: _template,
          bindingId: binding,
          requestId: _request!.id,
        );
        _request = null;
        if (!mounted) return;
        setState(() => _feedback = i18n.t('admin:push.queued'));
        await reload();
      }
    } catch (error) {
      if (mounted) setState(() => _feedback = errorText(i18n, error));
    } finally {
      if (mounted) setState(() => _sending = false);
    }
  }

  Widget _label(String title, String value) => Padding(
    padding: const EdgeInsets.symmetric(vertical: 6),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          i18n.t('admin:push.$title'),
          style: TextStyle(color: context.tokens.n500, fontSize: 12),
        ),
        Text(value),
      ],
    ),
  );
  Widget _section(String title, List<Widget> children) => Container(
    margin: const EdgeInsets.only(bottom: 16),
    padding: const EdgeInsets.all(16),
    decoration: BoxDecoration(
      color: context.tokens.card,
      border: Border.all(color: context.tokens.hair),
      borderRadius: BorderRadius.circular(16),
    ),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Text(
          i18n.t('admin:push.$title'),
          style: const TextStyle(fontWeight: FontWeight.w600),
        ),
        const SizedBox(height: 10),
        ...children,
      ],
    ),
  );

  @override
  Widget build(BuildContext context) {
    ref.listen(i18nProvider, (old, next) {
      if (old?.language != next.language) unawaited(reload());
    });
    final native = ref.watch(systemNotificationsProvider);
    final allowed = ref.watch(authProvider).user?.role == 'admin';
    if (!allowed) return Center(child: Text(i18n.t('admin:mobile.forbidden')));
    return loadable((snapshot) {
      final device = snapshot.record('device');
      final templates = snapshot.records('templates');
      final selected = templates
          .where((item) => item.string('id') == _template)
          .firstOrNull;
      final tests = snapshot.records('tests');
      return ListView(
        padding: const EdgeInsets.all(16),
        children: [
          Text(i18n.t('admin:push.scope')),
          Align(
            alignment: Alignment.centerRight,
            child: TextButton.icon(
              onPressed: loading ? null : reload,
              icon: const Icon(Icons.refresh),
              label: Text(i18n.t('admin:push.refresh')),
            ),
          ),
          _section('device', [
            _label(
              'phone',
              device.flag('registered')
                  ? i18n.t('admin:push.platform.${device.string('platform')}')
                  : i18n.t('admin:push.noDevice'),
            ),
            _label(
              'permission',
              i18n.t(
                device.flag('notificationsEnabled')
                    ? 'admin:push.enabled'
                    : 'admin:push.disabled',
              ),
            ),
            _label(
              'appState',
              i18n.t(
                'admin:push.presence.${snapshot.record('presence').string('appState', 'unknown')}',
              ),
            ),
            _label(
              'channel',
              snapshot
                  .records('providers')
                  .map(
                    (p) =>
                        '${i18n.t('admin:push.provider.${p.string('id')}')} · ${i18n.t(p.flag('configured') ? 'admin:push.configured' : 'admin:push.unconfigured')}',
                  )
                  .join(' / '),
            ),
            if (!device.flag('ready')) Text(i18n.t('admin:push.setupHint')),
          ]),
          _section('preview', [
            DropdownButtonFormField<String>(
              initialValue: _template,
              isExpanded: true,
              decoration: InputDecoration(
                labelText: i18n.t('admin:push.template'),
              ),
              items: [
                for (final item in templates)
                  DropdownMenuItem(
                    value: item.string('id'),
                    child: Text(
                      i18n.t('admin:push.templateName.${item.string('id')}'),
                      overflow: TextOverflow.ellipsis,
                    ),
                  ),
              ],
              onChanged: _sending
                  ? null
                  : (value) {
                      if (value != null) setState(() => _template = value);
                    },
            ),
            if (selected != null)
              Container(
                margin: const EdgeInsets.symmetric(vertical: 16),
                padding: const EdgeInsets.all(14),
                decoration: BoxDecoration(
                  color: context.tokens.hairSoft,
                  borderRadius: BorderRadius.circular(12),
                ),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      selected.string('title'),
                      style: const TextStyle(fontWeight: FontWeight.w600),
                    ),
                    const SizedBox(height: 4),
                    Text(selected.string('body')),
                  ],
                ),
              ),
            Text(i18n.t('admin:push.instructions')),
            const SizedBox(height: 12),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                FilledButton(
                  onPressed: _sending || !device.flag('ready')
                      ? null
                      : () => _send(false),
                  child: Text(
                    i18n.t(_sending ? 'admin:push.sending' : 'admin:push.send'),
                  ),
                ),
                OutlinedButton(
                  onPressed: _sending || !native.authorized
                      ? null
                      : () => _send(true),
                  child: Text(i18n.t('admin:push.local')),
                ),
              ],
            ),
            if (_feedback != null)
              Padding(
                padding: const EdgeInsets.only(top: 12),
                child: Semantics(liveRegion: true, child: Text(_feedback!)),
              ),
          ]),
          _section('history', [
            Text(
              i18n.t('admin:push.receiptHint'),
              style: TextStyle(color: context.tokens.n600),
            ),
            if (tests.isEmpty)
              Padding(
                padding: const EdgeInsets.only(top: 12),
                child: Text(i18n.t('admin:push.empty')),
              ),
            for (final item in tests) ...[
              const Divider(height: 24),
              Text(
                item.string('title'),
                style: const TextStyle(fontWeight: FontWeight.w600),
              ),
              Text(
                i18n.t(
                  'admin:push.status.${item.string('receipt', item.string('status'))}',
                ),
              ),
              Text(item.string('body')),
              if (item.string('error').isNotEmpty)
                Text(_reason(item.string('error'))),
              Text(
                adminDate(item.string('createdAt'), i18n.language),
                style: TextStyle(color: context.tokens.n500, fontSize: 12),
              ),
            ],
          ]),
        ],
      );
    });
  }

  String _reason(String code) {
    final key = 'admin:push.reason.$code';
    final value = i18n.t(key);
    return value == key
        ? i18n.t(
            code.startsWith('presence_')
                ? 'admin:push.waitingBackground'
                : 'admin:push.deliveryFailed',
          )
        : value;
  }

  @override
  void dispose() {
    _poll?.cancel();
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }
}
