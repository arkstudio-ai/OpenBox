import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../../shared/api/platform_accounts_api.dart';
import '../../../../shared/api/providers.dart';
import '../../../../shared/i18n/i18n.dart';
import '../../../../shared/models/json.dart';
import '../../../../shared/models/message_part.dart';
import '../../../../shared/platforms/platform_links.dart';
import '../../../../shared/router/paths.dart';
import '../../../../shared/widgets/toast.dart';

/// Old messages still display their QR image and can inspect the server job.
/// New metadata optionally adds a time-bounded same-device launch button.
class DouyinToolActions extends ConsumerStatefulWidget {
  const DouyinToolActions({super.key, required this.part});
  final ToolPart part;
  @override
  ConsumerState<DouyinToolActions> createState() => _DouyinToolActionsState();
}

class _DouyinToolActionsState extends ConsumerState<DouyinToolActions> {
  bool _busy = false;
  String? get _jobId =>
      asString(widget.part.metadata['job_id']) ??
      asString(widget.part.metadata['id']);
  DateTime? get _expires =>
      DateTime.tryParse('${widget.part.metadata['expiresAt'] ?? ''}');
  bool get _valid => _expires?.isAfter(DateTime.now()) ?? false;
  Uri? get _uri => _jobId != null
      ? douyinPublishUri('${widget.part.metadata['launchUrl'] ?? ''}')
      : douyinAuthorizationUri('${widget.part.metadata['authorizeUrl'] ?? ''}');

  Future<void> _open() async {
    final uri = _uri;
    if (_busy || !_valid || uri == null) return;
    final userId = ref.read(authSessionProvider).userId;
    final workspaceId = ref.read(workspaceScopeProvider).currentId;
    if (userId == null || workspaceId == null) return;
    final scope = (userId: userId, workspaceId: workspaceId);
    final api = ref.read(platformAccountsApiProvider);
    final launcher = ref.read(platformLinkLauncherProvider);
    setState(() => _busy = true);
    try {
      if (_jobId != null) {
        final job = await api.job(scope, _jobId!);
        if (!mounted || !job.linkValidAt(DateTime.now())) return;
      }
      if (!mounted || !_valid) return;
      api.checkScope(scope);
      final opened = await launcher.open(uri);
      if (mounted) {
        ref
            .read(toastProvider.notifier)
            .info(
              ref
                  .read(i18nProvider)
                  .t(
                    opened
                        ? (_jobId == null
                              ? 'auth-center:mobile.authorizeReturnHint'
                              : 'auth-center:mobile.returnHint')
                        : 'auth-center:mobile.launchFailed',
                  ),
            );
      }
    } catch (_) {
      if (mounted) {
        ref
            .read(toastProvider.notifier)
            .error(ref.read(i18nProvider).t('auth-center:mobile.launchFailed'));
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final i18n = ref.watch(i18nProvider);
    return Wrap(
      spacing: 8,
      children: [
        OutlinedButton(
          onPressed: () => context.push(Paths.authCenter(jobId: _jobId)),
          child: Text(
            i18n.t(
              _jobId == null
                  ? 'auth-center:page.title'
                  : 'auth-center:mobile.jobDetails',
            ),
          ),
        ),
        if (_valid && _uri != null && widget.part.metadata['error'] != true)
          FilledButton(
            onPressed: _busy ? null : _open,
            child: Text(
              i18n.t(
                _jobId == null
                    ? 'auth-center:mobile.openAuthorize'
                    : 'auth-center:mobile.openDouyin',
              ),
            ),
          ),
      ],
    );
  }
}
