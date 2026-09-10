import 'dart:async';
import 'dart:convert';
import 'dart:math';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'providers.dart';

/// Random app installation identity; no hardware or advertising identifier.
final installationIdProvider = Provider<String>((ref) {
  final prefs = ref.read(prefsProvider);
  const key = 'openbox:installation-id';
  final existing = prefs.getString(key);
  if (existing != null && existing.length >= 16) return existing;
  final random = Random.secure();
  final value = base64Url.encode(List.generate(24, (_) => random.nextInt(256)));
  unawaited(prefs.setString(key, value));
  return value;
});
