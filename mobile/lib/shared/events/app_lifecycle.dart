import 'package:flutter_riverpod/flutter_riverpod.dart';

/// Whether the app is in the foreground (`AppLifecycleState.resumed`), kept
/// by the app shell's lifecycle observer (`NotificationHost`).
///
/// Starts true, so a container without that observer — a test, a screen
/// pumped on its own — behaves as a foreground app always did. Work that only
/// matters to someone looking, like a live chat's catch-up poll, checks it.
final appResumedProvider = StateProvider<bool>((ref) => true);
