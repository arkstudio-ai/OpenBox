import 'package:bossip_mobile/features/chat/state/chat_session_controller.dart';
import 'package:bossip_mobile/features/chat/state/config_providers.dart';
import 'package:bossip_mobile/features/chat/utils/reasoning.dart';
import 'package:bossip_mobile/features/chat/widgets/composer/picker_sheets.dart';
import 'package:bossip_mobile/shared/api/auth_store.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/models/app_config.dart';
import 'package:bossip_mobile/shared/models/auth_user.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

I18nBundle _bundle() => I18nBundle({
  'en-US': {
    'chat': {
      'model': {'pick': 'Switch model'},
      'videoModel': {'pick': 'Switch video model'},
      'tier': {
        'chat': {'pick': 'Model tier', 'high': 'Deep', 'medium': 'Pro', 'low': 'Fast'},
        'video': {
          'pick': 'Video tier',
          'high': 'High',
          'medium': 'Medium',
          'low': 'Low',
          'perSecond': '{{price}} credits/s',
        },
        'more': 'More models…',
      },
    },
  },
});

const _config = AppConfig(
  models: [
    ModelInfo(id: 'openai/gemini-3.8-flash', name: 'Gemini 3.8 Flash', variants: ['low', 'medium', 'high'], defaultVariant: 'medium'),
    ModelInfo(id: 'openai/qwen3.8-max', name: 'Qwen3.8 Max', variants: ['none', 'low', 'medium', 'xhigh'], defaultVariant: 'xhigh'),
    ModelInfo(id: 'openai/qwen3.8-flash', name: 'Qwen3.8 Flash', variants: ['none', 'low', 'medium', 'xhigh'], defaultVariant: 'xhigh'),
  ],
  defaultModel: 'openai/gemini-3.8-flash',
  videoModels: [
    VideoModelInfo(id: 'video-sd-1080p-pro', name: 'SD 1080p Pro', resolutions: ['1080p']),
    VideoModelInfo(id: 'wan3.0-video', name: 'Wan 3.0', resolutions: ['480p', '720p', '1080p']),
    VideoModelInfo(id: 'MiniMax-H3', name: 'MiniMax H3', resolutions: ['480p', '512p', '768p', '2k']),
  ],
  defaultVideoModel: 'wan3.0-video',
  defaultVideoResolution: '720p',
  modelTiers: ModelTiers(
    chat: [
      ChatTierRow(tier: 'high', model: 'openai/qwen3.8-max', variant: 'xhigh'),
      ChatTierRow(tier: 'medium', model: 'openai/gemini-3.8-flash', variant: 'medium'),
      ChatTierRow(tier: 'low', model: 'openai/qwen3.8-flash', variant: 'low'),
    ],
    video: [
      VideoTierRow(
        tier: 'high',
        model: 'video-sd-1080p-pro',
        label: '高清',
        description: '画质优先',
        resolutions: ['1080p'],
        resolution: '1080p',
        prices: {'1080p': '0.50'},
      ),
      VideoTierRow(
        tier: 'medium',
        model: 'wan3.0-video',
        resolutions: ['720p', '1080p'],
        resolution: '720p',
        prices: {'720p': '0.60', '1080p': '1.20'},
      ),
      VideoTierRow(
        tier: 'low',
        model: 'MiniMax-H3',
        label: '省钱',
        resolutions: ['768p'],
        resolution: '768p',
      ),
    ],
  ),
);

class _Auth extends AuthController {
  _Auth(this.role);
  final String role;
  @override
  AuthState build() => AuthState(
        user: AuthUser(id: 'u1', username: 'u', role: role),
        isLoading: false,
      );
}

Future<ProviderContainer> _open(
  WidgetTester tester, {
  required bool video,
  String role = 'user',
}) async {
  SharedPreferences.setMockInitialValues({});
  final prefs = await SharedPreferences.getInstance();
  late ProviderContainer container;

  await tester.pumpWidget(
    ProviderScope(
      overrides: [
        i18nProvider.overrideWith(() => I18nController(_bundle(), prefs)),
        appConfigProvider.overrideWith((ref) => _config),
        authProvider.overrideWith(() => _Auth(role)),
      ],
      child: MaterialApp(
        theme: ThemeData(
          extensions: [
            BossipTokens.resolve(BossipThemeName.default_, Brightness.light),
          ],
        ),
        home: Consumer(
          builder: (context, ref, _) {
            container = ProviderScope.containerOf(context);
            return Scaffold(
              body: Center(
                child: ElevatedButton(
                  onPressed: () => video
                      ? showVideoTierPicker(
                          context,
                          ref,
                          sessionKey: 's1',
                          currentModel: null,
                          currentResolution: null,
                        )
                      : showChatTierPicker(
                          context,
                          ref,
                          sessionKey: 's1',
                          currentModel: null,
                          currentVariant: null,
                        ),
                  child: const Text('open'),
                ),
              ),
            );
          },
        ),
      ),
    ),
  );
  await tester.tap(find.text('open'));
  await tester.pumpAndSettle();
  return container;
}

