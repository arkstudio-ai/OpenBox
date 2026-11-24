import 'dart:math';

import 'package:dio/dio.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/providers.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/json.dart';
import '../../../shared/models/light.dart';
import '../../../shared/models/resource.dart';
import 'production_shot_models.dart';

const shortcutStory = 'shortcut_story';
const productStory = 'product_story';

enum ProductionKind {
  video('video'),
  footage('footage_edit'),
  menu('menu'),
  poster('poster');

  const ProductionKind(this.id);
  final String id;

  static ProductionKind? fromId(String? id) =>
      values.where((kind) => kind.id == id).firstOrNull;

  String label(I18nState i18n) => i18n.t('light:production.kind.$id');

  bool get imagesOnly => this == menu || this == poster;

  List<String> get templates => switch (this) {
    video => [productStory, shortcutStory, 'soda_story', 'silk_road'],
    footage => ['documentary', 'quick_cut', 'product'],
    menu || poster => const [],
  };
}

enum ProductionVideoTier {
  fast,
  medium,
  high;

  static ProductionVideoTier? fromId(Object? id) =>
      values.where((tier) => tier.name == id).firstOrNull;

  String label(I18nState i18n) => i18n.t('light:production.videoTier.$name');
}

enum ProductionMusic {
  auto,
  upbeat,
  calm,
  energetic,
  off;

  static ProductionMusic? fromId(Object? id) =>
      values.where((style) => style.name == id).firstOrNull;

  String label(I18nState i18n) => i18n.t('light:production.music.$name');
}

class ProductionMedia {
  const ProductionMedia({
    required this.id,
    required this.name,
    required this.mime,
    this.thumbnail,
    this.aspectRatio,
    this.subtitles,
    this.kind,
  });

  final String id;
  final String name;
  final String mime;
  final String? thumbnail;
  final double? aspectRatio;
  final String? subtitles;
  final String? kind;

  bool get video => kind == 'video' || mime.startsWith('video/');

  factory ProductionMedia.fromJson(Map<String, dynamic> json) {
    final width = asInt(json['width']) ?? 0;
    final height = asInt(json['height']) ?? 0;
    return ProductionMedia(
      id: asString(json['id']) ?? '',
      name: asString(json['name']) ?? '',
      mime: asString(json['mime']) ?? '',
      thumbnail: asString(json['thumbnail']),
      subtitles: asString(json['subtitles_srt']),
      aspectRatio: width > 0 && height > 0 ? width / height : null,
    );
  }

