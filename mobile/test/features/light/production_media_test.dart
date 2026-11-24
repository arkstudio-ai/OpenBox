import 'dart:async';
import 'dart:ui' as ui;

import 'package:bossip_mobile/features/chat/api/assets_api.dart';
import 'package:bossip_mobile/features/chat/state/config_providers.dart';
import 'package:bossip_mobile/features/light/api/light_api.dart';
import 'package:bossip_mobile/features/light/production/production_api.dart';
import 'package:bossip_mobile/features/light/production/production_history.dart';
import 'package:bossip_mobile/features/light/production/production_media.dart';
import 'package:bossip_mobile/features/light/production/production_page.dart';
import 'package:bossip_mobile/features/light/production/production_shots.dart';
import 'package:bossip_mobile/features/light/production/production_stages.dart';
import 'package:bossip_mobile/features/light/production/production_widgets.dart';
import 'package:bossip_mobile/features/light/state/light_providers.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/models/app_config.dart';
import 'package:bossip_mobile/shared/router/paths.dart';
import 'package:bossip_mobile/shared/widgets/media_viewer.dart';
import 'package:dio/dio.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:shared_preferences/shared_preferences.dart';

ui.Image _frame() {
  final recorder = ui.PictureRecorder();
  Canvas(recorder).drawColor(Colors.blue, BlendMode.src);
  final picture = recorder.endRecording();
  final frame = picture.toImageSync(8, 8);
  picture.dispose();
  return frame;
}

void _cacheImage(String url) {
  final key = NetworkImage(url);
  PaintingBinding.instance.imageCache.putIfAbsent(
    key,
    () => OneFrameImageStreamCompleter(
      SynchronousFuture(ImageInfo(image: _frame())),
    ),
  );
  addTearDown(() => key.evict());
}

