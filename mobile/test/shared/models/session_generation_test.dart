import 'package:bossip_mobile/shared/models/session.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  group('acceptsEventGeneration', () {
    test('rejects an old terminal event after a newer generation', () {
      expect(
        acceptsEventGeneration(2, 1, rejectLegacyAfterSeen: true),
        isFalse,
      );
      expect(
        acceptsEventGeneration(2, null, rejectLegacyAfterSeen: true),
        isFalse,
      );
      expect(acceptsEventGeneration(2, 2, rejectLegacyAfterSeen: true), isTrue);
      expect(acceptsEventGeneration(2, 3, rejectLegacyAfterSeen: true), isTrue);
    });

    test('keeps unversioned transcript events compatible', () {
      expect(acceptsEventGeneration(2, null), isTrue);
      expect(acceptsEventGeneration(2, 1), isFalse);
    });
  });
}
