import 'package:bossip_mobile/features/chat/state/pending_store.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  group('interactionReplyRequestId', () {
    test('reads the compatibility request_id field first', () {
      expect(
        interactionReplyRequestId({'id': 'new-id', 'request_id': 'compat-id'}),
        'compat-id',
      );
    });

    test('keeps id-only events from older backend workers working', () {
      expect(interactionReplyRequestId({'id': 'legacy-id'}), 'legacy-id');
    });

    test('keeps request_id-only events working', () {
      expect(
        interactionReplyRequestId({'request_id': 'request-id'}),
        'request-id',
      );
    });
  });
}
