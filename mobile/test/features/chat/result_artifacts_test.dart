import 'package:bossip_mobile/features/chat/api/assets_api.dart';
import 'package:bossip_mobile/features/chat/utils/content_view.dart';
import 'package:bossip_mobile/features/chat/widgets/result_artifacts.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/models/message.dart';
import 'package:bossip_mobile/shared/models/message_part.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

ArtifactGroup group(
  String id, {
  String kind = 'video_segment',
  String? role,
  int? ordinal,
  List<FilePart>? parts,
}) => ArtifactGroup(
  id: id,
  order: 0,
  artifactKind: kind,
  role: role ?? (kind == 'video_final' ? 'final' : 'intermediate'),
  label: kind == 'video_final' ? 'Final video' : null,
  caption: null,
  ordinal: ordinal,
  revision: null,
  metadata: {},
  sourceTool: null,
  parts:
      parts ??
      [FilePart(id: id, path: '$id.mp4', assetId: id, mimeType: 'video/mp4')],
);

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  late SharedPreferences prefs;
  final bundle = I18nBundle({
    'en-US': {
      'chat': {
        'artifacts': {
          'segmentCollection': 'Segments · {{count}}',
          'segment': 'Segment {{number}}',
        },
        'toolDetail': {'expand': 'Expand', 'collapse': 'Collapse'},
      },
    },
  });
  final segments = [group('one', ordinal: 1), group('two', ordinal: 2)];
  final finalVideo = group('final', kind: 'video_final');
  setUp(() async {
    SharedPreferences.setMockInitialValues({'bossip.language': 'en-US'});
    prefs = await SharedPreferences.getInstance();
  });

  test(
    'persisted materials sort by ordinal before final, with stable ties',
    () {
      final message = ChatMessage.fromJson({
        'id': 'm',
        'session_id': 's',
        'role': 'assistant',
        'finish': 'stop',
        'parts': [
          {
            'type': 'file',
            'id': 'final',
            'path': 'final.mp4',
            'relation': {'kind': 'video_final', 'role': 'final'},
          },
          {
            'type': 'file',
            'id': 'second',
            'path': 'second.mp4',
            'relation': {'kind': 'video_segment', 'ordinal': 2},
          },
          {
            'type': 'file',
            'id': 'first',
            'path': 'first.mp4',
            'relation': {'kind': 'video_segment', 'ordinal': 1},
          },
          {
            'type': 'file',
            'id': 'revision',
            'path': 'revision.mp4',
            'relation': {'kind': 'video_segment', 'ordinal': 1},
          },
          {
            'type': 'file',
            'id': 'unknown',
            'path': 'unknown.mp4',
            'relation': {'kind': 'video_segment'},
          },
        ],
      });
      expect(
        buildAssistantContentView([
          message,
        ], false).resultGroups.map((g) => g.parts.first.id),
        ['first', 'revision', 'second', 'unknown', 'final'],
      );
    },
  );

  Future<void> render(WidgetTester tester, List<ArtifactGroup> groups) async {
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          i18nProvider.overrideWith(() => I18nController(bundle, prefs)),
          assetUrlProvider.overrideWith(
            (ref, id) async =>
                AssetUrl(url: 'https://assets.test/$id', mime: 'video/mp4'),
          ),
          videoThumbnailProvider.overrideWith((ref, id) async => null),
        ],
        child: MaterialApp(
          theme: ThemeData(
            extensions: [
              BossipTokens.resolve(BossipThemeName.default_, Brightness.light),
            ],
          ),
          home: Scaffold(
            body: SingleChildScrollView(
              child: ResultArtifacts(groups: groups, verification: null),
            ),
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();
    expect(tester.takeException(), isNull);
  }

  testWidgets(
    'segments stay expanded and cannot collapse without a final video',
    (tester) async {
      await render(tester, segments);
      expect(find.text('one.mp4'), findsOneWidget);
      expect(find.text('two.mp4'), findsOneWidget);
      expect(find.byType(TextButton), findsNothing);
    },
  );

  for (final width in [320.0, 390.0, 768.0]) {
    testWidgets(
      'folded materials precede the final and toggle at $width pixels',
      (tester) async {
        tester.view.physicalSize = Size(width, 900);
        tester.view.devicePixelRatio = 1;
        addTearDown(tester.view.resetPhysicalSize);
        addTearDown(tester.view.resetDevicePixelRatio);
        await render(tester, [finalVideo, ...segments]);
        expect(find.text('one.mp4'), findsNothing);
        expect(find.text('final.mp4'), findsOneWidget);
        final toggle = find.byType(TextButton);
        expect(
          tester.getTopLeft(toggle).dy,
          lessThan(tester.getTopLeft(find.text('Final video')).dy),
        );
        expect(tester.getSize(toggle).height, greaterThanOrEqualTo(44));
        await tester.tap(toggle);
        await tester.pumpAndSettle();
        expect(find.text('one.mp4'), findsOneWidget);
        expect(
          tester.getTopLeft(find.text('Segment 1')).dy,
          lessThan(tester.getTopLeft(find.text('Segment 2')).dy),
        );
        expect(
          tester.getTopLeft(find.text('Segment 2')).dy,
          lessThan(tester.getTopLeft(find.text('Final video')).dy),
        );
        expect(tester.takeException(), isNull);
        await tester.tap(toggle);
        await tester.pumpAndSettle();
        expect(find.text('one.mp4'), findsNothing);
      },
    );
  }

  testWidgets(
    'live completion folds once, rerenders preserve the user choice, removal expands',
    (tester) async {
      await render(tester, segments);
      await render(tester, [...segments, finalVideo]);
      expect(find.text('one.mp4'), findsNothing);
      await tester.tap(find.byType(TextButton));
      await tester.pumpAndSettle();
      await render(tester, [finalVideo, ...segments]);
      expect(find.text('one.mp4'), findsOneWidget);
      await render(tester, [
        ...segments,
        group('revision-2', kind: 'video_final'),
      ]);
      expect(find.text('one.mp4'), findsNothing);
      await render(tester, segments);
      expect(find.text('one.mp4'), findsOneWidget);
      expect(find.byType(TextButton), findsNothing);
    },
  );

  for (final other in [
    group('pending', kind: 'video_final', parts: []),
    group(
      'missing',
      kind: 'video_final',
      parts: [
        const FilePart(
          id: 'missing',
          path: 'missing.mp4',
          mimeType: 'video/mp4',
        ),
      ],
    ),
    group(
      'image',
      kind: 'video_final',
      parts: [
        const FilePart(
          id: 'image',
          path: 'image.png',
          assetId: 'image',
          mimeType: 'image/png',
        ),
      ],
    ),
    group('document', kind: 'file', role: 'final', parts: []),
  ]) {
    testWidgets('${other.id} does not count as a displayed final video', (
      tester,
    ) async {
      await render(tester, [...segments, other]);
      expect(find.text('one.mp4'), findsOneWidget);
      expect(find.byType(TextButton), findsNothing);
    });
  }

  testWidgets('legacy final segment metadata does not duplicate the card', (
    tester,
  ) async {
    await render(tester, [group('one', role: 'final')]);
    expect(find.text('one.mp4'), findsOneWidget);
    expect(find.byType(TextButton), findsNothing);
  });

  testWidgets(
    'legacy final video role result still folds, final-only has no collection',
    (tester) async {
      await render(tester, [
        ...segments,
        group('final', kind: 'video_final', role: 'result'),
      ]);
      expect(find.text('one.mp4'), findsNothing);
      expect(find.byType(TextButton), findsOneWidget);
      await render(tester, [finalVideo]);
      expect(find.byType(TextButton), findsNothing);
      expect(find.text('final.mp4'), findsOneWidget);
    },
  );
}
