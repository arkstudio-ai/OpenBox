import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/providers.dart';
import '../models/store.dart';

/// `/api/stores` reads. The workspace scope rides on the shared Dio's
/// `X-Workspace-Id` header.
class StoreApi {
  StoreApi(this._dio);

  final Dio _dio;

  /// The workspace's store, at most one.
  Future<Store?> current() async {
    final resp = await _dio.get<Map<String, dynamic>>('/api/stores');
    final items = resp.data?['items'];
    if (items is! List || items.isEmpty) return null;
    final first = items.first;
    return first is Map<String, dynamic> ? Store.fromJson(first) : null;
  }

  Future<List<StarterCard>> starterCards(
    String id, {
    required String locale,
  }) async {
    final resp = await _dio.get<Map<String, dynamic>>(
      '/api/stores/${Uri.encodeComponent(id)}/starter-cards',
      queryParameters: {'locale': locale},
    );
    final items = resp.data?['items'];
    return [
      if (items is List)
        for (final item in items)
          if (item is Map<String, dynamic>) StarterCard.fromJson(item),
    ];
  }
}

final storeApiProvider = Provider<StoreApi>(
  (ref) => StoreApi(ref.watch(apiDioProvider)),
);
