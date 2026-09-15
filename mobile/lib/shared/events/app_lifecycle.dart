import 'dart:ui' show AppLifecycleState;

import 'package:flutter_riverpod/flutter_riverpod.dart';

/// Whether the app is on screen, kept by the app shell's lifecycle observer
/// (`NotificationHost`) through [appIsOnScreen].
///
/// Starts true, so a container without that observer — a test, a screen
/// pumped on its own — behaves as an app on screen always did. Work that only
/// matters to someone looking, like a live chat's catch-up poll, checks it.
final appVisibleProvider = StateProvider<bool>((ref) => true);

/// Resumed, or inactive while still showing: iOS launching, the notification
/// centre pulled down over the app, the app switcher, an Android dialog or a
/// split-screen neighbour holding focus. Hidden, paused and detached are off
/// screen.
bool appIsOnScreen(AppLifecycleState state) =>
    state == AppLifecycleState.resumed || state == AppLifecycleState.inactive;
