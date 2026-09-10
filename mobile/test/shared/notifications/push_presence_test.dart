import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:bossip_mobile/shared/notifications/push_controller.dart';
import 'package:bossip_mobile/shared/notifications/system_notifications.dart';
import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

class _Native extends SystemNotifications {
  Map<String, dynamic>? scheduled;
  @override
  Future<void> refresh() async {}
  @override
  Future<void> clear() async {}
  @override
  Future<bool> showLocal(Map<String, dynamic> payload) async {
    scheduled = payload;
    return true;
  }
}

class _Adapter implements HttpClientAdapter {
  _Adapter(this.handle);
  final Future<Map<String, dynamic>> Function(RequestOptions) handle;
  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) async => ResponseBody.fromString(
    jsonEncode(await handle(options)),
    200,
    headers: {
      Headers.contentTypeHeader: ['application/json'],
    },
  );
  @override
  void close({bool force = false}) {}
}

Map<String, dynamic> _body(RequestOptions options) => options.data is String
    ? jsonDecode(options.data as String) as Map<String, dynamic>
    : Map<String, dynamic>.from(options.data as Map);

Future<void> _until(bool Function() ready) async {
  for (var i = 0; i < 100; i++) {
    if (ready()) return;
    await Future<void>.delayed(const Duration(milliseconds: 5));
  }
  fail('Expected asynchronous presence report');
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  late SharedPreferences prefs;
  late _Native native;
  late Dio dio;
  late PushController push;
  late List<Map<String, dynamic>> reports;
  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    prefs = await SharedPreferences.getInstance();
    native = _Native();
    reports = [];
    dio = Dio(BaseOptions(baseUrl: 'https://test.invalid'));
    dio.httpClientAdapter = _Adapter((o) async {
      if (o.path == '/api/push/presence') reports.add(_body(o));
      return {};
    });
    push = PushController(dio, native, prefs);
  });
  tearDown(() {
    push.dispose();
    native.dispose();
    dio.close();
  });

  test(
    'inactive remains visible; hidden and paused report background',
    () async {
      push.setLifecycle('resumed');
      push.setIdentity('user', 'session');
      await push.checkSession();
      for (final state in [
        'inactive',
        'hidden',
        'paused',
        'detached',
        'resumed',
      ]) {
        push.setLifecycle(state);
        await _until(
          () => reports.isNotEmpty && reports.last['state'] == state,
        );
        expect(push.foreground, state == 'inactive' || state == 'resumed');
      }
      final sequences = reports.map((r) => r['sequence'] as int).toList();
      expect(sequences.toSet().length, sequences.length);
      expect(sequences, orderedEquals([...sequences]..sort()));
      expect(reports.any((r) => r['state'] == 'offline'), isFalse);
    },
  );

  test('foreground report can overtake a stalled background request', () async {
    final release = Completer<Map<String, dynamic>>();
    Map<String, dynamic>? background;
    dio.httpClientAdapter = _Adapter((o) async {
      if (o.path != '/api/push/presence') return {};
      final body = _body(o);
      reports.add(body);
      if (body['state'] == 'paused') {
        background = body;
        return release.future;
      }
      return {};
    });
    push.setIdentity('user', 'session');
    push.setLifecycle('paused');
    await _until(() => background != null);
    push.setLifecycle('resumed');
    await _until(() => reports.last['state'] == 'resumed');
    expect(
      reports.last['sequence'] as int,
      greaterThan(background!['sequence'] as int),
    );
    release.complete({});
    await push.reportPresence();
    expect(push.lifecycle, 'resumed');
    expect(push.foreground, isTrue);
  });

  test('process restart preserves the monotonic report sequence', () async {
    push.setIdentity('user', 'session');
    push.setLifecycle('paused');
    await push.reportPresence();
    final previous = reports.last['sequence'] as int;
    push.dispose();
    push = PushController(dio, native, prefs);
    push.setLifecycle('resumed');
    push.setIdentity('user', 'session');
    await push.reportPresence();
    expect(reports.last['sequence'] as int, greaterThan(previous));
    expect(reports.last['state'], 'resumed');
  });

  test('failed networking never rewrites visible state to offline', () async {
    dio.httpClientAdapter = _Adapter((o) async {
      if (o.path == '/api/push/presence') {
        throw DioException(
          requestOptions: o,
          type: DioExceptionType.connectionError,
        );
      }
      return {};
    });
    push.setIdentity('user', 'session');
    push.setLifecycle('inactive');
    await push.reportPresence();
    expect(push.lifecycle, 'inactive');
    expect(push.foreground, isTrue);
  });

  test('local test is scheduled for background testing', () async {
    push.setIdentity('user', 'session');
    expect(await push.testLocal('Test', 'Body'), isTrue);
    expect(native.scheduled?['delaySeconds'], 10);
    expect(native.scheduled?['recipientId'], 'user');
  });
}
