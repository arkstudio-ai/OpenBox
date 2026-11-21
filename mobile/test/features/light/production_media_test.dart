import 'dart:ui' as ui;

import 'package:bossip_mobile/features/chat/api/assets_api.dart';
import 'package:bossip_mobile/features/light/production/production_api.dart';
import 'package:bossip_mobile/features/light/production/production_media.dart';
import 'package:bossip_mobile/features/light/production/production_stages.dart';
import 'package:bossip_mobile/features/light/production/production_widgets.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:dio/dio.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
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
