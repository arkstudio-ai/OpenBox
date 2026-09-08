import 'dart:async';

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/platform_accounts_api.dart';
import '../../../shared/appearance/tokens.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/platform_account.dart';
import '../../../shared/platforms/platform_links.dart';
import 'auth_widgets.dart';
import 'platform_qr.dart';

/// Only the backend/Webhook decides success. Opening Douyin is not a receipt.
class PublishJobView extends ConsumerStatefulWidget {
  const PublishJobView({
    super.key,
    required this.scope,
    required this.jobId,
    this.result,
    this.onChanged,
  });
  final PlatformScope scope;
  final String jobId;
  final PublishResult? result;
  final VoidCallback? onChanged;
  @override
  ConsumerState<PublishJobView> createState() => _PublishJobViewState();
}

class _PublishJobViewState extends ConsumerState<PublishJobView>
    with WidgetsBindingObserver {
  PublishJob? _job;
  Object? _error;
  Timer? _poll;
  Timer? _clock;
  CancelToken? _cancel;
  int _generation = 0;
  bool _active = true;
  bool _loading = false;

  @override
  void initState() {
    super.initState();
    _job = widget.result?.job;
    WidgetsBinding.instance.addObserver(this);
    _clock = Timer.periodic(const Duration(seconds: 1), (_) {
      if (mounted && _active && (_job?.pending ?? false)) setState(() {});
    });
    unawaited(_load());
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    _active = state == AppLifecycleState.resumed;
    _poll?.cancel();
    if (_active) {
      unawaited(_load());
    } else {
      _generation++;
      _cancel?.cancel();
      _loading = false;
    }
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _generation++;
    _cancel?.cancel();
    _poll?.cancel();
    _clock?.cancel();
    super.dispose();
  }

  Future<void> _load() async {
    if (_loading || !_active) return;
    _poll?.cancel();
    final generation = ++_generation;
    _loading = true;
    final cancel = _cancel = CancelToken();
    try {
      final job = await ref
          .read(platformAccountsApiProvider)
          .job(widget.scope, widget.jobId, cancel: cancel);
      if (!mounted || generation != _generation) return;
      final changed = _job?.status != job.status;
      setState(() {
        _job = job;
        _error = null;
      });
      if (changed) widget.onChanged?.call();
    } catch (error) {
      if (!mounted ||
          generation != _generation ||
          (error is DioException && CancelToken.isCancel(error))) {
        return;
      }
      setState(() => _error = error);
    } finally {
      if (mounted && generation == _generation) {
        setState(() => _loading = false);
        if (_active && (_error != null || (_job?.pending ?? true))) {
          _poll = Timer(const Duration(seconds: 5), _load);
        }
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final i18n = ref.watch(i18nProvider);
    final t = context.tokens;
    final job = _job;
    final uri = douyinPublishUri(widget.result?.schema ?? '');
    final now = DateTime.now();
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Row(
          children: [
            Expanded(
              child: Text(
                i18n.t('auth-center:mobile.jobDetails'),
                style: TextStyle(fontWeight: FontWeight.w600, color: t.ink),
              ),
            ),
            IconButton(
              onPressed: _loading ? null : _load,
              tooltip: i18n.t('common:action.retry'),
              icon: const Icon(Icons.refresh),
            ),
          ],
        ),
        if (_error != null) ...[
          Text(
            platformErrorText(i18n, _error!),
            style: TextStyle(color: t.danger),
          ),
          Text(i18n.t('auth-center:mobile.statusUnconfirmed')),
        ],
        if (job == null && _error == null)
          const Center(child: CircularProgressIndicator()),
        if (job != null) ...[
          const SizedBox(height: 8),
          Align(
            alignment: Alignment.centerLeft,
            child: AuthStatusPill(job.status, job: true),
          ),
          const SizedBox(height: 8),
          if (job.title.isNotEmpty) Text(job.title),
          if (job.hashtags.isNotEmpty)
            Text(job.hashtags.map((h) => '#$h').join(' ')),
          if (job.status == 'published') ...[
            const SizedBox(height: 10),
            Icon(Icons.check_circle_outline, color: t.a700, size: 40),
            Text(
              i18n.t('auth-center:publish.done'),
              textAlign: TextAlign.center,
            ),
            if (job.itemId != null)
              SelectableText(
                i18n.t('auth-center:publish.itemId', vars: {'id': job.itemId}),
              ),
          ] else if (job.status == 'expired')
            Text(i18n.t('auth-center:publish.expiredHint'))
          else if (job.status == 'failed')
            Text(i18n.t('auth-center:mobile.failedHint'))
          else if (job.pending) ...[
            const SizedBox(height: 12),
            if (uri != null && _error == null)
              PlatformQr(
                uri: uri,
                valid: job.linkValidAt(now),
                name: 'douyin-${job.id}.png',
              )
            else
              Text(i18n.t('auth-center:mobile.resumeJobHint')),
            const SizedBox(height: 10),
            Text(i18n.t('auth-center:publish.waiting')),
            if (job.expiresAt != null)
              Text(
                i18n.t(
                  'auth-center:publish.expiresIn',
                  vars: {'time': _remaining(job.expiresAt!, now)},
                ),
              ),
            if (job.shareId == null)
              Text(i18n.t('auth-center:publish.noTracking')),
          ],
          const SizedBox(height: 12),
          SelectableText(job.id, style: TextStyle(fontSize: 12, color: t.n600)),
        ],
      ],
    );
  }
}

String _remaining(DateTime until, DateTime now) {
  final seconds = until.difference(now).inSeconds.clamp(0, 86400);
  return '${seconds ~/ 60}:${(seconds % 60).toString().padLeft(2, '0')}';
}
