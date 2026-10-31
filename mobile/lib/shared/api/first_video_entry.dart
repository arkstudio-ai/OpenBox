import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../models/first_video.dart';

/// Where the sign-in that just completed should land with respect to the
/// first-video flow. `AuthFlow` sets it from the account preferences before
/// publishing the session, so the router's auth guard — which fires on that
/// publish — already knows. Only consulted on the sign-in pages; the flow
/// resets it once the person is through or has skipped.
final firstVideoEntryProvider = StateProvider<FirstVideoEntry>(
  (_) => FirstVideoEntry.settled,
);
