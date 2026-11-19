import 'dart:math';

import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/providers.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/json.dart';
import '../../../shared/models/resource.dart';

enum ProductionKind {
  video('video'),
  footage('footage_edit'),
  menu('menu');

  const ProductionKind(this.id);
  final String id;

  static ProductionKind? fromId(String? id) =>
      values.where((kind) => kind.id == id).firstOrNull;

  String label(I18nState i18n) => i18n.t('light:production.kind.$id');

  List<String> get templates => switch (this) {
    video => ['soda_story', 'silk_road', 'wan_story', 'wok'],
    footage => ['documentary', 'quick_cut', 'product'],
    menu => ['warm_red', 'monochrome'],
  };
}

class ProductionMedia {
  const ProductionMedia({
    required this.id,
    required this.name,
    required this.mime,
    this.thumbnail,
  });

  final String id;
  final String name;
  final String mime;
  final String? thumbnail;

  bool get video => mime.startsWith('video/');

  factory ProductionMedia.fromJson(Map<String, dynamic> json) =>
      ProductionMedia(
        id: asString(json['id']) ?? '',
        name: asString(json['name']) ?? '',
        mime: asString(json['mime']) ?? '',
        thumbnail: asString(json['thumbnail']),
      );

  factory ProductionMedia.fromResource(Resource resource) => ProductionMedia(
    id: resource.id,
    name: resource.name,
    mime: resource.mime,
    thumbnail: resource.kind == 'image' ? resource.url : null,
  );
}

class ProductionDish {
  const ProductionDish(this.name, this.price);

  final String name;
  final String price;

  factory ProductionDish.fromJson(Map<String, dynamic> json) => ProductionDish(
    asString(json['name']) ?? '',
    asString(json['price']) ?? '',
  );

  Map<String, String> toJson() => {'name': name, 'price': price};
}

class ProductionShotProgress {
  const ProductionShotProgress(
    this.total,
    this.filmed,
    this.reviewed,
    this.retaking,
    this.phase,
  );

  final int total, filmed, reviewed, retaking;
  final String? phase;

  static ProductionShotProgress? parse(Object? value) {
    final data = asMap(value);
    final total = asInt(data['total']) ?? 0;
    if (total <= 0) return null;
    return ProductionShotProgress(
      total,
      (asInt(data['filmed']) ?? 0).clamp(0, total),
      (asInt(data['reviewed']) ?? 0).clamp(0, total),
      (asInt(data['retaking']) ?? 0).clamp(0, total),
      asString(data['phase']),
    );
  }
}

class LightProduction {
  LightProduction.fromJson(Map<String, dynamic> json)
    : sessionId = asString(json['session_id']) ?? '',
      kind =
          ProductionKind.fromId(asString(json['entry_id'])) ??
          ProductionKind.video,
      title = asString(json['title']) ?? '',
      legacy = json['legacy'] == true,
      revision = asInt(json['revision']) ?? 1,
      page = asString(json['page']) ?? 'materials',
      status = asString(json['status']) ?? 'draft',
      phase = asString(json['phase']),
      shotProgress = ProductionShotProgress.parse(json['shot_progress']),
      shop = asString(json['shop']) ?? '',
      brief = asString(json['brief']) ?? '',
      script = asString(json['script']) ?? '',
      referenceCopy = asString(json['reference_copy']) ?? '',
      remakePrepared = json['remake_prepared'] == true,
      template = asString(json['template']) ?? '',
      notes = asString(json['notes']) ?? '',
      agentRevision = asInt(json['agent_revision']) ?? 0,
      keepOriginal = json['keep_original'] != false,
      assets = asList(json['asset_ids']).whereType<String>().toList(),
      materials = [
        for (final item in asList(json['materials']))
          ProductionMedia.fromJson(asMap(item)),
      ],
      dishes = [
        for (final item in asList(json['dishes']))
          ProductionDish.fromJson(asMap(item)),
      ],
      output = json['output'] == null
          ? null
          : ProductionMedia.fromJson(asMap(json['output'])),
      proposal = json['proposal'] == null ? null : asMap(json['proposal']);

  final String sessionId;
  final ProductionKind kind;
  final String title;
  final bool legacy;
  final int revision;
  final String page;
  final String status;
  final String? phase;
  final ProductionShotProgress? shotProgress;
  final String shop;
  final String brief;
  final String script;
  final String referenceCopy;
  final bool remakePrepared;
  final String template;
  final String notes;
  final int agentRevision;
  final bool keepOriginal;
  final List<String> assets;
  final List<ProductionMedia> materials;
  final List<ProductionDish> dishes;
  final ProductionMedia? output;
  final Map<String, dynamic>? proposal;

  bool get working => status == 'working';
  bool get manualPreparation =>
      const ['soda_story', 'silk_road'].contains(template);
  bool get wanRemake => manualPreparation || template == 'wan_story';
  bool get remake =>
      kind == ProductionKind.video && (wanRemake || template == 'twist');
}

class LightProductionApi {
  LightProductionApi(this._dio);

  final Dio _dio;
  static const _path = '/api/light/productions';

  Future<LightProduction> create(ProductionKind kind, {String? brief}) async {
    final response = await _dio.post<Map<String, dynamic>>(
      _path,
      data: {'entry_id': kind.id, 'brief': ?brief},
    );
    return LightProduction.fromJson(response.data!);
  }

  Future<LightProduction> get(String sessionId) async {
    final response = await _dio.get<Map<String, dynamic>>('$_path/$sessionId');
    return LightProduction.fromJson(response.data!);
  }

  Future<LightProduction> edit(
    LightProduction task,
    Map<String, Object?> fields,
  ) async {
    final response = await _dio.patch<Map<String, dynamic>>(
      '$_path/${task.sessionId}',
      data: {'revision': task.revision, ...fields},
    );
    return LightProduction.fromJson(response.data!);
  }

  Future<LightProduction> action(
    LightProduction task,
    String action,
    String requestId,
  ) async {
    final response = await _dio.post<Map<String, dynamic>>(
      '$_path/${task.sessionId}/actions',
      data: {
        'revision': task.revision,
        'action': action,
        'request_id': requestId,
      },
    );
    return LightProduction.fromJson(response.data!);
  }

  Future<({List<LightProduction> items, bool hasMore})> history({
    int offset = 0,
  }) async {
    final response = await _dio.get<Map<String, dynamic>>(
      _path,
      queryParameters: {'offset': offset},
    );
    return (
      items: [
        for (final item in asList(response.data?['items']))
          LightProduction.fromJson(asMap(item)),
      ],
      hasMore: response.data?['has_more'] == true,
    );
  }

  Future<ResourcePage> library({
    required String kind,
    required int offset,
  }) async {
    final response = await _dio.get<Map<String, dynamic>>(
      '/api/assets',
      queryParameters: {'kind': kind, 'offset': offset, 'owned_only': true},
    );
    return ResourcePage.fromJson(response.data ?? const {});
  }
}

String productionRequestId() {
  final random = Random.secure();
  return List.generate(
    16,
    (_) => random.nextInt(256).toRadixString(16).padLeft(2, '0'),
  ).join();
}

final lightProductionApiProvider = Provider<LightProductionApi>(
  (ref) => LightProductionApi(ref.watch(apiDioProvider)),
);
