import 'dart:async';

import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../features/workspace/state/active_workspace_store.dart';
import '../shared/api/auth_store.dart';
import '../shared/api/providers.dart';
import '../shared/i18n/i18n.dart';
import '../shared/notifications/push_controller.dart';
import '../shared/notifications/system_notifications.dart';
import '../shared/router/paths.dart';
import '../shared/widgets/toast.dart';
import 'router.dart';

/// Composition layer: authenticated push, workspace restore and navigation.
class NotificationHost extends ConsumerStatefulWidget {
  const NotificationHost({super.key, required this.child});
  final Widget child;
  @override
  ConsumerState<NotificationHost> createState() => _NotificationHostState();
}

class _NotificationHostState extends ConsumerState<NotificationHost>
    with WidgetsBindingObserver {
  StreamSubscription<NativeNotificationEvent>? _events;
  late final Listenable _routeInformation;
  Timer? _heartbeat;
  AppLifecycleState _lifecycle = AppLifecycleState.detached;
  (String?, String?) _identity = (null, null);
  Map<String, dynamic>? _pending;
  bool _opening = false;
  final _seen = <String>{};

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _lifecycle =
        WidgetsBinding.instance.lifecycleState ?? AppLifecycleState.detached;
    final native = ref.read(systemNotificationsProvider);
    _events = native.events.listen(_handle);
    _routeInformation = ref.read(routerProvider).routeInformationProvider;
    _routeInformation.addListener(_routeChanged);
    _heartbeat = Timer.periodic(const Duration(seconds: 15), (_) {
      if (_lifecycle == AppLifecycleState.resumed ||
          _lifecycle == AppLifecycleState.inactive) {
        final push = ref.read(pushControllerProvider);
        unawaited(push.reportPresence());
        unawaited(push.checkSession());
      }
    });
    WidgetsBinding.instance.addPostFrameCallback((_) async {
      if (!mounted) return;
      _syncIdentity();
      await native.refresh();
      if (mounted) unawaited(_openPending());
    });
  }

  void _syncIdentity() {
    final auth = ref.read(authProvider);
    final next = (auth.user?.id, auth.mobileSessionId);
    if (_identity.$1 != null && _identity != next) {
      _pending = null;
      _seen.clear();
    }
    _identity = next;
    final push = ref.read(pushControllerProvider);
    push.setLifecycle(_lifecycle.name);
    push.setIdentity(next.$1, next.$2);
    _routeChanged();
  }

  String? get _activeSession {
    final segments = ref
        .read(routerProvider)
        .routeInformationProvider
        .value
        .uri
        .pathSegments;
    return segments.length == 3 && segments[0] == 'app' && segments[1] == 's'
        ? segments[2]
        : null;
  }

  void _routeChanged() {
    if (!mounted) return;
    unawaited(
      ref.read(systemNotificationsProvider).setContext({
        'appLifecycle': _lifecycle.name,
        'section': _activeSession == null ? 'other' : 'conversation',
        'activeSessionId': _activeSession ?? '',
        'userId': ref.read(authProvider).user?.id ?? '',
        'workspaceId': ref.read(workspaceScopeProvider).currentId ?? '',
        'bindingId': ref.read(pushControllerProvider).bindingId ?? '',
      }),
    );
    unawaited(_openPending());
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    _lifecycle = state;
    final push = ref.read(pushControllerProvider);
    push.setLifecycle(state.name);
    _routeChanged();
    if (state == AppLifecycleState.resumed) unawaited(push.checkSession());
  }

  void _handle(NativeNotificationEvent event) {
    if (!mounted) return;
    final p = event.payload;
    if (p['source'] != 'openbox' ||
        p['schemaVersion'] != 1 ||
        p['eventId'] is! String) {
      return;
    }
    final key = '${event.opened}:${p['eventId']}';
    if (!_seen.add(key)) return;
    if (_seen.length > 512) _seen.remove(_seen.first);
    if (event.opened) {
      _pending = p;
      unawaited(_openPending());
      return;
    }
    if (p['type'] == 'system_test') {
      unawaited(_testReceipt(p, 'received'));
      return;
    }
    final push = ref.read(pushControllerProvider);
    if (p['recipientId'] != ref.read(authProvider).user?.id ||
        (push.bindingId != null && p['bindingId'] != push.bindingId) ||
        _lifecycle != AppLifecycleState.resumed) {
      return;
    }
    if (p['sessionId'] != null &&
        p['sessionId'] == _activeSession &&
        p['workspaceId'] == ref.read(workspaceScopeProvider).currentId) {
      ref
          .read(toastProvider.notifier)
          .push(
            ToastKind.info,
            p['body']?.toString() ?? '',
            title: p['title']?.toString(),
          );
    }
  }

  Future<bool> _testReceipt(Map<String, dynamic> payload, String kind) async {
    final auth = ref.read(authProvider);
    final identity = (auth.user?.id, auth.mobileSessionId);
    bool current() {
      if (!mounted) return false;
      final latest = ref.read(authProvider);
      return latest.user?.role == 'admin' &&
          latest.isAuthenticated &&
          latest.user?.id == payload['recipientId'] &&
          identity == (latest.user?.id, latest.mobileSessionId);
    }

    if (!current()) return false;
    final id = payload['eventId'] as String;
    if (id.startsWith('local-')) return true;
    final push = ref.read(pushControllerProvider);
    if (push.bindingId == null) await push.checkSession();
    if (!current() ||
        push.bindingId == null ||
        payload['bindingId'] != push.bindingId) {
      return false;
    }
    try {
      await ref
          .read(apiDioProvider)
          .post<dynamic>(
            '/api/admin/push/messages/${Uri.encodeComponent(id)}/receipt',
            data: {'kind': kind},
          );
    } catch (_) {
      // Opening the diagnostics remains useful when receipt reporting is offline.
    }
    return current();
  }

  Future<void> _openPending() async {
    if (_opening || _pending == null || !mounted) return;
    final auth = ref.read(authProvider);
    if (!auth.isAuthenticated || auth.isLoading) return;
    final payload = _pending!;
    if (payload['recipientId'] != auth.user?.id) {
      _pending = null;
      return;
    }
    final identity = (auth.user?.id, auth.mobileSessionId);
    bool current() =>
        mounted &&
        identity ==
            (
              ref.read(authProvider).user?.id,
              ref.read(authProvider).mobileSessionId,
            );
    _opening = true;
    try {
      if (payload['type'] == 'system_test') {
        if (await _testReceipt(payload, 'opened') && current()) {
          ref.read(routerProvider).go(Paths.adminNotifications);
        }
        return;
      }
      final workspaceId = payload['workspaceId'];
      final sessionId = payload['sessionId'];
      if (workspaceId is! String) return;
      final workspaces = await ref.read(activeWorkspaceProvider.future);
      if (!current()) return;
      if (!workspaces.items.any((item) => item.id == workspaceId)) {
        throw StateError('Unavailable workspace');
      }
      if (sessionId is String && sessionId.isNotEmpty) {
        await ref
            .read(apiDioProvider)
            .get<dynamic>(
              '/api/agent/session/${Uri.encodeComponent(sessionId)}',
              options: Options(headers: {'X-Workspace-Id': workspaceId}),
            );
        if (!current()) return;
      }
      await ref.read(activeWorkspaceProvider.notifier).select(workspaceId);
      if (!current()) return;
      final type = payload['type']?.toString() ?? '';
      if (sessionId is String && sessionId.isNotEmpty) {
        ref.read(routerProvider).go(Paths.chat(sessionId));
      } else if (type.startsWith('cron_')) {
        ref.read(routerProvider).go(Paths.cron);
      } else if (type == 'platform_auth_expired' ||
          type.startsWith('publish_')) {
        ref.read(routerProvider).go(Paths.authCenter());
      }
    } catch (_) {
      if (current()) {
        ref
            .read(toastProvider.notifier)
            .push(
              ToastKind.warning,
              ref.read(i18nProvider).t('settings:notifications.unavailable'),
            );
      }
    } finally {
      if (identical(_pending, payload)) _pending = null;
      _opening = false;
      if (mounted && _pending != null) unawaited(_openPending());
    }
  }

  @override
  Widget build(BuildContext context) {
    ref.listen(authProvider, (_, _) => _syncIdentity());
    ref.listen(pushControllerProvider, (_, _) => _routeChanged());
    ref.listen(activeWorkspaceProvider, (_, _) => _routeChanged());
    return widget.child;
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _routeInformation.removeListener(_routeChanged);
    _heartbeat?.cancel();
    unawaited(_events?.cancel());
    super.dispose();
  }
}