  factory ProductionMedia.fromResource(Resource resource) => ProductionMedia(
    id: resource.id,
    name: resource.name,
    mime: resource.mime,
    kind: resource.kind,
    thumbnail: resource.kind == 'image' ? resource.url : resource.thumbnail,
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
      updatedAt = asDate(json['updated_at']),
      legacy = json['legacy'] == true,
      revision = asInt(json['revision']) ?? 1,
      page = asString(json['page']) ?? 'materials',
      status = asString(json['status']) ?? 'draft',
      phase = asString(json['phase']),
      shotProgress = ProductionShotProgress.parse(json['shot_progress']),
      shotWorkspace = json['shot_editor'] == null
          ? null
          : ProductionShotWorkspace.fromJson(asMap(json['shot_editor'])),
      shop = asString(json['shop']) ?? '',
      brief = asString(json['brief']) ?? '',
      script = asString(json['script']) ?? '',
      posterTitle = asString(json['poster_title']) ?? '',
      referenceCopy = asString(json['reference_copy']) ?? '',
      productName = asString(json['product_name']) ?? '',
      suggestedProduct = asString(json['suggested_product']) ?? '',
      music =
          ProductionMusic.fromId(json['music_style']) ??
          (json['beat_music'] == false
              ? ProductionMusic.off
              : ProductionMusic.auto),
      remakePrepared = json['remake_prepared'] == true,
      template = asString(json['template']) ?? '',
      referenceExampleId = asString(json['reference_example_id']),
      referenceExampleTitle = asString(json['reference_example_title']) ?? '',
      referenceExampleTitleTranslations = lightTitleTranslations(
        json['reference_example_title_translations'],
      ),
      chatTier = asString(json['chat_tier']),
      videoTier = ProductionVideoTier.fromId(json['video_tier']),
      availableVideoTiers = {
        for (final id in asList(json['available_video_tiers']))
          ?ProductionVideoTier.fromId(id),
      },
      notes = asString(json['notes']) ?? '',
      agentRevision = asInt(json['agent_revision']) ?? 0,
      keepOriginal = json['keep_original'] != false,
      redoNarration = json['redo_narration'] == true,
      voiceId = asString(json['voice_id']) ?? '',
      creativeEffects = json['creative_effects'] == true,
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
  final DateTime? updatedAt;
  final bool legacy;
  final int revision;
  final String page;
  final String status;
  final String? phase;
  final ProductionShotProgress? shotProgress;
  final ProductionShotWorkspace? shotWorkspace;
  final String shop;
  final String brief;
  final String script;
  final String posterTitle;
  final String referenceCopy;
  final String productName, suggestedProduct;
  final ProductionMusic music;
  final bool remakePrepared;
  final String template;
  final String? referenceExampleId;
  final String referenceExampleTitle;
  final Map<String, String> referenceExampleTitleTranslations;

  String referenceTitleFor(String language) =>
      referenceExampleTitleTranslations[language] ?? referenceExampleTitle;
  final ProductionVideoTier? videoTier;
  final String? chatTier;
  final Set<ProductionVideoTier> availableVideoTiers;
  final String notes;
  final int agentRevision;
  final bool keepOriginal, redoNarration;
  final String voiceId;
  final bool creativeEffects;
  final List<String> assets;
  final List<ProductionMedia> materials;
  final List<ProductionDish> dishes;
  final ProductionMedia? output;
  final Map<String, dynamic>? proposal;

  bool get working => status == 'working';
  bool supportsVideoTiers(String selectedTemplate) =>
      kind == ProductionKind.video &&
      selectedTemplate != shortcutStory &&
      selectedTemplate != productStory &&
      (selectedTemplate.isEmpty || kind.templates.contains(selectedTemplate));
  bool get manualPreparation => const [
    productStory,
    shortcutStory,
    'soda_story',
    'silk_road',
  ].contains(template);
  bool get supportsMusic =>
      kind == ProductionKind.video && template != productStory;
  bool get wanRemake => manualPreparation || template == 'wan_story';
  bool get remake =>
      kind == ProductionKind.video && (wanRemake || template == 'twist');
}

class LightProductionApi {
  LightProductionApi(this._dio);

  final Dio _dio;
  static const _path = '/api/light/productions';

  Future<LightProduction> preview(ProductionKind kind, {String? brief}) async {
    final response = await _dio.get<Map<String, dynamic>>(
      '$_path/preview',
      queryParameters: {'entry_id': kind.id, 'brief': ?brief},
    );
    return LightProduction.fromJson(response.data!);
  }

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

  Future<LightProduction> shotAction(
    LightProduction task,
    String action,
    String requestId, {
    String? versionId,
    String? prompt,
    String? firstFrame,
    String? lastFrame,
    List<String>? versions,
    bool? hideTitle,
  }) async {
    final response = await _dio.post<Map<String, dynamic>>(
      '$_path/${task.sessionId}/shot-actions',
      data: {
        'revision': task.revision,
        'action': action,
        'request_id': requestId,
        'version_id': ?versionId,
        'prompt': ?prompt,
        'first_frame': ?firstFrame,
        'last_frame': ?lastFrame,
        'versions': ?versions,
        'hide_title': ?hideTitle,
      },
    );
    return LightProduction.fromJson(response.data!);
  }

