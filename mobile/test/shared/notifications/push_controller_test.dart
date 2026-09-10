import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:bossip_mobile/shared/notifications/push_controller.dart';
import 'package:bossip_mobile/shared/notifications/system_notifications.dart';
import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

class _Native extends SystemNotifications {
  int authorizations = 0;
  int clears = 0;
  bool allow = true;
  @override
  Future<void> refresh() async {}
  @override
  Future<bool> requestAuthorization() async {
    authorizations++;
    status = allow ? 'granted' : 'denied';
    return allow;
  }

  @override
  Future<void> clear() async {
    clears++;
  }

  void rotate(String next) {
    token = next;
    notifyListeners();
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

Map<String, dynamic> _body(RequestOptions o) => o.data is String
    ? jsonDecode(o.data as String) as Map<String, dynamic>
    : Map<String, dynamic>.from(o.data as Map);

Future<void> _until(PushController push, bool Function() condition) async {
  if (condition()) return;
  final done = Completer<void>();
  void listener() {
    if (condition() && !done.isCompleted) done.complete();
  }

  push.addListener(listener);
  try {
    await done.future.timeout(const Duration(seconds: 3));
  } finally {
    push.removeListener(listener);
  }
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  late _Native native;
  late Dio dio;
  late PushController push;
  late List<Map<String, dynamic>> registrations;
  setUp(() async {
    SharedPreferences.setMockInitialValues({});
    native = _Native()
      ..status = 'granted'
      ..token = 'token1';
    registrations = [];
    dio = Dio(BaseOptions(baseUrl: 'https://test.invalid'));
    dio.httpClientAdapter = _Adapter((o) async {
      if (o.path == '/api/push/status') return {};
      if (o.path == '/api/push/presence') return {};
      registrations.add(_body(o));
      return {
        'bindingId': 'binding${registrations.length}',
        'deliveryEnabled': true,
      };
    });
    push = PushController(dio, native, await SharedPreferences.getInstance());
  });
  tearDown(() {
    push.dispose();
    native.dispose();
    dio.close();
  });

  test(
    'login registers without prompting, token rotation switches the endpoint',
    () async {
      push.setIdentity('user', 'session');
      await _until(push, () => push.deliveryReady);
      expect(native.authorizations, 0);
      expect(registrations.first['token'], 'token1');
      native.rotate('token2');
      await _until(
        push,
        () =>
            registrations.last['token'] == 'token2' &&
            push.bindingId != 'binding1',
      );
      expect(push.deliveryReady, isTrue);
    },
  );

  test(
    'late registration cannot restore the previous account binding',
    () async {
      final started = Completer<void>();
      final release = Completer<Map<String, dynamic>>();
      var requests = 0;
      dio.httpClientAdapter = _Adapter((o) async {
        if (o.path == '/api/push/status') return {};
        if (o.path == '/api/push/presence') return {};
        if (++requests == 1) {
          started.complete();
          return release.future;
        }
        return {'bindingId': 'new-binding', 'deliveryEnabled': true};
      });
      push.setIdentity('old-user', 'old-session');
      await started.future;
      push.setIdentity('new-user', 'new-session');
      release.complete({'bindingId': 'old-binding', 'deliveryEnabled': true});
      await _until(push, () => push.bindingId == 'new-binding');
      expect(push.userId, 'new-user');
      expect(native.clears, 1);
    },
  );

  test(
    'denied permission still checks login but never enables delivery',
    () async {
      native.status = 'denied';
      native.allow = false;
      push.setIdentity('user', 'session');
      await _until(push, () => push.bindingId != null);
      expect(registrations.last['notificationsEnabled'], isFalse);
      await push.setEnabled(true);
      expect(native.authorizations, 1);
      expect(push.deliveryReady, isFalse);
    },
  );

  test(
    'explicit opt-out survives session refresh and re-enabling is explicit',
    () async {
      push.setIdentity('user', 'session');
      await _until(push, () => push.deliveryReady);
      await push.setEnabled(false);
      await push.checkSession();
      expect(registrations.last['notificationsEnabled'], isFalse);
      expect(push.wanted, isFalse);
      expect(push.deliveryReady, isFalse);
      await push.setEnabled(true);
      expect(registrations.last['notificationsEnabled'], isTrue);
      expect(push.deliveryReady, isTrue);
    },
  );

  test(
    'registered token with an unconfigured server is not reported as ready',
    () async {
      dio.httpClientAdapter = _Adapter(
        (o) async => o.path == '/api/push/status'
            ? {}
            : {'bindingId': 'binding', 'deliveryEnabled': false},
      );
      push.setIdentity('user', 'session');
      await _until(push, () => push.bindingId != null);
      expect(push.deliveryReady, isFalse);
      expect(push.errorCode, 'PUSH_NOT_CONFIGURED');
      await expectLater(push.testRemote(), throwsStateError);
    },
  );
}
