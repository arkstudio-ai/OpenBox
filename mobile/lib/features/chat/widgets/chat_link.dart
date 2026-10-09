import 'dart:async';

import 'package:flutter/widgets.dart';
import 'package:go_router/go_router.dart';
import 'package:url_launcher/url_launcher.dart';

import '../../../shared/config/env.dart';
import '../../../shared/router/paths.dart';

/// Assistant replies use the same conversation links as the web app. Only
/// first-party links may become native routes; a lookalike host stays external.
String? conversationRoute(String url) {
  final uri = Uri.tryParse(url.trim());
  if (uri == null || uri.userInfo.isNotEmpty) return null;
  if (uri.hasScheme || uri.hasAuthority) {
    if (uri.scheme != 'http' && uri.scheme != 'https') return null;
    final origins = [Env.webBase, Env.apiBase, 'https://ai.bossipai.com.cn'];
    if (!origins.any((origin) => Uri.parse(origin).origin == uri.origin)) {
      return null;
    }
  }
  final segments = uri.pathSegments;
  if (segments.length != 3 || segments[0] != 'app' || segments[1] != 's') {
    return null;
  }
  final id = segments[2];
  if (!RegExp(r'^[a-zA-Z0-9_-]+$').hasMatch(id)) return null;
  return Uri(
    path: Paths.chat(id),
    query: uri.hasQuery ? uri.query : null,
    fragment: uri.hasFragment ? uri.fragment : null,
  ).toString();
}

Future<void> openChatLink(BuildContext context, String url) async {
  final route = conversationRoute(url);
  if (route != null) {
    unawaited(context.push(route));
    return;
  }
  final uri = Uri.tryParse(url.trim());
  if (uri == null) return;
  final target = uri.hasScheme ? uri : Uri.parse(Env.webBase).resolveUri(uri);
  if (!const {'http', 'https', 'mailto', 'tel'}.contains(target.scheme)) return;
  try {
    await launchUrl(target, mode: LaunchMode.externalApplication);
  } catch (_) {
    // An unavailable external handler must not break the conversation.
  }
}
