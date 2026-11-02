import 'dart:typed_data';

import 'package:bossip_mobile/features/chat/chat_screen.dart';
import 'package:bossip_mobile/features/light/api/light_api.dart';
import 'package:bossip_mobile/features/light/widgets/light_input_bar.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:image_picker/image_picker.dart';
import 'package:shared_preferences/shared_preferences.dart';

class _Picker implements LightPhotoPicker {
  _Picker(this.files);

  final List<XFile> files;
  final calls = <String>[];

  @override
  Future<List<XFile>> pick({required bool camera, required int limit}) async {
    calls.add('pick');
    return files.take(limit).toList();
  }

  @override
  Future<List<XFile>> pickMedia({required int limit}) async {
    calls.add('pickMedia:$limit');
    return files.take(limit).toList();
  }
}

class _Api extends LightApi {
  _Api() : super(Dio());

  final uploads = <(String, String, int)>[];

  @override
  Future<String> uploadStream({
    required String name,
    required String mime,
    required int length,
    required Stream<List<int>> Function() stream,
    void Function(double fraction)? onProgress,
  }) async {
    await stream().drain<void>();
    onProgress?.call(1);
    uploads.add((name, mime, length));
    return 'asset_${uploads.length}';
  }
}

XFile _clip(String name, int bytes) =>
    XFile.fromData(Uint8List(bytes), path: '/picked/$name', length: bytes);


void main() {
  testWidgets('你拍我剪 picks clips from the album and uploads each one', (
    tester,
  ) async {
    SharedPreferences.setMockInitialValues({'bossip:lang': 'zh-CN'});
    final prefs = await SharedPreferences.getInstance();
    final bundle = (await tester.runAsync(I18nBundle.load))!;
    final picker = _Picker([
      _clip('IMG_0001.MOV', 1200),
      _clip('shop.mp4', 800),
      _clip('walk.m4v', 300),
    ]);
    final api = _Api();
    final sent = <(String, List<String>)>[];
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          prefsProvider.overrideWithValue(prefs),
          i18nProvider.overrideWith(() => I18nController(bundle, prefs)),
          lightApiProvider.overrideWithValue(api),
          lightPhotoPickerProvider.overrideWithValue(picker),
        ],
        child: MaterialApp(
          theme: ThemeData(
            extensions: [
              BossipTokens.resolve(BossipThemeName.default_, Brightness.light),
            ],
          ),
          home: Scaffold(
            body: Column(
              children: [
                const Spacer(),
                LightInputBar(
                  allowVideo: true,
                  maxPhotos: 10,
                  state: ChatInputState(
                    busy: false,
                    userTurns: 2,
                    hasPendingInput: false,
                    onSend: (text, ids) async => sent.add((text, ids)),
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('light-album')));
    await tester.pumpAndSettle();
    expect(picker.calls, ['pickMedia:10']);
    // Clips draw a camera tile instead of being decoded.
    expect(find.byIcon(Icons.videocam_outlined), findsNWidgets(3));
    await tester.runAsync(() async {
      await tester.tap(find.byKey(const Key('light-send')));
      await Future<void>.delayed(const Duration(milliseconds: 50));
    });
    await tester.pumpAndSettle();
    expect(api.uploads.map((u) => (u.$1, u.$2)).take(2), [
      ('IMG_0001.MOV', 'video/quicktime'),
      ('shop.mp4', 'video/mp4'),
    ]);
    expect(api.uploads.length, 3);
    expect(sent.single.$1, '素材传好了，帮我剪');
    expect(sent.single.$2, ['asset_1', 'asset_2', 'asset_3']);
  });

  test('a clip is told apart by type or extension', () {
    expect(
      lightIsVideo(XFile.fromData(Uint8List(1), path: '/p/a.MOV')),
      isTrue,
    );
    expect(
      lightIsVideo(
        XFile.fromData(Uint8List(1), path: '/p/x', mimeType: 'video/mp4'),
      ),
      isTrue,
    );
    expect(
      lightIsVideo(XFile.fromData(Uint8List(1), path: '/p/a.heic')),
      isFalse,
    );
  });
}
