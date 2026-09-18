import 'package:integration_test/integration_test.dart';

import '../test/features/chat/compaction_widget_test.dart' as cases;

/// Real native rendering of isolated compaction fixtures; no remote services.
void main() {
  IntegrationTestWidgetsFlutterBinding.ensureInitialized();
  cases.main();
}
