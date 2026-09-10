import 'dart:async';
import 'dart:io';

import 'package:bossip_mobile/shared/download/native_download.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  const pathChannel = MethodChannel('plugins.flutter.io/path_provider');
  const saveChannel = MethodChannel('com.bossip.bipmobile/download');
  final messenger =
      TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger;
  late Directory directory;
  late List<MethodCall> calls;
  setUp(() async {
    directory = await Directory.systemTemp.createTemp('admin-download-test-');
    calls = [];
    messenger.setMockMethodCallHandler(
      pathChannel,
      (_) async => directory.path,
    );
    messenger.setMockMethodCallHandler(saveChannel, (call) async {
      calls.add(call);
      return true;
    });
  });
  tearDown(() async {
    messenger.setMockMethodCallHandler(pathChannel, null);
    messenger.setMockMethodCallHandler(saveChannel, null);
    await directory.delete(recursive: true);
  });
  test(
    'streamed ZIP stays byte-identical, uses native picker and cleans staging',
    () async {
      messenger.setMockMethodCallHandler(saveChannel, (call) async {
        calls.add(call);
        final args = call.arguments as Map<dynamic, dynamic>;
        expect(await File(args['path'] as String).readAsBytes(), [
          80,
          75,
          3,
          4,
          0,
          255,
        ]);
        expect(args['name'], 'skill.zip');
        expect(args['mimeType'], 'application/zip');
        return true;
      });
      final saved = await NativeDownloadService().saveStream(
        stream: Stream.fromIterable([
          [80, 75],
          [3, 4, 0, 255],
        ]),
        suggestedName: '../../skill.zip',
        checkAccess: () {},
      );
      expect(saved, isTrue);
      expect(calls.single.method, 'saveFile');
      expect(await directory.list().toList(), isEmpty);
    },
  );
  test('cancelled picker is not a successful save', () async {
    messenger.setMockMethodCallHandler(saveChannel, (_) async => false);
    final saved = await NativeDownloadService().saveStream(
      stream: Stream.value([80, 75]),
      suggestedName: 'cancel.zip',
      checkAccess: () {},
    );
    expect(saved, isFalse);
    expect(await directory.list().toList(), isEmpty);
  });
  test(
    'scope revoked after streaming prevents export and releases save lock',
    () async {
      var checks = 0;
      final service = NativeDownloadService();
      await expectLater(
        service.saveStream(
          stream: Stream.value([80, 75]),
          suggestedName: 'private.zip',
          checkAccess: () {
            if (++checks >= 3) throw StateError('scope changed');
          },
        ),
        throwsStateError,
      );
      expect(calls, isEmpty);
      expect(await directory.list().toList(), isEmpty);
      expect(
        await service.saveStream(
          stream: Stream.value([1]),
          suggestedName: 'next.zip',
          checkAccess: () {},
        ),
        isTrue,
      );
    },
  );
  test('stream failure never exposes a partial archive', () async {
    await expectLater(
      NativeDownloadService().saveStream(
        stream: Stream<List<int>>.error(StateError('read failed')),
        suggestedName: 'partial.zip',
        checkAccess: () {},
      ),
      throwsStateError,
    );
    expect(calls, isEmpty);
    expect(await directory.list().toList(), isEmpty);
  });
  test('only one archive export can be active', () async {
    final service = NativeDownloadService();
    final stream = StreamController<List<int>>();
    final first = service.saveStream(
      stream: stream.stream,
      suggestedName: 'first.zip',
      checkAccess: () {},
    );
    await expectLater(
      service.saveStream(
        stream: Stream.value([1]),
        suggestedName: 'second.zip',
        checkAccess: () {},
      ),
      throwsStateError,
    );
    stream.add([80, 75]);
    await stream.close();
    expect(await first, isTrue);
    expect(calls.length, 1);
  });
}