void main() {
  late I18nBundle bundle;
  late SharedPreferences prefs;

  setUpAll(() async {
    TestWidgetsFlutterBinding.ensureInitialized();
    SharedPreferences.setMockInitialValues({'bossip:lang': 'zh-CN'});
    prefs = await SharedPreferences.getInstance();
    bundle = await I18nBundle.load();
  });

  Widget host(Widget body, {List<Override> overrides = const []}) =>
      ProviderScope(
        overrides: [
          i18nProvider.overrideWith(() => I18nController(bundle, prefs)),
          ...overrides,
        ],
        child: MaterialApp(
          theme: ThemeData(
            extensions: [
              BossipTokens.resolve(BossipThemeName.default_, Brightness.light),
            ],
          ),
          home: Scaffold(body: body),
        ),
      );

  for (final (kind, page) in [('video', 'review'), ('footage_edit', 'style')]) {
    testWidgets('$kind saves its promotion brief from the bottom sheet', (
      tester,
    ) async {
      tester.view.devicePixelRatio = 1;
      tester.view.physicalSize = const Size(430, 900);
      addTearDown(tester.view.resetDevicePixelRatio);
      addTearDown(tester.view.resetPhysicalSize);
      const session = 'brief-draft';
      var saved = <String, dynamic>{
        'session_id': session,
        'entry_id': kind,
        'page': page,
        'shop': '56谣',
        'template': kind == 'video' ? shortcutStory : 'documentary',
        'revision': 1,
      };
      final dio = Dio()
        ..interceptors.add(
          InterceptorsWrapper(
            onRequest: (request, handler) {
              if (request.method == 'PATCH') {
                saved = {
                  ...saved,
                  ...request.data as Map<String, dynamic>,
                  'revision': 2,
                };
              }
              handler.resolve(
                Response(
                  requestOptions: request,
                  data: request.path == '/api/light/examples'
                      ? {'items': <Object>[]}
                      : saved,
                ),
              );
            },
          ),
        );
      Widget draft() => host(
        ProductionPage(
          sessionId: session,
          initial: LightProduction.fromJson(saved),
        ),
        overrides: [
          lightProductionApiProvider.overrideWithValue(LightProductionApi(dio)),
          lightApiProvider.overrideWithValue(LightApi(dio)),
        ],
      );
      const audience = '下班小聚';
      const offer = '晚间驻唱和咖啡';
      const action = '私信留座';
      const details = '可以选窗边座位';
      final card = find.byKey(const Key('production-promotion'));
      final audienceField = find.byKey(
        const Key('production-promotion-audience'),
      );
      final offerField = find.byKey(const Key('production-promotion-offer'));
      final actionField = find.byKey(
        const Key('production-promotion-call_to_action'),
      );
      final detailsField = find.byKey(
        const Key('production-promotion-details'),
      );
      final close = find.byKey(const Key('production-promotion-close'));
      final done = find.byKey(const Key('production-promotion-done'));
      final sheet = find.byType(BottomSheet);

      await tester.pumpWidget(draft());
      await tester.pumpAndSettle();
      expect(sheet, findsNothing);
      expect(offerField, findsNothing);
      await tester.ensureVisible(card);
      await tester.tap(card);
      await tester.pumpAndSettle();
      expect(sheet, findsOneWidget);
      expect(find.descendant(of: sheet, matching: offerField), findsOneWidget);
      expect(tester.getSize(offerField).height, greaterThanOrEqualTo(52));
      await tester.enterText(audienceField, '这次编辑将关闭');
      await tester.tap(close);
      await tester.pumpAndSettle();
      expect(sheet, findsNothing);
      expect(saved['promotion_brief'], isNull);
      expect(find.text('选填'), findsOneWidget);

      await tester.tap(card);
      await tester.pumpAndSettle();
      await tester.enterText(audienceField, audience);
      await tester.enterText(offerField, offer);
      await tester.enterText(actionField, action);
      await tester.enterText(detailsField, details);
      await tester.pumpAndSettle(const Duration(milliseconds: 600));
      expect(saved['promotion_brief'], isNull);
      tester.view.viewInsets = const FakeViewPadding(bottom: 330);
      addTearDown(tester.view.resetViewInsets);
      await tester.pumpAndSettle();
      expect(tester.getBottomRight(done).dy, lessThanOrEqualTo(900 - 330));
      await tester.tap(done);
      tester.view.resetViewInsets();
      await tester.pumpAndSettle(const Duration(milliseconds: 600));
      expect(sheet, findsNothing);
      expect(saved['promotion_brief'], {
        'audience': audience,
        'offer': offer,
        'call_to_action': action,
        'details': details,
      });
      expect(find.text('$audience · $offer · $action'), findsOneWidget);

      await tester.pumpWidget(const SizedBox.shrink());
      await tester.pumpWidget(draft());
      await tester.pumpAndSettle();
      expect(offerField, findsNothing);
      expect(find.text('$audience · $offer · $action'), findsOneWidget);
      await tester.ensureVisible(card);
      await tester.tap(card);
      await tester.pumpAndSettle();
      expect(tester.widget<TextField>(offerField).controller!.text, offer);
      expect(tester.widget<TextField>(detailsField).controller!.text, details);
      await tester.tap(close);
      await tester.pumpAndSettle();
      await tester.pumpWidget(const SizedBox.shrink());
    });
  }

  testWidgets(
    'the commercial saves the selected product and renders in one tap',
    (tester) async {
      tester.view.devicePixelRatio = 1;
      tester.view.physicalSize = const Size(430, 900);
      addTearDown(tester.view.resetDevicePixelRatio);
      addTearDown(tester.view.resetPhysicalSize);
      const session = 'brand-draft';
      var saved = <String, dynamic>{
        'session_id': session,
        'entry_id': 'video',
        'page': 'review',
        'template': productStory,
        'revision': 1,
        'asset_ids': ['coffee-photo'],
        'materials': [
          {'id': 'coffee-photo', 'name': '咖啡包装.jpg', 'mime': 'image/jpeg'},
        ],
      };
      final actions = <String>[];
      final dio = Dio()
        ..interceptors.add(
          InterceptorsWrapper(
            onRequest: (request, handler) {
              if (request.method == 'PATCH') {
                saved = {
                  ...saved,
                  ...request.data as Map<String, dynamic>,
                  'revision': 2,
                };
              } else if (request.path.endsWith('/actions')) {
                final body = request.data as Map<String, dynamic>;
                actions.add(body['action'] as String);
                expect(saved['product_name'], '山野咖啡');
                expect(saved['reference_copy'], '展示手冲过程。片尾：每天一杯。');
                saved = {
                  ...saved,
                  'script': '山野咖啡 · 手冲场景',
                  'remake_prepared': true,
                };
              }
              handler.resolve(Response(requestOptions: request, data: saved));
            },
          ),
        );
      await tester.pumpWidget(
        host(
          ProductionPage(
            sessionId: session,
            initial: LightProduction.fromJson(saved),
          ),
          overrides: [
            lightProductionApiProvider.overrideWithValue(
              LightProductionApi(dio),
            ),
            lightApiProvider.overrideWithValue(LightApi(dio)),
          ],
        ),
      );
      await tester.pumpAndSettle();
      final product = find.byKey(const Key('production-product-name'));
      final copy = find.byKey(const Key('production-script'));
      final primary = find.byKey(const Key('production-primary'));
      final field = tester.widget<TextField>(product);
      expect(field.decoration?.labelText, '商品名称（选填）');
      expect(find.textContaining('根据你的商品素材构思分镜'), findsOneWidget);
      expect(find.textContaining('手机型号'), findsNothing);
      expect(
        find.descendant(of: primary, matching: find.text('生成视频')),
        findsOneWidget,
      );
      await tester.ensureVisible(product);
      await tester.enterText(product, '山野咖啡');
      await tester.ensureVisible(copy);
      await tester.enterText(copy, '展示手冲过程。片尾：每天一杯。');
      await tester.tap(primary);
      await tester.pumpAndSettle();
      expect(actions, ['render']);
      expect(find.byKey(const Key('production-remake-script')), findsOneWidget);
    },
  );

  testWidgets('completed video preview uses landscape media dimensions', (
    tester,
  ) async {
    final media = {'id': 'film', 'name': 'film.mp4', 'mime': 'video/mp4'};
    final preview = find.byKey(const Key('production-result-preview'));
    for (final (dimensions, ratio) in [
      ({'width': 1280, 'height': 720}, 16 / 9),
      (<String, int>{}, 9 / 16),
    ]) {
      await tester.pumpWidget(
        host(
          ProductionResult(
            task: LightProduction.fromJson({
              'entry_id': 'video',
              'template': productStory,
              'output': {...media, ...dimensions},
            }),
          ),
          overrides: [
            videoThumbnailProvider.overrideWith((_, _) async => null),
          ],
        ),
      );
      await tester.pumpAndSettle();
      expect(tester.getSize(preview).aspectRatio, closeTo(ratio, .01));
    }
  });

  testWidgets('a stopped brand review explains the shot and correction', (
    tester,
  ) async {
    await tester.pumpWidget(
      host(
        ProductionDecision(
          task: LightProduction.fromJson({
            'entry_id': 'video',
            'status': 'failed',
            'template': productStory,
            'review_feedback': {
              'shot': 6,
              'reason': '主商品在切镜时消失。',
              'correction': '让下一镜保留桌面上的同一个商品。',
            },
          }),
        ),
      ),
    );
    expect(find.byKey(const Key('production-review-feedback')), findsOneWidget);
    expect(find.text('第 6 镜需要修正'), findsOneWidget);
    expect(find.text('主商品在切镜时消失。'), findsOneWidget);
    expect(find.text('让下一镜保留桌面上的同一个商品。'), findsOneWidget);
  });

  testWidgets('opening video creation is read-only until the first edit', (
    tester,
  ) async {
    const path = '/api/light/productions';
    const start = '/light/topic/video';
    const preview = {'entry_id': 'video', 'shop': '默认门店'};
    final requests = <String>[];
    final dio = Dio()
      ..interceptors.add(
        InterceptorsWrapper(
          onRequest: (request, handler) {
            requests.add('${request.method} ${request.path}');
            final data = switch ((request.method, request.path)) {
              ('GET', '$path/preview') => preview,
              ('POST', path) => {...preview, 'session_id': 'video-draft'},
              ('PATCH', '$path/video-draft') => {
                ...preview,
                'session_id': 'video-draft',
                'revision': 2,
                ...request.data as Map,
              },
              _ => throw StateError('Unexpected request ${request.path}'),
            };
            handler.resolve(
              Response(
                requestOptions: request,
                data: Map<String, dynamic>.from(data),
              ),
            );
          },
        ),
      );
    final router = GoRouter(
      initialLocation: '/',
      routes: [
        GoRoute(
          path: '/',
          builder: (context, _) => Scaffold(
            body: TextButton(
              onPressed: () => context.push(start),
              child: const Text('开始制作'),
            ),
          ),
        ),
        GoRoute(
          path: start,
          builder: (_, _) =>
              const ProductionStartPage(kind: ProductionKind.video),
        ),
      ],
    );
    addTearDown(router.dispose);
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          i18nProvider.overrideWith(() => I18nController(bundle, prefs)),
          lightProductionApiProvider.overrideWithValue(LightProductionApi(dio)),
          appConfigProvider.overrideWith((ref) async => AppConfig.fromJson({})),
        ],
        child: MaterialApp.router(
          routerConfig: router,
          theme: ThemeData(
            extensions: [
              BossipTokens.resolve(BossipThemeName.default_, Brightness.light),
            ],
          ),
        ),
      ),
    );
    final startButton = find.text('开始制作');
    final shop = find.byKey(const Key('production-shop'));
    await tester.tap(startButton);
    await tester.pumpAndSettle();
    expect(tester.widget<TextField>(shop).controller!.text, '默认门店');
    await tester.tap(find.byKey(const Key('production-back')));
    await tester.pumpAndSettle();
    expect(requests, ['GET $path/preview']);
    await tester.tap(startButton);
    await tester.pumpAndSettle();
    await tester.enterText(shop, '已填写的门店');
    await tester.pump(const Duration(milliseconds: 500));
    await tester.pumpAndSettle();
    expect(requests, [
      'GET $path/preview',
      'GET $path/preview',
      'POST $path',
      'PATCH $path/video-draft',
    ]);
    expect(tester.widget<TextField>(shop).controller!.text, '已填写的门店');
    await tester.pumpWidget(const SizedBox.shrink());
  });

  testWidgets('AI video has an independent music picker and saves the choice', (
    tester,
  ) async {
    tester.view.devicePixelRatio = 1;
    tester.view.physicalSize = const Size(430, 850);
    addTearDown(tester.view.resetDevicePixelRatio);
    addTearDown(tester.view.resetPhysicalSize);
    final saved = <String, dynamic>{
      'session_id': 'music-test',
      'entry_id': 'video',
      'revision': 1,
      'shop': '兴旺菜馆',
      'page': 'materials',
      'status': 'draft',
      'video_tier': 'fast',
      'available_video_tiers': ['fast', 'medium', 'high'],
    };
    final dio = Dio()
      ..interceptors.add(
        InterceptorsWrapper(
          onRequest: (request, handler) {
            expect(request.path, '/api/light/productions/music-test');
            if (request.method == 'PATCH') {
              expect(request.data, {'revision': 1, 'music_style': 'calm'});
              saved.addAll({'music_style': 'calm', 'revision': 2});
            }
            handler.resolve(
              Response(requestOptions: request, data: Map.of(saved)),
            );
          },
        ),
      );
    await tester.pumpWidget(
      host(
        const ProductionPage(sessionId: 'music-test'),
        overrides: [
          lightProductionApiProvider.overrideWithValue(LightProductionApi(dio)),
          appConfigProvider.overrideWith((ref) async => AppConfig.fromJson({})),
        ],
      ),
    );
    await tester.pumpAndSettle();
    final row = find.byKey(const Key('production-music-settings'));
    await tester.ensureVisible(row);
    expect(find.text('自动选曲'), findsOneWidget);
    expect(find.byKey(const Key('production-voice-settings')), findsNothing);
    await tester.tap(row);
    await tester.pumpAndSettle();
    expect(find.text('不配乐'), findsOneWidget);
    await tester.tap(find.text('舒缓'));
    await tester.pumpAndSettle(const Duration(milliseconds: 500));
    expect(saved['music_style'], 'calm');
    expect(find.descendant(of: row, matching: find.text('舒缓')), findsOneWidget);
    await tester.pumpWidget(const SizedBox.shrink());
  });

  testWidgets(
    'returning from advanced chat refreshes the clip and final version',
    (tester) async {
      tester.view.devicePixelRatio = 1;
      tester.view.physicalSize = const Size(430, 850);
      addTearDown(tester.view.resetDevicePixelRatio);
      addTearDown(tester.view.resetPhysicalSize);
      const sessionId = 'advanced-sync';
      final requestInFlight = Completer<void>();
      Map<String, dynamic> task(int film, int clip) {
        Map<String, dynamic> video(String id) => {
          'id': id,
          'name': '$id.mp4',
          'mime': 'video/mp4',
          'width': 1280,
          'height': 720,
        };
        return {
          'session_id': sessionId,
          'entry_id': 'video',
          'status': 'completed',
          'revision': 16,
          'agent_revision': 16,
          'output': video('film-$film'),
          'shot_editor': {
            'title': '说好只吃一口',
            'films': [
              {'number': film, 'current': true, 'media': video('film-$film')},
            ],
            'shots': [
              {
                'index': 8,
                'title': '目送公交离站',
                'status': 'ready',
                'version': {'id': 'shot-$clip', 'media': video('clip-$clip')},
              },
            ],
          },
        };
      }

      var requests = 0;
      var response = task(1, 2);
      final dio = Dio()
        ..interceptors.add(
          InterceptorsWrapper(
            onRequest: (request, handler) async {
              final data = ++requests == 1 ? task(1, 1) : response;
              if (requests == 1) await requestInFlight.future;
              handler.resolve(Response(requestOptions: request, data: data));
            },
          ),
        );
      final router = GoRouter(
        initialLocation: '/',
        routes: [
          GoRoute(
            path: '/',
            builder: (_, _) => ProductionPage(
              sessionId: sessionId,
              initial: LightProduction.fromJson(task(1, 1)),
            ),
          ),
          GoRoute(
            path: Paths.chat(sessionId),
            builder: (_, _) => const Scaffold(body: Text('Agent')),
          ),
        ],
      );
      addTearDown(router.dispose);
      await tester.pumpWidget(
        ProviderScope(
          overrides: [
            i18nProvider.overrideWith(() => I18nController(bundle, prefs)),
            lightProductionApiProvider.overrideWithValue(
              LightProductionApi(dio),
            ),
            videoThumbnailProvider.overrideWith((_, _) async => null),
          ],
          child: MaterialApp.router(
            routerConfig: router,
            theme: ThemeData(
              extensions: [
                BossipTokens.resolve(
                  BossipThemeName.default_,
                  Brightness.light,
                ),
              ],
            ),
          ),
        ),
      );
      await tester.pumpAndSettle();
      final advanced = find.byKey(const Key('production-advanced'));
      await tester.tap(advanced);
      await tester.pumpAndSettle();
      expect(find.text('Agent'), findsOneWidget);
      router.pop();
      requestInFlight.complete();
      await tester.pumpAndSettle();
      expect(requests, 2);
      expect(find.text('已从对话更新'), findsOneWidget);
      final page = tester.widget<ProductionShotList>(
        find.byType(ProductionShotList),
      );
      expect(page.task.shotWorkspace!.shots.single.version!.id, 'shot-2');

      await tester.tap(advanced);
      await tester.pumpAndSettle();
      response = task(2, 2);
      router.pop();
      await tester.pumpAndSettle();
      expect(requests, 3);
      expect(find.text('当前成片 · v2'), findsOneWidget);
      expect(find.text('已从对话更新'), findsOneWidget);
      await tester.pumpWidget(const SizedBox.shrink());
    },
  );

  testWidgets('library paints video covers and falls back for older servers', (
    tester,
  ) async {
    const cover = 'https://assets.test/first.jpg';
    _cacheImage(cover);
    final png = await tester.runAsync(() async {
      final image = _frame();
      final data = await image.toByteData(format: ui.ImageByteFormat.png);
      image.dispose();
      return data!.buffer.asUint8List();
    });
    final dio = Dio()
      ..interceptors.add(
        InterceptorsWrapper(
          onRequest: (request, handler) {
            expect(request.path, '/api/assets');
            final video = request.queryParameters['kind'] == 'video';
            expect(request.queryParameters['owned_only'], true);
            expect(
              request.queryParameters['video_thumbnails'],
              video ? true : null,
            );
            handler.resolve(
              Response<Map<String, dynamic>>(
                requestOptions: request,
                data: {
                  'items': video
                      ? [
                          {
                            'id': 'first',
                            'name': '海鲜.mp4',
                            'kind': 'video',
                            'mime': 'application/octet-stream',
                            'thumbnail': cover,
                          },
                          {
                            'id': 'legacy',
                            'name': '街景.mp4',
                            'kind': 'video',
                            'mime': 'video/mp4',
                          },
                          {
                            'id': 'unsupported',
                            'name': '原片.mov',
                            'kind': 'video',
                            'mime': 'video/quicktime',
                            'thumbnail': 'https://assets.test/no-snapshot.jpg',
                          },
                        ]
                      : <Map<String, dynamic>>[],
                },
              ),
            );
          },
        ),
      );
    final decoded = <String>[];
    await tester.pumpWidget(
      host(
        const ProductionLibrary(imagesOnly: false, selected: []),
        overrides: [
          lightProductionApiProvider.overrideWithValue(LightProductionApi(dio)),
          videoThumbnailProvider.overrideWith((ref, id) async {
            decoded.add(id);
            return png!;
          }),
        ],
      ),
    );
    await tester.pumpAndSettle();
    await tester.tap(find.text('视频'));
    await tester.pumpAndSettle();

    Finder tile(String id) => find.byKey(Key('production-library-$id'));
    Image imageInside(String id) => tester.widget<Image>(
      find.descendant(of: tile(id), matching: find.byType(Image)),
    );
    expect(imageInside('first').image, const NetworkImage(cover));
    expect(imageInside('legacy').image, isA<MemoryImage>());
    final unsupported = find.descendant(
      of: tile('unsupported'),
      matching: find.byWidgetPredicate(
        (widget) => widget is Image && widget.image is MemoryImage,
      ),
    );
    expect(unsupported, findsOneWidget);
    expect(decoded, unorderedEquals(['legacy', 'unsupported']));
    await tester.tap(tile('legacy'));
    await tester.pumpAndSettle();
    expect(find.text('确认 1 个素材'), findsOneWidget);
  });

  testWidgets(
    'library opens full-size photos and video without selecting them',
    (tester) async {
      const photo = 'https://assets.test/thumbnail.jpg';
      const original = 'https://assets.test/original.jpg';
      const cover = 'https://assets.test/cover.jpg';
      const clip = 'https://assets.test/original.mp4';
      for (final url in [photo, original, cover]) {
        _cacheImage(url);
      }
      final dio = Dio()
        ..interceptors.add(
          InterceptorsWrapper(
            onRequest: (request, handler) {
              expect(request.path, '/api/assets');
              final video = request.queryParameters['kind'] == 'video';
              handler.resolve(
                Response(
                  requestOptions: request,
                  data: {
                    'items': [
                      if (video)
                        {
                          'id': 'video',
                          'name': '海鲜.mp4',
                          'kind': 'video',
                          'mime': 'video/mp4',
                          'thumbnail': cover,
                        }
                      else
                        {
                          'id': 'photo',
                          'name': '菜单.jpg',
                          'kind': 'image',
                          'mime': 'image/jpeg',
                          'url': photo,
                        },
                    ],
                  },
                ),
              );
            },
          ),
        );
      final loaded = <String>[];
      List<String>? confirmed;
      await tester.pumpWidget(
        host(
          Builder(
            builder: (context) => TextButton(
              onPressed: () async {
                confirmed = await showModalBottomSheet<List<String>>(
                  context: context,
                  isScrollControlled: true,
                  builder: (_) =>
                      const ProductionLibrary(imagesOnly: false, selected: []),
                );
              },
              child: const Text('打开素材库'),
            ),
          ),
          overrides: [
            lightProductionApiProvider.overrideWithValue(
              LightProductionApi(dio),
            ),
            lightAssetUrlProvider.overrideWith((_, id) async {
              loaded.add(id);
              return id == 'photo' ? original : clip;
            }),
          ],
        ),
      );
      await tester.tap(find.text('打开素材库'));
      await tester.pumpAndSettle();
      await tester.tap(
        find.byKey(const Key('production-library-preview-photo')),
      );
      await tester.pumpAndSettle();
      expect(find.byType(MediaViewer), findsOneWidget);
      expect(
        tester.widget<MediaPreviewImage>(find.byType(MediaPreviewImage)).url,
        original,
      );
      expect(find.byType(InteractiveViewer), findsOneWidget);
      final close = find.byKey(const Key('media-viewer-close'));
      await tester.tap(close);
      await tester.pumpAndSettle();
      expect(find.text('确认 0 个素材'), findsOneWidget);
      await tester.tap(find.byKey(const Key('production-library-photo')));
      await tester.tap(find.text('视频'));
      await tester.pumpAndSettle();
      await tester.tap(
        find.byKey(const Key('production-library-preview-video')),
      );
      await tester.pumpAndSettle();
      expect(find.byType(MediaViewer), findsOneWidget);
      expect(
        tester.widget<MediaPreviewVideo>(find.byType(MediaPreviewVideo)).source,
        clip,
      );
      await tester.tap(close);
      await tester.pumpAndSettle();
      expect(loaded, ['photo', 'video']);
      await tester.tap(find.text('确认 1 个素材'));
      await tester.pumpAndSettle();
      expect(confirmed, ['photo']);
    },
  );

  testWidgets('the same OSS preview survives polling and draft saves', (
    tester,
  ) async {
    const base = 'https://assets.test/shop.jpg?OSSAccessKeyId=key';
    const first = '$base&Expires=100&Signature=initial';
    const renewed = '$base&Expires=103&Signature=renewed';
    _cacheImage(first);
    Material mediaPage(String url, {required bool working}) {
      final task = LightProduction.fromJson({
        'entry_id': 'video',
        'status': working ? 'working' : 'draft',
        'phase': 'copy',
        'materials': [
          {
            'id': 'photo',
            'name': 'shop.jpg',
            'mime': 'image/jpeg',
            'thumbnail': url,
          },
        ],
      });
      return Material(
        child: working
            ? ProductionProgress(task: task)
            : ProductionMediaStrip(media: task.materials),
      );
    }

    for (final working in [true, false]) {
      await tester.pumpWidget(host(mediaPage(first, working: working)));
      await tester.pump();
      await tester.pumpWidget(host(mediaPage(renewed, working: working)));
      await tester.pump();
      final thumb = find.byType(ProductionThumbnail);
      expect(
        tester.getCenter(thumb).dx,
        tester.getCenter(find.byType(Scaffold)).dx,
      );
      expect(
        tester.widget<Image>(find.byType(Image)).image,
        const NetworkImage(first),
      );
      expect(tester.widget<RawImage>(find.byType(RawImage)).image, isNotNull);
    }
  });

  testWidgets('one selected material is centered with its remove action', (
    tester,
  ) async {
    const media = ProductionMedia(
      id: 'one',
      name: '照片.jpg',
      mime: 'image/jpeg',
    );
    ProductionMedia? removed;
    await tester.pumpWidget(
      host(
        ProductionSelectionGrid(
          materials: const [media],
          onRemove: (value) => removed = value,
        ),
      ),
    );
    final thumb = find.byType(ProductionThumbnail);
    expect(
      tester.getCenter(thumb).dx,
      tester.getCenter(find.byType(Scaffold)).dx,
    );
    await tester.tap(find.byIcon(Icons.close));
    expect(removed, media);
  });
}
