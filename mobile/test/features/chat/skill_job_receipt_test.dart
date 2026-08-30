import 'package:bossip_mobile/features/chat/api/assets_api.dart';
import 'package:bossip_mobile/features/chat/widgets/cards/skill_job_receipt.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/models/message_part.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

I18nBundle _bundle() => I18nBundle({
  'en-US': {
    'jobs': {
      'status': {
        'succeeded': 'Completed',
        'failed': 'Failed',
        'cancelled': 'Cancelled',
      },
    },
    'common': {
      'state': {'unavailable': 'Unavailable'},
    },
  },
});

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  group('SkillJobPart.fromJson', () {
    test('parses the complete durable artifact envelope', () {
      final part =
          MessagePart.fromJson({
                'type': 'skill_job',
                'id': 'receipt-1',
                'jobId': 'job-1',
                'skillKey': 'builtin:video-production',
                'operation': 'segment.generate',
                'status': 'succeeded',
                'summary': 'done',
                'artifacts': [
                  {
                    'assetId': 'asset-video',
                    'name': 'video.mp4',
                    'mime': 'video/mp4',
                  },
                ],
              })
              as SkillJobPart;

      expect(part.artifacts, hasLength(1));
      expect(part.artifacts.single.assetId, 'asset-video');
      expect(part.artifacts.single.name, 'video.mp4');
      expect(part.artifacts.single.mime, 'video/mp4');
    });

    test('keeps legacy and missing receipt fields readable', () {
      final legacy =
          MessagePart.fromJson({
                'type': 'skill_job',
                'id': 'receipt-legacy',
                'skillKey': 'builtin:demo-echo',
                'status': 'succeeded',
              })
              as SkillJobPart;
      final missing =
          MessagePart.fromJson({
                'type': 'skill_job',
                'id': 'receipt-missing',
                'artifacts': [
                  {'name': 'missing-id.mp4'},
                  'invalid-entry',
                ],
              })
              as SkillJobPart;

      expect(legacy.artifacts, isEmpty);
      expect(legacy.operation, isEmpty);
      expect(missing.skillKey, isEmpty);
      expect(missing.status, isEmpty);
      expect(missing.artifacts, hasLength(1));
      expect(missing.artifacts.single.assetId, isEmpty);
    });
  });

  test(
    'status, title, and artifact kind preserve unknown and missing values',
    () {
      final i18n = I18nState(language: 'en-US', bundle: _bundle());
      const missing = SkillJobPart(
        id: 'missing',
        jobId: '',
        skillKey: '',
        operation: '',
        status: '',
      );
      const partial = SkillJobPart(
        id: 'partial',
        jobId: '',
        skillKey: 'user:custom-skill',
        operation: '',
        status: 'timed_out',
      );

      expect(skillJobReceiptStatusLabel(i18n, ' timed_out '), 'timed_out');
      expect(skillJobReceiptStatusLabel(i18n, ''), 'Unavailable');
      expect(skillJobReceiptStatusLabel(i18n, 'cancelled'), 'Cancelled');
      expect(skillJobReceiptTitle(missing), isEmpty);
      expect(skillJobReceiptTitle(partial), 'custom-skill');
      expect(
        skillJobReceiptArtifactKind('VIDEO/MP4'),
        SkillJobReceiptArtifactKind.video,
      );
      expect(
        skillJobReceiptArtifactKind('image/png'),
        SkillJobReceiptArtifactKind.image,
      );
      expect(
        skillJobReceiptArtifactKind('application/pdf'),
        SkillJobReceiptArtifactKind.file,
      );
    },
  );

  testWidgets('renders video, image, file, and unavailable artifact states', (
    tester,
  ) async {
    SharedPreferences.setMockInitialValues({});
    final prefs = await SharedPreferences.getInstance();
    final bundle = _bundle();
    const part = SkillJobPart(
      id: 'receipt-artifacts',
      jobId: 'job-1',
      skillKey: 'builtin:video-production',
      operation: 'segment.generate',
      status: 'succeeded',
      artifacts: [
        SkillJobArtifact(
          assetId: 'asset-video',
          name: 'video.mp4',
          mime: 'video/mp4',
        ),
        SkillJobArtifact(
          assetId: 'asset-image',
          name: 'image.png',
          mime: 'image/png',
        ),
        SkillJobArtifact(assetId: 'asset-file'),
        SkillJobArtifact(assetId: 'asset-dead', name: 'dead.mp4'),
        SkillJobArtifact(name: 'missing-id.mp4', mime: 'video/mp4'),
      ],
    );

    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          i18nProvider.overrideWith(() => I18nController(bundle, prefs)),
          assetUrlProvider.overrideWith((ref, assetId) async {
            if (assetId == 'asset-dead') throw StateError('gone');
            return AssetUrl(
              url: 'https://assets.test/$assetId',
              mime: switch (assetId) {
                'asset-video' => 'video/mp4',
                'asset-image' => 'image/png',
                _ => 'application/pdf',
              },
              name: assetId == 'asset-file' ? 'from-api.pdf' : null,
            );
          }),
        ],
        child: MaterialApp(
          theme: ThemeData(
            extensions: [
              BossipTokens.resolve(BossipThemeName.default_, Brightness.light),
            ],
          ),
          home: const Scaffold(
            body: SingleChildScrollView(child: SkillJobReceipts(parts: [part])),
          ),
        ),
      ),
    );
    await tester.pump();

    expect(
      find.byKey(const ValueKey('skill-job-artifact-video-asset-video')),
      findsOneWidget,
    );
    expect(
      find.byKey(const ValueKey('skill-job-artifact-image-asset-image')),
      findsOneWidget,
    );
    expect(
      find.byKey(const ValueKey('skill-job-artifact-file-asset-file')),
      findsOneWidget,
    );
    expect(find.text('from-api.pdf'), findsOneWidget);
    expect(
      find.byKey(const ValueKey('skill-job-artifact-unavailable')),
      findsNWidgets(2),
    );
    expect(find.text('dead.mp4 · Unavailable'), findsOneWidget);
    expect(find.text('missing-id.mp4 · Unavailable'), findsOneWidget);
  });
}
