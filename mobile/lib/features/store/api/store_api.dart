import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/providers.dart';
import '../models/store.dart';

/// `/api/stores` transport (docs/OPS_CASE_PLAN.md §6.2). The workspace scope
/// rides on the shared Dio's `X-Workspace-Id` header.
class StoreApi {
  StoreApi(this._dio);

  final Dio _dio;

  Future<StoreSnapshot> list({CancelToken? cancel}) async {
    final resp = await _dio.get<Map<String, dynamic>>(
      '/api/stores',
      cancelToken: cancel,
    );
    return StoreSnapshot.fromJson(resp.data ?? const {});
  }

  Future<Store> create({
    required String name,
    required String category,
    required List<String> mainPlatforms,
  }) async {
    final resp = await _dio.post<Map<String, dynamic>>(
      '/api/stores',
      data: {
        'name': name,
        'category': category,
        'main_platforms': mainPlatforms,
      },
    );
    return Store.fromJson(resp.data ?? const {});
  }

  Future<Store> patch(
    String id, {
    String? name,
    String? category,
    List<String>? mainPlatforms,
  }) async {
    final resp = await _dio.patch<Map<String, dynamic>>(
      '/api/stores/${Uri.encodeComponent(id)}',
      data: {
        'name': ?name,
        'category': ?category,
        'main_platforms': ?mainPlatforms,
      },
    );
    return Store.fromJson(resp.data ?? const {});
  }

  Future<List<StarterCard>> starterCards(
    String id, {
    required String locale,
    CancelToken? cancel,
  }) async {
    final resp = await _dio.get<Map<String, dynamic>>(
      '/api/stores/${Uri.encodeComponent(id)}/starter-cards',
      queryParameters: {'locale': locale},
      cancelToken: cancel,
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
