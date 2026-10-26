import 'package:flutter_test/flutter_test.dart';
import 'package:bossip_mobile/features/chat/state/assistant_watch.dart';

void main() {
  test('a recovered task keeps its earlier result without showing stale failure', () {
    final result = <String, dynamic>{
      'outcome': 'error', 'summary': 'The old attachment failed.', 'is_current': false,
    };
    final item = AssistantWatchItem({'observed_state': 'waiting_input', 'latest_result': result});
    expect(item.summary, isNull);
    expect(item.latestResult['summary'], 'The old attachment failed.');
    result['is_current'] = true;
    expect(item.summary, 'The old attachment failed.');
  });
}