void main() {
  testWidgets('chat tiers show the resolved model and the default is checked',
      (tester) async {
    await _open(tester, video: false);
    expect(find.text('Deep'), findsOneWidget);
    expect(find.text('Qwen3.8 Max'), findsOneWidget);
    final pro = tester.widget<ListTile>(
      find.ancestor(of: find.text('Pro'), matching: find.byType(ListTile)),
    );
    expect(pro.trailing, isA<Icon>());
    // Nobody but an admin sees the catalogue behind the tiers.
    expect(find.text('More models…'), findsNothing);
  });

  testWidgets('a chat tier picks its model and its strength in one tap',
      (tester) async {
    final container = await _open(tester, video: false);
    await tester.tap(find.text('Deep'));
    await tester.pumpAndSettle();

    expect(container.read(pickedModelProvider('s1')), 'openai/qwen3.8-max');
    expect(
      container.read(pickedVariantProvider(reasoningKey('s1', 'openai/qwen3.8-max'))),
      const Variant('xhigh'),
    );
    expect(find.byType(ListTile), findsNothing);
  });

  testWidgets('a single-resolution video tier picks the pair outright',
      (tester) async {
    final container = await _open(tester, video: true);
    // Deployment wording wins over the UI's; the description and model show.
    expect(find.text('高清'), findsOneWidget);
    expect(find.text('画质优先 · SD 1080p Pro'), findsOneWidget);
    expect(find.text('省钱'), findsOneWidget);
    await tester.tap(find.text('省钱'));
    await tester.pumpAndSettle();

    final pick = container.read(pickedVideoProvider('s1'));
    expect(pick?.modelId, 'MiniMax-H3');
    expect(pick?.resolution, '768p');
    expect(find.byType(ListTile), findsNothing);
  });

  testWidgets('a multi-resolution video tier asks, with the price beside each',
      (tester) async {
    final container = await _open(tester, video: true);
    await tester.tap(find.text('Medium'));
    await tester.pumpAndSettle();
    expect(find.text('720p · 0.60 credits/s'), findsOneWidget);
    expect(find.text('1080p · 1.20 credits/s'), findsOneWidget);
    await tester.tap(find.text('1080p · 1.20 credits/s'));
    await tester.pumpAndSettle();

    final pick = container.read(pickedVideoProvider('s1'));
    expect(pick?.modelId, 'wan3.0-video');
    expect(pick?.resolution, '1080p');
  });

  testWidgets('an admin can reach the catalogue behind the tiers',
      (tester) async {
    await _open(tester, video: false, role: 'admin');
    await tester.tap(find.text('More models…'));
    await tester.pumpAndSettle();
    // The plain model picker, with every model by name.
    expect(find.text('Switch model'), findsOneWidget);
    expect(find.text('Gemini 3.8 Flash'), findsOneWidget);
  });

  test('tier resolution helpers', () {
    expect(activeChatTier(_config, 'openai/qwen3.8-flash'), 'low');
    expect(activeChatTier(_config, 'openai/deepseek-v4-pro'), isNull);
    expect(activeVideoTier(_config, 'wan3.0-video', '720p'), 'medium');
    expect(activeVideoTier(_config, 'wan3.0-video', '1080p'), 'medium');
    // Outside the tier's resolutions is not the tier any more.
    expect(activeVideoTier(_config, 'wan3.0-video', '480p'), isNull);
  });

  test('model_tiers parses from the config payload', () {
    final parsed = AppConfig.fromJson({
      'models': <Map<String, dynamic>>[],
      'model_tiers': {
        'chat': [
          {'tier': 'high', 'model': 'm1', 'variant': null},
        ],
        'video': [
          {
            'tier': 'low',
            'model': 'v1',
            'resolutions': ['480p', '720p'],
            'resolution': '480p',
            'prices': {'480p': '0.30'},
          },
          {'tier': '', 'model': 'dropped'},
        ],
      },
    });
    expect(parsed.modelTiers.chat.single.variant, isNull);
    expect(parsed.modelTiers.video.single.resolution, '480p');
    expect(parsed.modelTiers.video.single.resolutions, ['480p', '720p']);
    expect(parsed.modelTiers.video.single.prices, {'480p': '0.30'});
    expect(const AppConfig(models: []).modelTiers.chat, isEmpty);
  });
}
