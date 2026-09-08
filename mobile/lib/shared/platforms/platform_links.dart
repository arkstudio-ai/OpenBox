import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:url_launcher/url_launcher.dart';

/// Only server-provided Douyin capabilities are accepted by platform buttons.
/// These are never sent through the authenticated API HTTP client.
Uri? douyinAuthorizationUri(String value) {
  final uri = Uri.tryParse(value);
  if (uri == null ||
      uri.scheme != 'https' ||
      uri.host != 'open.douyin.com' ||
      uri.userInfo.isNotEmpty ||
      uri.hasPort ||
      uri.path != '/platform/oauth/connect/' ||
      (uri.queryParameters['state']?.isNotEmpty != true)) {
    return null;
  }
  return uri.replace(
    queryParameters: {...uri.queryParameters, 'is_call_app': '1'},
  );
}

Uri? douyinPublishUri(String value) {
  final uri = Uri.tryParse(value);
  if (uri == null || uri.userInfo.isNotEmpty || uri.hasPort) return null;
  if (uri.scheme == 'snssdk1128' &&
      ((uri.host == 'openplatform' && uri.path == '/share') ||
          uri.host == 'webview')) {
    return uri;
  }
  if (uri.scheme == 'https' &&
      const {
        'open.douyin.com',
        'www.douyin.com',
        'aweme.snssdk.com',
      }.contains(uri.host)) {
    return uri;
  }
  return null;
}

class PlatformLinkLauncher {
  Future<bool> open(Uri uri) =>
      launchUrl(uri, mode: LaunchMode.externalApplication);
}

final platformLinkLauncherProvider = Provider<PlatformLinkLauncher>(
  (ref) => PlatformLinkLauncher(),
);
