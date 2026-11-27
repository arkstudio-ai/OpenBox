import 'package:bossip_mobile/features/chat/api/assets_api.dart';
import 'package:bossip_mobile/features/light/production/production_api.dart';
import 'package:bossip_mobile/features/light/production/production_media.dart';
import 'package:bossip_mobile/features/resources/api/resources_api.dart';
import 'package:bossip_mobile/features/resources/resources_screen.dart';
import 'package:bossip_mobile/shared/appearance/theme_builder.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:integration_test/integration_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

Map<String, dynamic> _asset(
  String id,
  String name,
  String kind, {
  String? sessionId,
  String? archiveSessionId,
  String? sessionTitle,
  String source = 'user',
}) => {
  'id': id,
  'name': name,
  'kind': kind,
  'mime': switch (kind) {
    'image' => 'image/jpeg',
    'video' => 'video/mp4',
    _ => 'application/pdf',
  },
  'source': source,
  'sessionId': ?sessionId,
  'archiveSessionId': ?archiveSessionId,
  'sessionTitle': ?sessionTitle,
};

void main() {
  IntegrationTestWidgetsFlutterBinding.ensureInitialized();

  testWidgets('browse generated archives and reuse mixed media', (
    tester,
  ) async {
    SharedPreferences.setMockInitialValues({'bossip:lang': 'zh-CN'});
    final prefs = await SharedPreferences.getInstance();
    final bundle = await I18nBundle.load();
    final items = [
      _asset('photo', '门店照片.jpg', 'image'),
      _asset('video', '门店实拍.mp4', 'video'),
      _asset('file', '制作需求.pdf', 'document'),
      _asset(
        'generated-photo',
        '周末海报.jpg',
        'image',
        source: 'agent',
        sessionId: 'weekend-subtask',
        archiveSessionId: 'weekend',
        sessionTitle: '周末活动',
      ),
      _asset(
        'weekend-video',
        '活动短片.mp4',
        'video',
        source: 'agent',
        sessionId: 'weekend',
        archiveSessionId: 'weekend',
        sessionTitle: '周末活动',
      ),
      _asset(
        'generated-video',
        '菜单短片.mp4',
        'video',
        source: 'agent',
        sessionId: 'menu',
        sessionTitle: '更新菜单',
      ),
      _asset('legacy', '旧海报.jpg', 'image', source: 'agent'),
    ];
    final dio = Dio()
      ..interceptors.add(
        InterceptorsWrapper(
          onRequest: (request, handler) {
            final query = request.queryParameters;
            final source = query['source'] ?? 'all';
            final kind = query['kind'];
            final matching = items.where((item) {
              return (source == 'all' || item['source'] == source) &&
                  (kind == 'all' ||
                      kind == item['kind'] ||
                      (kind == 'media' && item['kind'] != 'document'));
            }).toList();
            handler.resolve(
              Response(
                requestOptions: request,
                data: request.path == '/api/agent/project'
                    ? <Object>[]
                    : {'items': matching, 'total': matching.length},
              ),
            );
          },
        ),
      );

    List<String>? selected;
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          i18nProvider.overrideWith(() => I18nController(bundle, prefs)),
          resourcesApiProvider.overrideWithValue(ResourcesApi(dio)),
          lightProductionApiProvider.overrideWithValue(LightProductionApi(dio)),
          videoThumbnailProvider.overrideWith((_, _) async => null),
        ],
        child: MaterialApp(
          theme: buildBossipTheme(BossipThemeName.default_, Brightness.light),
          home: Scaffold(
            body: Builder(
              builder: (context) => Column(
                children: [
                  TextButton(
                    onPressed: () => Navigator.of(context).push(
                      MaterialPageRoute<void>(
                        builder: (_) => const ResourcesScreen(),
                      ),
                    ),
                    child: const Text('浏览资源'),
                  ),
                  TextButton(
                    onPressed: () async {
                      selected = await showModalBottomSheet<List<String>>(
                        context: context,
                        isScrollControlled: true,
                        builder: (_) => const ProductionLibrary(
                          imagesOnly: false,
                          selected: [],
                        ),
                      );
                    },
                    child: const Text('选制作素材'),
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );

    await tester.tap(find.text('浏览资源'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('文件'));
    await tester.pumpAndSettle();
    expect(find.text('制作需求.pdf'), findsOneWidget);
    expect(find.text('门店照片.jpg'), findsNothing);
    await tester.tap(find.text('全部类型'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('生成'));
    await tester.pumpAndSettle();
    for (final title in ['周末活动', '更新菜单', '未归档的生成']) {
      expect(find.text(title), findsOneWidget);
    }
    expect(find.text('门店照片.jpg'), findsNothing);
    await tester.pageBack();
    await tester.pumpAndSettle();

    await tester.tap(find.text('选制作素材'));
    await tester.pumpAndSettle();
    Finder tile(String id) => find.byKey(Key('production-library-$id'));
    expect(tile('photo'), findsOneWidget);
    expect(tile('video'), findsOneWidget);
    expect(tile('file'), findsNothing);
    await tester.tap(tile('photo'));
    await tester.tap(find.text('视频'));
    await tester.pumpAndSettle();
    await tester.tap(tile('video'));
    await tester.tap(find.text('生成'));
    await tester.pumpAndSettle();
    expect(find.text('周末活动'), findsOneWidget);
    await tester.tap(tile('generated-photo'));
    await tester.tap(find.text('照片'));
    await tester.pumpAndSettle();
    expect(find.text('确认 3 个素材'), findsOneWidget);
    await tester.tap(find.text('确认 3 个素材'));
    await tester.pumpAndSettle();
    expect(selected, ['photo', 'video', 'generated-photo']);
  });
}
