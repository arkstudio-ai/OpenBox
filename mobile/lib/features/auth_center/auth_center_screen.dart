import 'dart:async';

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../shared/api/platform_accounts_api.dart';
import '../../shared/appearance/tokens.dart';
import '../../shared/i18n/i18n.dart';
import '../../shared/models/platform_account.dart';
import '../../shared/platforms/platform_links.dart';
import '../../shared/widgets/toast.dart';
import 'state/auth_center_providers.dart';
import 'widgets/auth_widgets.dart';
import 'widgets/platform_account_card.dart';
import 'widgets/platform_qr.dart';
import 'widgets/publish_job_view.dart';
import 'widgets/publish_sheet.dart';

/// Scope is injected by app composition; no auth-center → workspace imports.
/// Inner panels stay in this keyed subtree, so no modal can outlive its tenant.
class AuthCenterScreen extends ConsumerStatefulWidget {
  const AuthCenterScreen({
    super.key,
    required this.scope,
    required this.canManage,
    this.initialJobId,
    this.onExit,
  });
  final PlatformScope scope;
  final bool canManage;
  final String? initialJobId;
  final VoidCallback? onExit;
  @override
  ConsumerState<AuthCenterScreen> createState() => _AuthCenterScreenState();
}

class _AuthCenterScreenState extends ConsumerState<AuthCenterScreen>
    with WidgetsBindingObserver {
  final _cancel = CancelToken();
  Timer? _poll;
  Timer? _clock;
  bool _active = true;
  bool _busy = false;
  bool _publishing = false;
  String? _jobId;
  Uri? _authorizeUri;
  DateTime? _authorizeUntil;
  PlatformAccount? _pendingUnbind;

  @override
  void initState() {
    super.initState();
    _jobId = widget.initialJobId;
    WidgetsBinding.instance.addObserver(this);
    _startPoll();
    _clock = Timer.periodic(const Duration(seconds: 1), (_) {
      if (mounted && _active && _authorizeUri != null) setState(() {});
    });
  }

  void _startPoll() {
    _poll?.cancel();
    _poll = Timer.periodic(const Duration(seconds: 15), (_) => _refresh());
  }

  void _refresh() {
    if (!mounted || !_active) return;
    ref.invalidate(platformAccountsProvider(widget.scope));
    ref.invalidate(publishJobsProvider(widget.scope));
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    _active = state == AppLifecycleState.resumed;
    _poll?.cancel();
    if (_active) {
      _refresh();
      _startPoll();
    }
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _cancel.cancel();
    _poll?.cancel();
    _clock?.cancel();
    super.dispose();
  }

  Future<void> _bind(String platform) async {
    if (_busy || !widget.canManage) return;
    final deadline = DateTime.now().add(const Duration(minutes: 10));
    setState(() => _busy = true);
    try {
      final url = await ref
          .read(platformAccountsApiProvider)
          .authorize(widget.scope, platform, cancel: _cancel);
      if (!mounted) return;
      final uri = douyinAuthorizationUri(url);
      if (uri == null) {
        throw const FormatException('Unsupported platform authorization URL');
      }
      setState(() {
        _authorizeUri = uri;
        _authorizeUntil = deadline;
      });
    } catch (e) {
      _showError(e);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _probe(PlatformAccount account) async {
    if (_busy) return;
    setState(() => _busy = true);
    try {
      final result = await ref
          .read(platformAccountsApiProvider)
          .probe(widget.scope, account.id, cancel: _cancel);
      if (!mounted) return;
      ref
          .read(toastProvider.notifier)
          .info(
            ref
                .read(i18nProvider)
                .t(
                  result.status == 'bound'
                      ? 'auth-center:toast.probeOk'
                      : 'auth-center:toast.probeExpired',
                ),
          );
      _refresh();
    } catch (e) {
      _showError(e);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _unbind() async {
    final target = _pendingUnbind;
    if (target == null || _busy || !widget.canManage) return;
    setState(() => _busy = true);
    try {
      await ref
          .read(platformAccountsApiProvider)
          .unbind(widget.scope, target.id, cancel: _cancel);
      if (!mounted) return;
      setState(() => _pendingUnbind = null);
      ref
          .read(toastProvider.notifier)
          .success(ref.read(i18nProvider).t('auth-center:toast.unbound'));
      _refresh();
    } catch (e) {
      _showError(e);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  void _showError(Object error) {
    if (!mounted || _cancel.isCancelled) return;
    ref
        .read(toastProvider.notifier)
        .error(platformErrorText(ref.read(i18nProvider), error));
  }

  bool get _inner => _publishing || _jobId != null || _authorizeUri != null;
  void _backToAccounts() {
    setState(() {
      _publishing = false;
      _jobId = null;
      _authorizeUri = null;
    });
    _refresh();
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final platforms = ref.watch(platformsProvider(widget.scope));
    final accounts = ref.watch(platformAccountsProvider(widget.scope));
    final jobs = ref.watch(publishJobsProvider(widget.scope));
    return PopScope(
      canPop: !_inner,
      onPopInvokedWithResult: (didPop, _) {
        if (!didPop && _inner) _backToAccounts();
      },
      child: Scaffold(
        backgroundColor: t.bg,
        appBar: AppBar(
          title: Text(i18n.t('auth-center:page.title')),
          leading: BackButton(
            onPressed: _inner
                ? _backToAccounts
                : widget.onExit ?? () => Navigator.maybePop(context),
          ),
          actions: [
            IconButton(
              onPressed: () {
                ref.invalidate(platformsProvider(widget.scope));
                _refresh();
              },
              tooltip: i18n.t('common:action.retry'),
              icon: const Icon(Icons.refresh),
            ),
          ],
        ),
        body: _publishing
            ? PublishSheet(
                key: ValueKey(widget.scope),
                scope: widget.scope,
                onClose: _backToAccounts,
              )
            : RefreshIndicator(
                onRefresh: () async => _refresh(),
                child: ListView(
                  padding: const EdgeInsets.all(16),
                  physics: const AlwaysScrollableScrollPhysics(),
                  children: [
                    if (_jobId != null)
                      AuthCard(
                        child: PublishJobView(
                          key: ValueKey(_jobId),
                          scope: widget.scope,
                          jobId: _jobId!,
                          onChanged: _refresh,
                        ),
                      )
                    else if (_authorizeUri != null) ...[
                      AuthCard(
                        child: Column(
                          children: [
                            Text(i18n.t('auth-center:mobile.authorizeIntro')),
                            const SizedBox(height: 12),
                            PlatformQr(
                              uri: _authorizeUri!,
                              valid:
                                  widget.canManage &&
                                  (_authorizeUntil?.isAfter(DateTime.now()) ??
                                      false),
                              name: 'douyin-authorize.png',
                              authorize: true,
                            ),
                            const SizedBox(height: 8),
                            OutlinedButton(
                              onPressed: _backToAccounts,
                              child: Text(
                                i18n.t('auth-center:mobile.checkAccounts'),
                              ),
                            ),
                          ],
                        ),
                      ),
                    ] else ...[
                      Text(
                        i18n.t('auth-center:page.subtitle'),
                        style: TextStyle(color: t.n600),
                      ),
                      const SizedBox(height: 16),
                      if (_pendingUnbind != null && widget.canManage)
                        AuthCard(
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Text(
                                i18n.t('auth-center:unbind.title'),
                                style: TextStyle(
                                  fontWeight: FontWeight.w600,
                                  color: t.ink,
                                ),
                              ),
                              const SizedBox(height: 8),
                              Text(
                                i18n.t(
                                  'auth-center:unbind.body',
                                  vars: {
                                    'name':
                                        _pendingUnbind!.nickname ??
                                        _pendingUnbind!.externalId,
                                  },
                                ),
                              ),
                              Wrap(
                                spacing: 8,
                                children: [
                                  TextButton(
                                    onPressed: _busy
                                        ? null
                                        : () => setState(
                                            () => _pendingUnbind = null,
                                          ),
                                    child: Text(i18n.t('common:action.cancel')),
                                  ),
                                  FilledButton(
                                    onPressed: _busy ? null : _unbind,
                                    child: Text(
                                      i18n.t('auth-center:actions.unbind'),
                                    ),
                                  ),
                                ],
                              ),
                            ],
                          ),
                        ),
                      if ((platforms.isLoading && !platforms.hasValue) ||
                          (accounts.isLoading && !accounts.hasValue))
                        const Center(child: CircularProgressIndicator())
                      else if (platforms.hasError || accounts.hasError)
                        AuthError(
                          error: platforms.error ?? accounts.error!,
                          retry: () {
                            ref.invalidate(platformsProvider(widget.scope));
                            _refresh();
                          },
                        )
                      else if (platforms.valueOrNull?.isEmpty ?? true)
                        AuthCard(
                          child: Text(i18n.t('auth-center:state.noPlatforms')),
                        )
                      else
                        for (final platform in platforms.value!)
                          _platformCard(platform, accounts.value ?? [], i18n),
                      const SizedBox(height: 12),
                      Text(
                        i18n.t('auth-center:mobile.recentJobs'),
                        style: TextStyle(
                          fontWeight: FontWeight.w600,
                          fontSize: 18,
                          color: t.ink,
                        ),
                      ),
                      const SizedBox(height: 12),
                      if (jobs.hasError)
                        AuthError(
                          error: jobs.error!,
                          retry: () =>
                              ref.invalidate(publishJobsProvider(widget.scope)),
                        )
                      else if (jobs.isLoading && !jobs.hasValue)
                        const Center(child: CircularProgressIndicator())
                      else if (jobs.valueOrNull?.isEmpty ?? true)
                        Text(
                          i18n.t('auth-center:mobile.noJobs'),
                          style: TextStyle(color: t.n600),
                        )
                      else
                        for (final job in jobs.value!)
                          AuthCard(
                            child: ListTile(
                              contentPadding: EdgeInsets.zero,
                              onTap: () => setState(() => _jobId = job.id),
                              title: Text(
                                job.title.isEmpty
                                    ? i18n.t('auth-center:mobile.untitledJob')
                                    : job.title,
                                maxLines: 2,
                                overflow: TextOverflow.ellipsis,
                              ),
                              subtitle: Padding(
                                padding: const EdgeInsets.only(top: 6),
                                child: Align(
                                  alignment: Alignment.centerLeft,
                                  child: AuthStatusPill(job.status, job: true),
                                ),
                              ),
                              trailing: const Icon(Icons.chevron_right),
                            ),
                          ),
                    ],
                  ],
                ),
              ),
      ),
    );
  }

  Widget _platformCard(
    PlatformInfo platform,
    List<PlatformAccount> accounts,
    I18nState i18n,
  ) {
    final rows = accounts.where((a) => a.platform == platform.key).toList();
    final supported = platform.key == 'douyin';
    final hasBound = rows.any(
      (a) => const {'bound', 'expiring'}.contains(a.statusAt(DateTime.now())),
    );
    return AuthCard(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Expanded(
                child: Text(
                  platform.display,
                  style: TextStyle(
                    fontSize: 20,
                    fontWeight: FontWeight.w600,
                    color: context.tokens.ink,
                  ),
                ),
              ),
              if (widget.canManage && supported)
                TextButton(
                  onPressed: !platform.configured || _busy
                      ? null
                      : () => _bind(platform.key),
                  child: Text(i18n.t('auth-center:actions.bind')),
                ),
            ],
          ),
          Text(
            i18n.t(
              platform.configured
                  ? 'auth-center:platform.genericHint'
                  : 'auth-center:platform.notConfigured',
            ),
            style: TextStyle(color: context.tokens.n600),
          ),
          if (platform.maxGrantDays != null && platform.configured)
            Text(
              i18n.t(
                'auth-center:platform.grantHint',
                vars: {'days': platform.maxGrantDays},
              ),
              style: TextStyle(fontSize: 12, color: context.tokens.n600),
            ),
          if (rows.isEmpty)
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 16),
              child: Text(
                i18n.t(
                  widget.canManage
                      ? 'auth-center:platform.emptyManager'
                      : 'auth-center:platform.emptyMember',
                ),
              ),
            ),
          for (final account in rows)
            PlatformAccountCard(
              account: account,
              canManage: widget.canManage && supported,
              configured: platform.configured,
              busy: _busy,
              onProbe: () => _probe(account),
              onBind: () => _bind(platform.key),
              onUnbind: () => setState(() => _pendingUnbind = account),
            ),
          if (supported && platform.capabilities.contains('publish')) ...[
            const SizedBox(height: 8),
            FilledButton.icon(
              onPressed: platform.configured && hasBound && !_busy
                  ? () => setState(() => _publishing = true)
                  : null,
              icon: const Icon(Icons.publish_outlined, size: 18),
              label: Text(i18n.t('auth-center:actions.publish')),
            ),
          ],
        ],
      ),
    );
  }
}
