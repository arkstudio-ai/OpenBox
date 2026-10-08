import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../i18n/i18n.dart';
import '../ws/ws_client.dart';
import 'providers.dart';

/// The name the person gave their assistant (web `useAppearanceStore.assistantName`,
/// backend `assistant/identity.py`): `GET/PUT /api/assistant/name`. "" is the
/// default name the UI translates (`workspace:assistant`).
class AssistantNameApi {
  AssistantNameApi(this._dio);

  final Dio _dio;

  Future<String> get() async {
    final resp = await _dio.get<Map<String, dynamic>>('/api/assistant/name');
    return (resp.data?['name'] as String?) ?? '';
  }

  /// Saves the name (the server trims and quotes it, up to 20 characters) and
  /// returns what it kept; an empty name restores the default.
  Future<String> set(String name) async {
    final resp = await _dio.put<Map<String, dynamic>>(
      '/api/assistant/name',
      data: {'name': name},
    );
    return (resp.data?['name'] as String?) ?? '';
  }
}

final assistantNameApiProvider = Provider<AssistantNameApi>(
  (ref) => AssistantNameApi(ref.watch(apiDioProvider)),
);

/// Pushed when the name changes (Settings on any device, or the assistant
/// renaming itself in chat); `__connected` reads it again in case one was missed.
const assistantRenamedEvent = 'assistant.renamed';

class AssistantNameNotifier extends AsyncNotifier<String> {
  @override
  Future<String> build() {
    try {
      final sub = ref.watch(wsClientProvider).events.listen((event) {
        if (event.type == assistantRenamedEvent) {
          final name = event.data['name'];
          if (name is String) state = AsyncData(name);
        } else if (event.type == '__connected') {
          ref.invalidateSelf();
        }
      });
      ref.onDispose(sub.cancel);
    } catch (_) {
      // No socket here (early startup, a test): the name still loads; it
      // just does not follow a rename made elsewhere until the next read.
    }
    return ref.watch(assistantNameApiProvider).get();
  }

  Future<String> rename(String name) async {
    final kept = await ref.read(assistantNameApiProvider).set(name);
    state = AsyncData(kept);
    return kept;
  }
}

final assistantNameProvider =
    AsyncNotifierProvider<AssistantNameNotifier, String>(
      AssistantNameNotifier.new,
    );

/// What to call the assistant on screen: the chosen name, else the translated default.
String assistantLabel(WidgetRef ref) {
  final name = ref.watch(assistantNameProvider).valueOrNull ?? '';
  return name.isNotEmpty
      ? name
      : ref.watch(i18nProvider).t('workspace:assistant');
}
