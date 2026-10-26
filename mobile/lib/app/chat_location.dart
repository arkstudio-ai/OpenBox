import 'dart:async';
import 'dart:convert';

import 'package:flutter/widgets.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../features/chat/api/assistant_api.dart';
import '../features/voice/state/voice_call_controller.dart';
import '../shared/api/providers.dart';
import '../shared/config/env.dart';
import '../shared/router/paths.dart';

/// A device bookmark, not a conversation snapshot. Authentication and workspace
/// membership must settle before reading it; the session entry still checks
/// access with the server before mounting any history.
class ChatLocationStore {
  ChatLocationStore(this.prefs);
  final SharedPreferences prefs;

  String _userKey(String userId) =>
      'bossip:last-chat:${jsonEncode([Env.apiBase, userId])}:';

  String _key(AssistantScope scope) =>
      '${_userKey(scope.userId)}${scope.workspaceId}';

  bool hasSavedChat(String userId) =>
      prefs.getKeys().any((key) => key.startsWith(_userKey(userId)));

  static String? chatPath(String location) {
    final uri = Uri.tryParse(location);
    if (uri == null || uri.hasScheme || uri.hasAuthority) return null;
    if (uri.path == Paths.app || uri.path == Paths.assistant) return uri.path;
    final segments = uri.pathSegments;
    if (segments.length == 3 &&
        segments[0] == 'app' &&
        segments[1] == 's' &&
        RegExp(r'^[A-Za-z0-9_-]+$').hasMatch(segments[2])) {
      return uri.path;
    }
    return null;
  }

  String? read(AssistantScope scope) {
    final stored = prefs.getString(_key(scope));
    return stored == null ? null : chatPath(stored);
  }

  Future<void> remember(AssistantScope scope, String location) async {
    final path = chatPath(location);
    if (path == null || read(scope) == path) return;
    await prefs.setString(_key(scope), path);
  }
}

final chatLocationStoreProvider = Provider<ChatLocationStore>(
  (ref) => ChatLocationStore(ref.read(prefsProvider)),
);

/// Only mounted, authorized chat pages record a location. A settings page,
/// voice call, task/result query or an unverified deep link is never saved as
/// the next launch destination. Popping a pushed chat records the visible
/// chat underneath it again.
class RememberChatLocation extends ConsumerStatefulWidget {
  const RememberChatLocation({
    super.key,
    required this.location,
    required this.child,
  });

  final String location;
  final Widget child;

  @override
  ConsumerState<RememberChatLocation> createState() =>
      _RememberChatLocationState();
}

class _RememberChatLocationState extends ConsumerState<RememberChatLocation> {
  GoRouter? _router;
  AssistantScope? _scope;
  VoiceCallController? _voice;
  String? _viewedSessionId;

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    final router = GoRouter.maybeOf(context);
    if (router == _router) return;
    _router?.routerDelegate.removeListener(_schedule);
    _router = router;
    router?.routerDelegate.addListener(_schedule);
  }

  void _schedule() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted) return;
      final scope = _scope;
      if (scope == null || ref.read(assistantScopeProvider) != scope) return;
      // A first-run sheet or dialog can be on top when the authorized chat
      // finishes loading. It must not suppress the bookmark. Compare the
      // router's top destination so a chat underneath another page still
      // cannot overwrite that page's saved position.
      final current = _router?.state.uri.path;
      final sessionId = Uri.parse(widget.location).pathSegments;
      final viewed =
          sessionId.length == 3 && sessionId[0] == 'app' && sessionId[1] == 's'
          ? sessionId[2]
          : null;
      final voice = ref.read(voiceCallControllerProvider.notifier);
      _voice = voice;
      if (current != widget.location) {
        // The full-screen call overlays the originating conversation. Other
        // destinations clear only this page's hint, not a newer chat's hint.
        if (current != Paths.voice && _viewedSessionId != null) {
          voice.clearViewedSession(_viewedSessionId!);
        }
        return;
      }
      _viewedSessionId = viewed;
      voice.setViewedSession(viewed);
      // Saving a navigation bookmark must never block the chat on disk errors.
      unawaited(
        ref
            .read(chatLocationStoreProvider)
            .remember(scope, widget.location)
            .catchError((Object _) {}),
      );
    });
  }

  @override
  void dispose() {
    _router?.routerDelegate.removeListener(_schedule);
    if (_viewedSessionId != null) _voice?.clearViewedSession(_viewedSessionId!);
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    _scope = ref.watch(assistantScopeProvider);
    _schedule();
    return widget.child;
  }
}
