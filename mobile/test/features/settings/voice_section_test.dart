import 'package:bossip_mobile/features/settings/api/settings_api.dart';
import 'package:bossip_mobile/features/settings/widgets/voice_section.dart';
import 'package:bossip_mobile/shared/api/assistant_profile.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import '../voice/voice_fakes.dart';

/// The API the section talks to, scripted.
class _FakeApi extends SettingsApi {
  _FakeApi() : super(Dio(BaseOptions(baseUrl: 'https://qa.example')));

  String selected = 'Serena';
  final saved = <String>[];

  @override
  Future<Map<String, dynamic>> getAssistantVoices() async => {
    'default': 'Serena',
    'selected': selected,
    'voices': [
      {
        'id': 'Tina',
        'name': '甜甜',
        'gender': 'female',
        'lang': 'zh',
        'description': '甜暖亲切',
        'description_en': 'Sweet',
      },
      {
        'id': 'Serena',
        'name': '苏瑶',
        'gender': 'female',
        'lang': 'zh',
        'description': '温柔小姐姐',
        'description_en': 'Gentle',
      },
      {
        'id': 'Andre',
        'name': '安德雷',
        'gender': 'male',
        'lang': 'zh',
        'description': '沉稳',
        'description_en': 'Steady',
      },
      {
        'id': 'Jennifer',
        'name': '詹妮弗',
        'gender': 'female',
        'lang': 'en',
        'description': '美式英语',
        'description_en': 'American',
      },
    ],
  };

  @override
  Future<void> setAssistantVoice(String voice) async {
    saved.add(voice);
    selected = voice;
  }
}

class _FakePlayer implements VoicePreviewPlayer {
  _FakePlayer(this.uri);

  final Uri uri;
  static final played = <Uri>[];
  static int disposed = 0;

  @override
  Future<void> play({required VoidCallback onDone}) async => played.add(uri);

  @override
  Future<void> dispose() async => disposed++;
}

/// The assistant's profile, scripted: what a call-habit switch saves.
class _ProfileApi extends AssistantProfileApi {
  _ProfileApi() : super(Dio(BaseOptions(baseUrl: 'https://qa.example')));

  AssistantProfile profile = const AssistantProfile();
  final saved = <Map<String, Object>>[];

  @override
  Future<AssistantProfile> get() async => profile;

  @override
  Future<AssistantProfile> save(Map<String, Object> patch) async {
    saved.add(patch);
    profile = AssistantProfile.fromJson({...profile.toJson(), ...patch});
    return profile;
  }
}

final _profile = _ProfileApi();

Future<_FakeApi> _section(WidgetTester tester) async {
  tester.view.physicalSize = const Size(390, 2400);
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.resetPhysicalSize);
  addTearDown(tester.view.resetDevicePixelRatio);
  final api = _FakeApi();
  await tester.pumpWidget(
    ProviderScope(
      overrides: [
        await zhI18n(tester),
        settingsApiProvider.overrideWithValue(api),
        assistantProfileApiProvider.overrideWithValue(_profile),
      ],
      child: MaterialApp(
        theme: testTheme(),
        home: const Scaffold(
          body: VoiceSection(playerFactory: _FakePlayer.new),
        ),
      ),
    ),
  );
  await tester.pumpAndSettle();
  return api;
}

void main() {
  testWidgets('groups the voices and marks the default and current one', (
    tester,
  ) async {
    await _section(tester);
    expect(find.text('中文 · 女声'), findsOneWidget);
    expect(find.text('中文 · 男声'), findsOneWidget);
    expect(find.text('英文'), findsOneWidget);
    expect(find.text('默认'), findsOneWidget);
    expect(find.text('当前'), findsOneWidget);
    expect(find.text('苏瑶'), findsOneWidget);
  });

  testWidgets('a pick is saved and becomes the current voice', (tester) async {
    final api = await _section(tester);
    await tester.tap(find.text('安德雷'));
    await tester.pumpAndSettle();
    expect(api.saved, ['Andre']);
    final andre = find.byKey(const ValueKey('voice-Andre'));
    expect(
      find.descendant(of: andre, matching: find.text('当前')),
      findsOneWidget,
    );
    await tester.tap(find.text('安德雷'));
    await tester.pumpAndSettle();
    expect(api.saved, ['Andre']); // already in use
  });

  testWidgets('a preview plays the voice sample and a second press stops it', (
    tester,
  ) async {
    _FakePlayer.played.clear();
    await _section(tester);
    await tester.tap(find.byKey(const ValueKey('voice-preview-Tina')));
    await tester.pump();
    expect(
      _FakePlayer.played.single.toString(),
      'https://qa.example/api/assistant/voice/samples/Tina',
    );
    expect(find.text('停止'), findsOneWidget);
    await tester.tap(find.byKey(const ValueKey('voice-preview-Tina')));
    await tester.pumpAndSettle();
    expect(find.text('停止'), findsNothing);
  });

  testWidgets('each call habit is saved at once', (tester) async {
    _profile
      ..profile = const AssistantProfile()
      ..saved.clear();
    await _section(tester);
    expect(find.text('通话习惯'), findsOneWidget);
    await tester.tap(find.byKey(const ValueKey('call-call_recap')));
    await tester.pumpAndSettle();
    expect(_profile.saved, [
      {'call_recap': false},
    ]);
    await tester.tap(find.byKey(const ValueKey('call-detail-detailed')));
    await tester.pumpAndSettle();
    expect(_profile.saved.last, {'call_detail': 'detailed'});
    // The one in use is not saved again.
    await tester.tap(find.byKey(const ValueKey('call-detail-detailed')));
    await tester.pumpAndSettle();
    expect(_profile.saved, hasLength(2));
  });
}