  Future<LightProduction> generateFrame(
    LightProduction task, {
    required String requestId,
    required String versionId,
    required String role,
    required String prompt,
    required bool useCurrent,
  }) async {
    final response = await _dio.post<Map<String, dynamic>>(
      '$_path/${task.sessionId}/shot-frames',
      data: {
        'revision': task.revision,
        'request_id': requestId,
        'version_id': versionId,
        'role': role,
        'prompt': prompt,
        'use_current': useCurrent,
      },
    );
    return LightProduction.fromJson(response.data!);
  }

  Future<LightProduction> action(
    LightProduction task,
    String action,
    String requestId, {
    String? narrationPreviewId,
  }) async {
    final response = await _dio.post<Map<String, dynamic>>(
      '$_path/${task.sessionId}/actions',
      data: {
        'revision': task.revision,
        'action': action,
        'request_id': requestId,
        'narration_preview_id': ?narrationPreviewId,
      },
    );
    return LightProduction.fromJson(response.data!);
  }

  Future<({List<LightProduction> items, bool hasMore})> history({
    int offset = 0,
    ProductionKind? kind,
  }) async {
    final response = await _dio.get<Map<String, dynamic>>(
      _path,
      queryParameters: {'offset': offset, 'entry_id': ?kind?.id},
    );
    return (
      items: [
        for (final item in asList(response.data?['items']))
          LightProduction.fromJson(asMap(item)),
      ],
      hasMore: response.data?['has_more'] == true,
    );
  }

  static const _voices = '/api/light/production-voices';
  static final _mediaWait = Options(
    receiveTimeout: const Duration(seconds: 150),
  );

  Future<Map<String, dynamic>> voices() async =>
      (await _dio.get<Map<String, dynamic>>(
        _voices,
        options: _mediaWait,
      )).data!;

  Future<Map<String, dynamic>> addVoice(
    String name,
    String path,
    String requestId,
  ) async => (await _dio.post<Map<String, dynamic>>(
    _voices,
    data: FormData.fromMap({
      'name': name,
      'consent': 'true',
      'request_id': requestId,
      'audio': await MultipartFile.fromFile(path),
    }),
    options: _mediaWait,
  )).data!;

  Future<String> auditionVoice(String id) async =>
      (await _dio.post<Map<String, dynamic>>(
            '$_voices/audition',
            data: {'voice_id': id},
            options: _mediaWait,
          )).data!['url']
          as String;

  Future<Map<String, dynamic>> speech(String sessionId) async =>
      (await _dio.get<Map<String, dynamic>>(
        '$_path/$sessionId/narration',
        options: _mediaWait,
      )).data!;

  Future<Map<String, dynamic>> narrationDraft(
    LightProduction task,
    String emphasis,
    List<String>? lines,
  ) async => (await _dio.post<Map<String, dynamic>>(
    '$_path/${task.sessionId}/narration/draft',
    data: {'revision': task.revision, 'emphasis': emphasis, 'lines': ?lines},
    options: _mediaWait,
  )).data!;

  Future<void> saveNarrationLines(LightProduction task, List<String> lines) =>
      _dio.patch<Map<String, dynamic>>(
        '$_path/${task.sessionId}/narration/draft',
        data: {'revision': task.revision, 'lines': lines},
        options: _mediaWait,
      );

  Future<Map<String, dynamic>> narrationPreview(
    LightProduction task,
    List<String> lines,
  ) async => (await _dio.post<Map<String, dynamic>>(
    '$_path/${task.sessionId}/narration/preview',
    data: {'revision': task.revision, 'lines': lines},
    options: _mediaWait,
  )).data!;

  Future<ResourcePage> library({
    required String kind,
    required int offset,
  }) async {
    final response = await _dio.get<Map<String, dynamic>>(
      '/api/assets',
      queryParameters: {
        'kind': kind,
        'offset': offset,
        'owned_only': true,
        if (kind == 'video') 'video_thumbnails': true,
      },
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
