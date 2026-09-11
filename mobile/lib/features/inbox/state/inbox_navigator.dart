import 'dart:async';

import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:url_launcher/url_launcher.dart';

import '../../../shared/api/providers.dart';
import '../../../shared/models/inbox.dart';
import '../../../shared/router/paths.dart';
import '../../workspace/state/active_workspace_store.dart';

typedef Reader = T Function<T>(ProviderListenable<T> provider);

/// What happened to a tap.
enum InboxOpen { opened, unavailable }

/// Turns an allow-listed inbox `link` into navigation (docs/MESSAGE_CENTER.md
/// §link). Shared by the inbox list and the push-tap path in
/// `NotificationHost`, so both validate the same way: confirm the user still
/// belongs to the target workspace (and can still see the session), switch
/// scope, then route. Unknown kinds land in the inbox itself; nothing here
/// ever opens a URL the server did not vet.
class InboxNavigator {
  const InboxNavigator(this._read, this.router);

  final Reader _read;
  final GoRouter router;

  Future<InboxOpen> open(
    InboxLink? link, {
    bool Function()? stillCurrent,
  }) async {
    bool current() => stillCurrent?.call() ?? true;
    if (link == null) {
      unawaited(router.push(Paths.inbox));
      return InboxOpen.opened;
    }
    switch (link.kind) {
      case 'url':
        final uri = Uri.tryParse(link.url ?? '');
        if (uri == null || uri.scheme != 'https') return InboxOpen.unavailable;
        final ok = await launchUrl(uri, mode: LaunchMode.externalApplication);
        return ok ? InboxOpen.opened : InboxOpen.unavailable;
      case 'topic':
        final slug = link.slug ?? '';
        if (slug.isEmpty) return InboxOpen.unavailable;
        unawaited(router.push(Paths.topic(slug)));
        return InboxOpen.opened;
      case 'admin_skills':
        unawaited(router.push('${Paths.admin}/skills'));
        return InboxOpen.opened;
      case 'session':
      case 'cron':
      case 'auth_center':
      case 'skills':
        return _openInWorkspace(link, current);
      default:
        unawaited(router.push(Paths.inbox));
        return InboxOpen.opened;
    }
  }

  Future<InboxOpen> _openInWorkspace(
    InboxLink link,
    bool Function() current,
  ) async {
    final workspaceId = link.workspaceId;
    if (workspaceId == null || workspaceId.isEmpty) {
      return InboxOpen.unavailable;
    }
    try {
      final workspaces = await _read(activeWorkspaceProvider.future);
      if (!current()) return InboxOpen.unavailable;
      if (!workspaces.items.any((item) => item.id == workspaceId)) {
        return InboxOpen.unavailable;
      }
      final sessionId = link.sessionId;
      if (link.kind == 'session') {
        if (sessionId == null || sessionId.isEmpty) {
          return InboxOpen.unavailable;
        }
        await _read(apiDioProvider).get<dynamic>(
          '/api/agent/session/${Uri.encodeComponent(sessionId)}',
          options: Options(headers: {'X-Workspace-Id': workspaceId}),
        );
        if (!current()) return InboxOpen.unavailable;
      }
      if (workspaces.currentId != workspaceId) {
        await _read(activeWorkspaceProvider.notifier).select(workspaceId);
        if (!current()) return InboxOpen.unavailable;
      }
    } catch (_) {
      return InboxOpen.unavailable;
    }
    switch (link.kind) {
      case 'session':
        // Same shape as a takeover card: the chat, optionally with the
        // desktop panel and input control on.
        if (link.panel == 'desktop') {
          router.go(Paths.chat(link.sessionId!));
          unawaited(
            router.push(
              Paths.workbench(
                link.sessionId!,
                tab: 'desktop',
                control: link.control,
              ),
            ),
          );
        } else {
          router.go(Paths.chat(link.sessionId!));
        }
      case 'cron':
        unawaited(router.push(Paths.cron));
      case 'auth_center':
        unawaited(router.push(Paths.authCenter(jobId: link.jobId)));
      case 'skills':
        unawaited(router.push(Paths.skills));
    }
    return InboxOpen.opened;
  }
}
