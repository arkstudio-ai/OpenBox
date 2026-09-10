import 'dart:async';

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/platform_accounts_api.dart';
import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/platform_account.dart';
import '../state/auth_center_providers.dart';
import 'auth_widgets.dart';
import 'desktop_login_card.dart';

class DesktopLoginPanel extends ConsumerStatefulWidget {
  const DesktopLoginPanel({
    super.key,
    required this.scope,
    required this.sites,
    required this.accounts,
    required this.canManage,
    required this.onChanged,
    this.onOpenDesktop,
  });
  final PlatformScope scope;
  final List<PlatformInfo> sites;
  final List<PlatformAccount> accounts;
  final bool canManage;
  final VoidCallback onChanged;
  final VoidCallback? onOpenDesktop;

  @override
  ConsumerState<DesktopLoginPanel> createState() => _DesktopLoginPanelState();
}

class _DesktopLoginPanelState extends ConsumerState<DesktopLoginPanel>
    with
        AutomaticKeepAliveClientMixin<DesktopLoginPanel>,
        WidgetsBindingObserver {
  @override
  bool get wantKeepAlive => true;
  CancelToken? _operation;
  CancelToken? _pollCancel;
  Timer? _timer;
  ({String site, String id})? _awaiting;
  DateTime? _deadline;
  PlatformAccount? _logout;
  Object? _error;
  String? _message;
  bool _active = true;

  PlatformAccountsApi get _api => ref.read(platformAccountsApiProvider);
  bool get _busy => _operation != null || _pollCancel != null || !_active;
  bool _current(CancelToken token) => mounted && _active && !token.isCancelled;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    _active = state == AppLifecycleState.resumed;
    if (!_active) {
      _timer?.cancel();
      _pollCancel?.cancel();
      _operation?.cancel();
      _pollCancel = null;
      _operation = null;
    } else if (_awaiting != null) {
      _schedulePoll(Duration.zero);
    }
    if (mounted) setState(() {});
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _timer?.cancel();
    _pollCancel?.cancel();
    _operation?.cancel();
    super.dispose();
  }

  Future<void> _run(Future<void> Function(CancelToken) action) async {
    if (_busy) return;
    final token = CancelToken();
    setState(() {
      _operation = token;
      _error = null;
      _message = null;
    });
    try {
      await action(token);
      if (_current(token)) widget.onChanged();
    } catch (error) {
      if (_current(token)) setState(() => _error = error);
    } finally {
      if (mounted && identical(_operation, token)) {
        setState(() => _operation = null);
      }
    }
  }

  Future<void> _open(PlatformInfo site) => _run((token) async {
    final row = await _api.openDesktopLogin(
      widget.scope,
      site.key,
      cancel: token,
    );
    if (!_current(token)) return;
    setState(() {
      _awaiting = (site: site.key, id: row.id);
      _deadline = ref
          .read(desktopLoginClockProvider)()
          .add(const Duration(minutes: 3));
      _message = 'toast.desktopLoginOpened';
    });
    _schedulePoll();
    widget.onOpenDesktop?.call();
  });

  Future<void> _probe(PlatformAccount account) => _run((token) async {
    final row = await _api.probe(widget.scope, account.id, cancel: token);
    if (!_current(token)) return;
    setState(
      () => _message = row.status == 'bound'
          ? 'toast.desktopLoggedIn'
          : 'desktop.status.${row.status}',
    );
  });

  Future<void> _probeAll() => _run((token) async {
    await _api.probeDesktopLogins(widget.scope, cancel: token);
  });

  Future<void> _confirmLogout() async {
    final row = _logout;
    if (row == null || !widget.canManage || _busy) return;
    await _run((token) async {
      await _api.logoutDesktopLogin(widget.scope, row.id, cancel: token);
      if (!_current(token)) return;
      setState(() {
        _logout = null;
        _message = 'toast.desktopLoggedOut';
      });
    });
  }

  void _schedulePoll([Duration delay = const Duration(seconds: 5)]) {
    _timer?.cancel();
    if (mounted && _active && _awaiting != null) {
      _timer = Timer(delay, () => unawaited(_poll()));
    }
  }

  Future<void> _poll() async {
    final awaiting = _awaiting;
    if (!mounted || !_active || awaiting == null) return;
    if (!ref.read(desktopLoginClockProvider)().isBefore(_deadline!)) {
      setState(() {
        _awaiting = null;
        _message = 'toast.desktopLoginTimeout';
      });
      return;
    }
    if (_busy) {
      _schedulePoll();
      return;
    }
    final token = CancelToken();
    setState(() => _pollCancel = token);
    try {
      final row = await _api.probe(widget.scope, awaiting.id, cancel: token);
      if (!_current(token) || _awaiting != awaiting) return;
      widget.onChanged();
      if (row.status == 'bound') {
        setState(() {
          _awaiting = null;
          _message = 'toast.desktopLoggedIn';
          _error = null;
        });
      }
    } catch (error) {
      if (_current(token)) setState(() => _error = error);
    } finally {
      if (mounted && identical(_pollCancel, token)) {
        setState(() => _pollCancel = null);
        _schedulePoll();
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    super.build(context);
    final i18n = ref.watch(i18nProvider);
    final t = context.tokens;
    final accounts = {
      for (final row in widget.accounts)
        if (row.authKind == 'desktop_cookie') row.platform: row,
    };
    final desktopId = accounts.values
        .map((row) => row.desktopId)
        .whereType<String>()
        .firstOrNull;
    return AuthCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            i18n.t('auth-center:desktop.title'),
            style: TextStyle(
              color: t.ink,
              fontSize: FontSizes.lg,
              fontWeight: FontWeight.w500,
            ),
          ),
          if (desktopId != null)
            Text(
              i18n.t(
                'auth-center:desktop.desktopId',
                vars: {
                  'id': desktopId.length > 8
                      ? desktopId.substring(desktopId.length - 8)
                      : desktopId,
                },
              ),
              style: TextStyle(color: t.n600, fontSize: 12),
            ),
          Text(
            i18n.t('auth-center:desktop.subtitle'),
            style: TextStyle(color: t.n600),
          ),
          TextButton.icon(
            onPressed: _busy ? null : _probeAll,
            icon: const Icon(Icons.refresh),
            label: Text(i18n.t('auth-center:desktop.probeAll')),
          ),
          if (_error != null)
            Text(
              platformErrorText(i18n, _error!),
              style: TextStyle(color: t.danger),
            ),
          if (_message != null)
            Text(
              i18n.t('auth-center:$_message'),
              style: TextStyle(color: t.n700),
            ),
          if (_logout != null && widget.canManage) ...[
            Text(
              i18n.t('auth-center:desktopLogout.title'),
              style: const TextStyle(fontWeight: FontWeight.w600),
            ),
            Text(
              i18n.t(
                'auth-center:desktopLogout.body',
                vars: {'name': _logout!.siteDisplay ?? _logout!.platform},
              ),
            ),
            Wrap(
              spacing: 8,
              children: [
                TextButton(
                  onPressed: _busy
                      ? null
                      : () => setState(() => _logout = null),
                  child: Text(i18n.t('common:action.cancel')),
                ),
                FilledButton(
                  key: const ValueKey('confirm-desktop-logout'),
                  onPressed: _busy ? null : _confirmLogout,
                  child: Text(i18n.t('auth-center:desktop.actions.logout')),
                ),
              ],
            ),
          ],
          for (final site in widget.sites)
            DesktopLoginCard(
              site: site,
              account: accounts[site.key],
              canManage: widget.canManage,
              busy: _busy,
              awaiting: _awaiting?.site == site.key,
              onLogin: () => unawaited(_open(site)),
              onProbe: () => unawaited(_probe(accounts[site.key]!)),
              onLogout: () => setState(() => _logout = accounts[site.key]),
              onViewDesktop: widget.onOpenDesktop,
            ),
        ],
      ),
    );
  }
}
