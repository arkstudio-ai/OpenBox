import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../shared/api/auth_store.dart';
import '../shared/api/providers.dart';
import '../shared/i18n/device_timezone.dart';
import '../shared/i18n/i18n.dart';

// Account preferences serve jobs that run without a phone request.
final devicePreferencesProvider = FutureProvider.autoDispose<void>((ref) async {
  final user = ref.watch(authProvider.select((state) => state.user?.id));
  final language = ref.watch(i18nProvider.select((state) => state.language));
  final auth = ref.watch(authSessionProvider);
  final revision = auth.revision;
  if (user == null) return;
  final cancel = CancelToken();
  ref.onDispose(cancel.cancel);
  final api = ref.watch(apiDioProvider);
  final timezone = await ref.watch(deviceTimezoneProvider.future);
  if (cancel.isCancelled || auth.userId != user || auth.revision != revision) {
    return;
  }
  try {
    await api.put<dynamic>(
      '/api/auth/me/preferences',
      data: {
        'ui_language': language,
        if (timezone != null) 'extra': {'timezone': timezone},
      },
      cancelToken: cancel,
    );
  } on DioException {
    // Foreground requests carry their own language and timezone.
  }
});
