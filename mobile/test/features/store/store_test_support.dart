import 'dart:convert';
import 'dart:typed_data';

import 'package:bossip_mobile/features/onboarding/state/onboarding_store.dart';
import 'package:bossip_mobile/features/store/models/store.dart';
import 'package:bossip_mobile/features/store/state/store_provider.dart';
import 'package:bossip_mobile/features/workspace/state/active_workspace_store.dart';
import 'package:bossip_mobile/shared/api/auth_store.dart';
import 'package:bossip_mobile/shared/models/auth_user.dart';
import 'package:dio/dio.dart';

/// One recorded call on the fake transport.
typedef Call = ({String method, String path, Object? data, Map<String, dynamic> query});

/// Fake Dio adapter: every request goes through [handle], which returns the
/// JSON body (status from [status], default 200).
class FakeAdapter implements HttpClientAdapter {
  FakeAdapter(this.handle, {this.status});

  final Future<Map<String, dynamic>> Function(RequestOptions) handle;
  final int Function(RequestOptions)? status;
  final calls = <Call>[];

  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) async {
    calls.add((
      method: options.method,
      path: options.path,
      data: options.data,
      query: options.queryParameters,
    ));
    return ResponseBody.fromString(
      jsonEncode(await handle(options)),
      status?.call(options) ?? 200,
      headers: {
        Headers.contentTypeHeader: ['application/json'],
      },
    );
  }

  @override
  void close({bool force = false}) {}
}

Map<String, dynamic> storeJson({
  String id = 'st1',
  String name = '泽岚鲜果',
  String category = 'food',
  List<String> platforms = const ['douyin_laike'],
  String personaStatus = 'none',
}) => {
  'id': id,
  'workspaceId': 'w1',
  'name': name,
  'category': category,
  'categoryOpen': true,
  'mainPlatforms': platforms,
  'address': null,
  'city': null,
  'platformBindings': <String, dynamic>{},
  'dataSources': <String, dynamic>{},
  'personaStatus': personaStatus,
  'personaSessionId': null,
  'personaStartedAt': null,
  'createdAt': '2026-09-23T00:00:00Z',
  'updatedAt': '2026-09-23T00:00:00Z',
};

Map<String, dynamic> storeListJson({Map<String, dynamic>? store}) => {
  'items': [?store],
  'categories': ['food', 'beauty', 'retail', 'other'],
  'openCategories': ['food'],
  'platforms': ['douyin_laike', 'meituan_merchant'],
};

class LoadedOnboarding extends OnboardingController {
  LoadedOnboarding([this.initial = const {}]);

  final Map<String, Object> initial;

  @override
  OnboardingState build() {
    ref.watch(authProvider);
    return OnboardingState(userId: 'u1', values: initial, loaded: true);
  }
}

class SignedIn extends AuthController {
  @override
  AuthState build() => const AuthState(
    isLoading: false,
    user: AuthUser(id: 'u1', username: 'wang', role: 'user'),
  );
}

class OneWorkspace extends ActiveWorkspaceController {
  @override
  Future<ActiveWorkspaceState> build() async =>
      const ActiveWorkspaceState(currentId: 'w1');
}

/// A store controller that answers without the transport; `create`/`patch`
/// still go through the API, so a save can be asserted on the fake adapter.
class FixedStore extends StoreController {
  FixedStore(this.snapshot);

  final StoreSnapshot snapshot;

  @override
  Future<StoreSnapshot> build() async => snapshot;
}
