import 'package:bossip_mobile/features/resources/utils/pick_source.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:file_picker/file_picker.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

I18nBundle _bundle() => I18nBundle({
  'en-US': {
    'resources': {
      'upload': {
        'source': 'Choose a source',
        'fromAlbum': 'Photo library',
        'fromPhotos': 'Photos',
        'fromVideos': 'Videos',
        'fromFiles': 'Browse files',
      },
    },
  },
});

/// Records the picker type each call asked for instead of opening anything.
class _RecordingPicker extends FilePickerPlatform {
  final types = <FileType>[];

  @override
  Future<List<PlatformFile>> pickFiles({
    String? dialogTitle,
    String? initialDirectory,
    FileType type = FileType.any,
    List<String>? allowedExtensions,
    void Function(FilePickerStatus)? onFileLoading,
    int compressionQuality = 0,
    AndroidOptions androidOptions = const AndroidOptions(),
    WindowsOptions windowsOptions = const WindowsOptions(),
    LinuxOptions linuxOptions = const LinuxOptions(),
    WebOptions webOptions = const WebOptions(),
  }) async {
    types.add(type);
    return const [];
  }
}

Future<_RecordingPicker> _pump(WidgetTester tester) async {
  SharedPreferences.setMockInitialValues({'bossip:lang': 'en-US'});
  final prefs = await SharedPreferences.getInstance();
  final picker = _RecordingPicker();
  FilePickerPlatform.instance = picker;
  await tester.pumpWidget(
    ProviderScope(
      overrides: [
        i18nProvider.overrideWith(() => I18nController(_bundle(), prefs)),
      ],
      child: MaterialApp(
        theme: ThemeData(
          extensions: [
            BossipTokens.resolve(BossipThemeName.default_, Brightness.light),
          ],
        ),
        home: Consumer(
          builder: (context, ref, _) => Scaffold(
            body: TextButton(
              onPressed: () => pickUploadFiles(context, ref),
              child: const Text('pick'),
            ),
          ),
        ),
      ),
    ),
  );
  // pump, not pumpAndSettle: the i18n controller keeps a listener alive.
  await tester.pump();
  await tester.pump();
  return picker;
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  testWidgets('iOS asks album or files, and the album goes through media',
      (tester) async {
    debugDefaultTargetPlatformOverride = TargetPlatform.iOS;
    final picker = await _pump(tester);

    await tester.tap(find.text('pick'));
    await tester.pump();
    await tester.pump(const Duration(seconds: 1));
    expect(find.text('Choose a source'), findsOneWidget);
    expect(find.text('Photo library'), findsOneWidget);
    expect(find.text('Photos'), findsNothing);
    expect(find.text('Browse files'), findsOneWidget);
    expect(picker.types, isEmpty);

    await tester.tap(find.text('Photo library'));
    await tester.pump();
    await tester.pump(const Duration(seconds: 1));
    expect(picker.types, [FileType.media]);
    // The binding checks this is back to null before the test ends.
    debugDefaultTargetPlatformOverride = null;
  });

  testWidgets('iOS "browse files" keeps the document picker', (tester) async {
    debugDefaultTargetPlatformOverride = TargetPlatform.iOS;
    final picker = await _pump(tester);

    await tester.tap(find.text('pick'));
    await tester.pump();
    await tester.pump(const Duration(seconds: 1));
    await tester.tap(find.text('Browse files'));
    await tester.pump();
    await tester.pump(const Duration(seconds: 1));
    expect(picker.types, [FileType.any]);
    debugDefaultTargetPlatformOverride = null;
  });

  testWidgets('Android offers photos and videos as image/* and video/*',
      (tester) async {
    // FileType.media would go out as */* and only the OEM file manager
    // answers; image and video types are what gallery apps register for.
    debugDefaultTargetPlatformOverride = TargetPlatform.android;
    final picker = await _pump(tester);

    await tester.tap(find.text('pick'));
    await tester.pump();
    await tester.pump(const Duration(seconds: 1));
    expect(find.text('Choose a source'), findsOneWidget);
    expect(find.text('Photo library'), findsNothing);
    expect(find.text('Photos'), findsOneWidget);
    expect(find.text('Videos'), findsOneWidget);
    expect(find.text('Browse files'), findsOneWidget);
    await tester.tap(find.text('Photos'));
    await tester.pump();
    await tester.pump(const Duration(seconds: 1));
    expect(picker.types, [FileType.image]);

    await tester.tap(find.text('pick'));
    await tester.pump();
    await tester.pump(const Duration(seconds: 1));
    await tester.tap(find.text('Videos'));
    await tester.pump();
    await tester.pump(const Duration(seconds: 1));
    expect(picker.types, [FileType.image, FileType.video]);
    debugDefaultTargetPlatformOverride = null;
  });

  testWidgets('backing out of the chooser picks nothing', (tester) async {
    debugDefaultTargetPlatformOverride = TargetPlatform.android;
    final picker = await _pump(tester);

    await tester.tap(find.text('pick'));
    await tester.pump();
    await tester.pump(const Duration(seconds: 1));
    // Tap the barrier above the sheet to dismiss it.
    await tester.tapAt(const Offset(200, 20));
    await tester.pump();
    await tester.pump(const Duration(seconds: 1));
    expect(find.text('Choose a source'), findsNothing);
    expect(picker.types, isEmpty);
    debugDefaultTargetPlatformOverride = null;
  });
}
