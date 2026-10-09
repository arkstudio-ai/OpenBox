import 'dart:async';

import 'package:bossip_mobile/features/chat/api/assets_api.dart';
import 'package:bossip_mobile/features/chat/state/assistant_controller.dart';
import 'package:bossip_mobile/features/chat/utils/content_view.dart';
import 'package:bossip_mobile/features/chat/widgets/attachment_gallery.dart';
import 'package:bossip_mobile/features/chat/widgets/result_artifacts.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/models/message.dart';
import 'package:bossip_mobile/shared/models/message_part.dart';
import 'package:bossip_mobile/shared/ws/ws_client.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'assistant_fixture.dart';

ChatMessage mediaReport() => ChatMessage.fromJson({
  'id': 'm02',
  'session_id': 'main',
  'role': 'assistant',
  'finish': 'stop',
  'parts': [
    {'id': 'reply', 'type': 'text', 'text': 'The picture and video are ready.'},
    for (final (id, mime, kind, project) in [
      ('image', 'image/png', 'generated_image', 'Image project'),
      ('video', 'video/mp4', 'video_final', 'Video project'),
    ])
      {
        'id': 'part-$id',
        'type': 'file',
        'asset_id': 'asset-$id',
        'mime_type': mime,
        'path': '$id.${mime.split('/').last}',
        'relation': {
          'group_id': 'assistant-result:$id',
          'kind': kind,
          'role': 'final',
          'metadata': {
            'assistant_source': {
              'session_id': 'session-$id',
              'project_name': project,
              'title': 'Make $id',
            },
          },
        },
      },
  ],
});

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  late SharedPreferences prefs;
  setUp(() async {
    SharedPreferences.setMockInitialValues({'bossip.language': 'en-US'});
    prefs = await SharedPreferences.getInstance();
  });

  test(
    'attachment hints load owned canonical media once and survive reopening',
    () async {
      final f = Fixture(prefs);
      addTearDown(f.close);
      await f.ready();
      final reads = f.api.historyReads.length;
      f.ws.frames.add(const WsEvent('part.created', {'sessionId': 'other'}));
      await Future<void>.delayed(Duration.zero);
      expect(f.api.historyReads.length, reads);

      f.api.stored['m02'] = mediaReport();
      final received = Completer<void>();
      final subscription = f.container.listen(
        assistantControllerProvider(scope),
        (_, state) {
          if (state.messages.single.parts.whereType<FilePart>().length == 2 &&
              !received.isCompleted) {
            received.complete();
          }
        },
      );
      addTearDown(subscription.close);
      // The event is only a refresh hint: the forged payload is never displayed.
      f.ws.frames.add(
        const WsEvent('part.created', {
          'sessionId': 'main',
          'part': {'id': 'forged', 'type': 'file', 'asset_id': 'foreign'},
        }),
      );
      await received.future.timeout(const Duration(seconds: 2));
      expect(f.api.historyReads.length, greaterThan(reads));
      expect(
        f.state.messages.single.parts.whereType<FilePart>().map(
          (p) => p.assetId,
        ),
        ['asset-image', 'asset-video'],
      );
      await f.controller.refresh();
      expect(f.state.messages.single.parts.whereType<FilePart>(), hasLength(2));

      f.close();
      final reopened = Fixture(prefs, server: f.api);
      addTearDown(reopened.close);
      await reopened.ready();
      final groups = buildAssistantContentView(
        reopened.state.messages,
        false,
      ).resultGroups;
      expect(groups, hasLength(2));
      expect(groups.every((g) => g.sourceTool == null), isTrue);
      expect(groups.map((g) => g.parts.single.assetId), [
        'asset-image',
        'asset-video',
      ]);
    },
  );

  for (final width in [320.0, 390.0]) {
    testWidgets(
      'forwarded media stays separate and opens its source at $width pixels',
      (tester) async {
        tester.view.physicalSize = Size(width, 1100);
        tester.view.devicePixelRatio = 1;
        addTearDown(tester.view.resetPhysicalSize);
        addTearDown(tester.view.resetDevicePixelRatio);
        final groups = buildAssistantContentView([
          mediaReport(),
        ], false).resultGroups;
        final router = GoRouter(
          routes: [
            GoRoute(
              path: '/',
              builder: (_, _) => Scaffold(
                body: SingleChildScrollView(
                  child: ResultArtifacts(groups: groups, verification: null),
                ),
              ),
            ),
            GoRoute(
              path: '/app/s/:id',
              builder: (_, state) =>
                  Scaffold(body: Text('Source ${state.pathParameters['id']}')),
            ),
          ],
        );
        addTearDown(router.dispose);
        final bundle = I18nBundle({
          'en-US': {
            'chat': {
              'artifacts': {'source': 'From:'},
            },
          },
        });
        await tester.pumpWidget(
          ProviderScope(
            overrides: [
              i18nProvider.overrideWith(() => I18nController(bundle, prefs)),
              assetUrlProvider.overrideWith(
                (ref, id) async => AssetUrl(
                  url: 'https://assets.test/$id',
                  mime: id == 'asset-image' ? 'image/png' : 'video/mp4',
                ),
              ),
              videoThumbnailProvider.overrideWith((ref, id) async => null),
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
        final galleries = tester.widgetList<AttachmentGallery>(
          find.byType(AttachmentGallery),
        );
        expect(galleries, hasLength(2));
        expect(galleries.map((g) => g.parts.single.assetId).toSet(), {
          'asset-image',
          'asset-video',
        });
        expect(find.text('From: Image project · Make image'), findsOneWidget);
        expect(find.text('From: Video project · Make video'), findsOneWidget);
        expect(tester.takeException(), isNull);
        for (final id in ['image', 'video']) {
          final label = id == 'image' ? 'Image' : 'Video';
          final link = find.widgetWithText(
            TextButton,
            'From: $label project · Make $id',
          );
          await tester.ensureVisible(link);
          expect(tester.getSize(link).height, greaterThanOrEqualTo(44));
          await tester.tap(link);
          await tester.pumpAndSettle();
          expect(find.text('Source session-$id'), findsOneWidget);
          router.pop();
          await tester.pumpAndSettle();
        }
        expect(tester.takeException(), isNull);
      },
    );
  }
}
