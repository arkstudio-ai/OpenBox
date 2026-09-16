import 'package:flutter/material.dart';
import 'package:integration_test/integration_test.dart';

import '../test/features/chat/question_scroll_test.dart' as cases;

/// Real iOS rendering with isolated HTTP/WS fixtures; no production writes.
void main() {
  final binding = IntegrationTestWidgetsFlutterBinding.ensureInitialized();
  cases.registerQuestionScrollTests(
    platforms: const [TargetPlatform.iOS],
    nativeViewport: true,
    screenshot: (name) async {
      await binding.takeScreenshot(name);
    },
  );
}
