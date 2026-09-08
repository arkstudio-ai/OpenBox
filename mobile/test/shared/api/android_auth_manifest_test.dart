import 'dart:io';

import 'package:flutter_test/flutter_test.dart';

void main() {
  test('Logto callback has exactly one Android activity handler', () {
    final manifest = File(
      'android/app/src/main/AndroidManifest.xml',
    ).readAsStringSync();
    final activities = RegExp(
      r'<activity\b[\s\S]*?</activity>',
    ).allMatches(manifest).map((match) => match.group(0)!).toList();

    const scheme = 'android:scheme="com.bossip.bipmobile"';
    const callbackHost = 'android:host="callback"';
    final customSchemeHandlers = activities
        .where((activity) => activity.contains(scheme))
        .toList();
    final callbackHandlers = customSchemeHandlers
        .where((activity) => activity.contains(callbackHost))
        .toList();

    expect(callbackHandlers, hasLength(1));
    expect(
      callbackHandlers.single,
      contains('com.linusu.flutter_web_auth_2.CallbackActivity'),
    );
    // Android ignores path/pathPrefix when an intent filter has no host. Such
    // a filter would therefore also match ://callback and recreate the chooser.
    expect(
      customSchemeHandlers.every(
        (activity) => activity.contains(RegExp(r'android:host="[^"]+"')),
      ),
      isTrue,
    );
    expect(
      manifest,
      contains(
        'android:scheme="https" android:host="ai.bossipai.com.cn" '
        'android:pathPrefix="/invite/"',
      ),
    );
  });
}
