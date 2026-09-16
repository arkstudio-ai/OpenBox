import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/providers.dart';
import '../../../shared/models/app_config.dart';

/// Settings REST calls (web `features/settings/api/settings.ts`). Reads the
/// same `/api/agent/*` endpoints as chat but through its own client —
/// features never import each other.
class SettingsApi {
  SettingsApi(this._dio);

  final Dio _dio;

  Future<Map<String, dynamic>> getPreferences() async {
    final resp =
        await _dio.get<Map<String, dynamic>>('/api/auth/me/preferences');
    return resp.data ?? const {};
  }

  Future<void> updatePreferences(Map<String, dynamic> patch) async {
    await _dio.put<dynamic>('/api/auth/me/preferences', data: patch);
  }

  /// 视频发布 route (web `features/settings/api/publish.ts`):
  /// `{preference, deploymentDefault, effective, routes}`.
  Future<Map<String, dynamic>> getPublishRoute() async {
    final resp =
        await _dio.get<Map<String, dynamic>>('/api/publish/preference');
    return resp.data ?? const {};
  }

  /// `null` clears the choice back to the deployment default.
  Future<void> setPublishRoute(String? route) async {
    await _dio.put<dynamic>('/api/publish/preference', data: {'route': route});
  }

  Future<AppConfig> getConfig() async {
    final resp = await _dio.get<Map<String, dynamic>>('/api/agent/config');
    return AppConfig.fromJson(resp.data ?? const {});
  }

  Future<List<AgentInfo>> listAgents() async {
    final resp = await _dio.get<List<dynamic>>('/api/agent/agent');
    return (resp.data ?? const [])
        .whereType<Map<String, dynamic>>()
        .map(AgentInfo.fromJson)
        .toList();
  }
}

final settingsApiProvider =
    Provider<SettingsApi>((ref) => SettingsApi(ref.watch(apiDioProvider)));

final preferencesProvider = FutureProvider<Map<String, dynamic>>(
  (ref) => ref.watch(settingsApiProvider).getPreferences(),
);

final settingsConfigProvider = FutureProvider<AppConfig>(
  (ref) => ref.watch(settingsApiProvider).getConfig(),
);

final settingsAgentsProvider = FutureProvider<List<AgentInfo>>(
  (ref) => ref.watch(settingsApiProvider).listAgents(),
);

final publishRouteProvider = FutureProvider<Map<String, dynamic>>(
  (ref) => ref.watch(settingsApiProvider).getPublishRoute(),
);
