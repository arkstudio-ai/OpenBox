import 'dart:async';

import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:url_launcher/url_launcher.dart';

import '../../../shared/api/auth_store.dart';
import '../../../shared/api/providers.dart';
import '../../../shared/api/ui_mode_store.dart';
import '../../../shared/api/workspace_scope.dart';
import '../../../shared/config/env.dart';
import '../../../shared/models/first_video.dart';
import '../../../shared/models/inbox.dart';
import '../../../shared/models/json.dart';
import '../../../shared/models/ui_mode.dart';
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
///
/// [kind] is the row's notification kind: a first-video notice carries a
/// `session` link but lands on the result page, not in that chat.
class InboxNavigator {
  const InboxNavigator(this._read, this.router);

  final Reader _read;
  final GoRouter router;

  Future<InboxOpen> open(
    InboxLink? link, {
    String? kind,
    bool Function()? stillCurrent,
  }) async {
    final actor = _read(authProvider).user?.id;
    bool current() =>
        actor == _read(authProvider).user?.id && (stillCurrent?.call() ?? true);
    if (!current()) return InboxOpen.unavailable;
    if (link == null) return _openInbox();
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
      case 'admin_trajectory_turn':
        return _openTrajectoryTurn(link);
      case 'session':
      case 'assistant_task':
      case 'cron':
      case 'auth_center':
      case 'skills':
      case 'media_templates':
        return _openInWorkspace(link, current, kind: kind);
      default:
        return _openInbox();
    }
  }

  InboxOpen _openInbox() {
    final matches = router.routerDelegate.currentConfiguration.matches;
    final current = matches.isEmpty
        ? router.routeInformationProvider.value.uri
        : router.state.uri;
    if (current.path != Paths.inbox && current.path != Paths.lightInbox) {
      final light = _read(uiModeProvider).mode == UiMode.light;
      unawaited(router.push(light ? Paths.lightInbox : Paths.inbox));
    }
    return InboxOpen.opened;
  }

  Future<InboxOpen> _openTrajectoryTurn(InboxLink link) async {
    final userId = link.userId;
    final sessionId = link.sessionId;
    final turnId = link.turnId;
    if (_read(authProvider).user?.role != 'admin' ||
        [userId, sessionId, turnId].any((id) => id == null || id.isEmpty)) {
      return InboxOpen.unavailable;
    }
    final path = Paths.adminTrajectoryTurn(userId!, sessionId!, turnId!);
    try {
      final url = Uri.parse(Env.webBase).resolve(path);
      final opened = await launchUrl(url, mode: LaunchMode.inAppBrowserView);
      return opened ? InboxOpen.opened : InboxOpen.unavailable;
    } catch (_) {
      return InboxOpen.unavailable;
    }
  }

  Future<InboxOpen> _openInWorkspace(
    InboxLink link,
    bool Function() current, {
    String? kind,
  }) async {
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
      if (link.kind == 'assistant_task') {
        if ((link.taskId ?? '').isEmpty || (link.resultId ?? '').isEmpty) {
          return InboxOpen.unavailable;
        }
        final response = await _read(apiDioProvider).get<Map<String, dynamic>>(
          '/api/assistant/results/${Uri.encodeComponent(link.resultId!)}/target',
          options: Options(
            headers: {'X-Workspace-Id': workspaceId},
            extra: {
              requestScopeUserKey: _read(authProvider).user?.id,
              requestScopeWorkspaceKey: workspaceId,
            },
          ),
        );
        if (!current() ||
            asMap(asMap(response.data?['task'])['task'])['id'] != link.taskId) {
          return InboxOpen.unavailable;
        }
      }
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
      case 'assistant_task':
        router.go(
          Uri(
            path: Paths.assistant,
            queryParameters: {'task': link.taskId!, 'result': link.resultId!},
          ).toString(),
        );
      case 'session':
        if (firstVideoNotificationKinds.contains(kind)) {
          // The finished (or failed) first video: its own result page,
          // which re-reads the job rather than trusting the push.
          router.go(Paths.firstVideoResult);
          return InboxOpen.opened;
        }
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
        } else if (_read(uiModeProvider).mode == UiMode.light) {
          // Light: the conversation on its topic page, where a result keeps
          // its save / publish / 省心托管 actions (a 托管 run lands here).
          router.go(Paths.lightChat(link.sessionId!));
        } else {
          router.go(Paths.chat(link.sessionId!));
        }
      case 'cron':
        unawaited(router.push(Paths.cron));
      case 'auth_center':
        unawaited(router.push(Paths.authCenter(jobId: link.jobId)));
      case 'skills':
        unawaited(router.push(Paths.skills));
      case 'media_templates':
        unawaited(
          router.push(
            Uri(
              path: Paths.lightMediaTemplates,
              queryParameters: {'template': ?link.jobId},
            ).toString(),
          ),
        );
    }
    return InboxOpen.opened;
  }
}
