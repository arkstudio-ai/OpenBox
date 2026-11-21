import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_timezone/flutter_timezone.dart';

final deviceTimezoneProvider = FutureProvider<String?>((ref) async {
  try {
    final zone = await FlutterTimezone.getLocalTimezone().timeout(
      const Duration(seconds: 2),
    );
    return zone.identifier.isEmpty ? null : zone.identifier;
  } catch (_) {
    return null;
  }
});
