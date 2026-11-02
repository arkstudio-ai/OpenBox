import 'package:bossip_mobile/features/light/utils/light_steps.dart';
import 'package:bossip_mobile/features/light/widgets/step_bar.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/models/interaction.dart';
import 'package:bossip_mobile/shared/models/light.dart';
import 'package:bossip_mobile/shared/models/message.dart';
import 'package:bossip_mobile/shared/models/message_part.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

const _steps = [
  LightStep(id: 'topic', label: '选主题'),
  LightStep(id: 'photos', label: '拍照片'),
  LightStep(id: 'confirm', label: '定讲稿'),
  LightStep(id: 'generate', label: '生成'),
];

void main() {
  group('step helpers', () {
    test('the card names its step; the last question wins', () {
      final request = QuestionRequest(
        id: 'q1',
        sessionId: 's1',
        questions: const [
          QuestionItem(
            question: 'Which?',
            detail: {
              'light': {'entry_id': 'video', 'step_id': 'topic'},
            },
          ),
          QuestionItem(
            question: 'Photos?',
            detail: {
              'light': {'entry_id': 'video', 'step_id': 'photos'},
            },
          ),
        ],
      );
      expect(lightStepIdOf(request), 'photos');
      expect(lightStepIndex(_steps, 'photos'), 1);
      expect(lightStepIndex(_steps, 'confirm'), 2);
    });

    test('a plain card or an unknown step lights nothing', () {
      const plain = QuestionRequest(
        id: 'q2',
        sessionId: 's1',
        questions: [QuestionItem(question: 'Plain?')],
      );
      expect(lightStepIdOf(plain), isNull);
      expect(lightStepIndex(_steps, null), -1);
      expect(lightStepIndex(_steps, 'deliver'), -1);
    });

    test('entries parse their steps, subtypes and availability', () {
      final view = LightEntriesView.fromJson({
        'version': '2026-10-31',
        'ui_mode': 'light',
        'plan_id': 'free',
        'desktop': false,
        'entries': [
          {
            'id': 'video',
            'title': '做一条视频',
            'kind': 'primary',
            'requires': <String, dynamic>{},
            'steps': [
              {'id': 'topic', 'label': '选主题'},
            ],
            'subtypes': [
              {
                'id': 'dish',
                'label': '餐品展示',
                'samples': [
                  {
                    'id': 'dish_closeup',
                    'label': '烟火气特写',
                    'description': '热气',
                    'needs': '1–3 张',
                  },
                ],
              },
            ],
            'opening_card': {
              'text': '先挑一个样子',
              'chips': ['我拍好了'],
            },
            'sample_media': <dynamic>[],
            'availability': 'ok',
          },
          {
            'id': 'douyin_publish',
            'title': '发到抖音',
            'kind': 'secondary',
            'requires': {'desktop': true},
            'availability': 'needs_desktop',
          },
          {
            'id': 'footage_edit',
            'title': '你拍我剪',
            'kind': 'coming_soon',
            'availability': 'coming_soon',
          },
        ],
      });
      expect(view.paidPlan, isFalse);
      final video = view.byId('video')!;
      expect(video.hasGallery, isTrue);
      expect(video.available, isTrue);
      expect(video.subtypes.single.samples.single.mediaUrl, isNull);
      expect(video.openingCard.chips, ['我拍好了']);
      expect(video.stepById('topic')?.label, '选主题');
      final publish = view.byId('douyin_publish')!;
      expect(publish.availability, LightAvailability.needsDesktop);
      expect(publish.requiresDesktop, isTrue);
      expect(publish.hasGallery, isFalse);
      expect(view.byId('footage_edit')!.isComingSoon, isTrue);
      expect(view.byId('nope'), isNull);
    });

    test('a clip sample shows its still; an image sample shows itself', () {
      final clip = LightSample.fromJson({
        'id': 'dish_closeup',
        'label': '烟火气特写',
        'media_url':
            'https://oss.example/light/dish_closeup.mp4?Expires=1&Signature=x',
        'poster_url': 'https://oss.example/light/dish_closeup.jpg?Expires=1',
      })!;
      expect(clip.isVideo, isTrue);
      expect(clip.thumbUrl, contains('dish_closeup.jpg'));
      final bare = LightSample.fromJson({
        'id': 'dish_dark',
        'label': '暗调',
        'media_url': 'https://oss.example/light/dish_dark.mp4',
      })!;
      expect(bare.isVideo, isTrue);
      expect(bare.thumbUrl, isNull);
      final image = LightSample.fromJson({
        'id': 'menu_red',
        'label': '烟火红',
        'media_url': 'https://oss.example/light/menu_red.jpg',
      })!;
      expect(image.isVideo, isFalse);
      expect(image.thumbUrl, image.mediaUrl);
    });
  });

  test('a delivered final video or image counts as the last step', () {
    ChatMessage message(String role, List<MessagePart> parts) =>
        ChatMessage(id: 'm-$role', sessionId: 's', role: role, parts: parts);
    const photo = FilePart(
      id: 'p1',
      path: 'uploads/dish.jpg',
      mimeType: 'image/jpeg',
      assetId: 'asset_photo',
    );
    const segment = FilePart(
      id: 'p2',
      path: 'seg.mp4',
      mimeType: 'video/mp4',
      assetId: 'asset_seg',
      relation: FileRelation(role: 'intermediate'),
    );
    const clip = FilePart(
      id: 'p3',
      path: 'final.mp4',
      mimeType: 'video/mp4',
      assetId: 'asset_final',
      relation: FileRelation(role: 'final'),
    );
    expect(
      lightResultDelivered([
        message('user', [photo]),
      ]),
      isFalse,
    );
    expect(
      lightResultDelivered([
        message('assistant', [segment]),
      ]),
      isFalse,
    );
    expect(
      lightResultDelivered([
        message('user', [photo]),
        message('assistant', [segment, clip]),
      ]),
      isTrue,
    );
  });

  testWidgets('the step bar lights done, current and working steps', (
    tester,
  ) async {
    await tester.pumpWidget(
      MaterialApp(
        theme: ThemeData(
          extensions: [
            BossipTokens.resolve(BossipThemeName.default_, Brightness.light),
          ],
        ),
        home: const Scaffold(
          body: LightStepBar(steps: _steps, activeIndex: 1, workingIndex: 2),
        ),
      ),
    );
    for (final step in _steps) {
      expect(find.byKey(Key('light-step-${step.id}')), findsOneWidget);
      expect(find.text(step.label), findsOneWidget);
    }
    // Done: a check instead of its number. Current and todo: their numbers.
    expect(find.byIcon(Icons.check), findsOneWidget);
    expect(find.text('1'), findsNothing);
    expect(find.text('2'), findsOneWidget);
    expect(find.text('4'), findsOneWidget);
    // Working: a spinner instead of its number.
    expect(find.byType(CircularProgressIndicator), findsOneWidget);
    expect(find.text('3'), findsNothing);
  });
}
