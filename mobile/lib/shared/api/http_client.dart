import 'package:cookie_jar/cookie_jar.dart';
import 'package:dio/dio.dart';
import 'package:dio_cookie_manager/dio_cookie_manager.dart';

import '../config/env.dart';
import 'auth_session.dart';
import 'workspace_scope.dart';

const _kRetried = 'bossip.retried';
const _kRequestUser = 'bossip.requestUser';
const _kRequestWorkspace = 'bossip.requestWorkspace';

/// Builds the app's Dio client, mirroring frontend-v2 `shared/api/http.ts`:
/// JSON content type, cookies included, `Authorization: Bearer` when a token
/// is present, and a single 401 → refresh → retry pass.
Dio buildApiDio({
  required AuthSession auth,
  required CookieJar cookieJar,
  required WorkspaceScope workspace,
}) {
  final dio = Dio(
    BaseOptions(
      baseUrl: Env.apiBase,
      contentType: 'application/json',
      connectTimeout: const Duration(seconds: 10),
    ),
  );
  dio.interceptors.add(CookieManager(cookieJar));
  dio.interceptors.add(
    InterceptorsWrapper(
      onRequest: (options, handler) {
        // A queued platform action can outlive an account/workspace switch.
        // Check at dispatch too, before attaching the new account's token.
        if (options.extra.containsKey(requestScopeUserKey) &&
            (options.extra[requestScopeUserKey] != auth.userId ||
                options.extra[requestScopeWorkspaceKey] !=
                    workspace.currentId)) {
          return handler.reject(
            DioException(
              requestOptions: options,
              type: DioExceptionType.cancel,
              message: 'Request scope changed',
            ),
          );
        }
        final token = auth.accessToken;
        final workspaceId = workspace.currentId;
        if (token != null) {
          options.headers['Authorization'] = 'Bearer $token';
        }
        if (workspaceId != null &&
            !options.headers.containsKey('X-Workspace-Id')) {
          options.headers['X-Workspace-Id'] = workspaceId;
        }
        options.extra[_kRequestUser] = auth.userId;
        options.extra[_kRequestWorkspace] =
            options.headers['X-Workspace-Id'] as String?;
        handler.next(options);
      },
      onError: (e, handler) async {
        final is401 = e.response?.statusCode == 401;
        final hadToken = auth.accessToken != null;
        final retried = e.requestOptions.extra[_kRetried] == true;
        if (!is401 || !hadToken || retried) return handler.next(e);
        final requestUser = e.requestOptions.extra[_kRequestUser] as String?;
        final requestWorkspace =
            e.requestOptions.extra[_kRequestWorkspace] as String?;
        final token = await auth.refresh();
        if (token == null) return handler.next(e);
        // Never replay a request into a different identity or workspace. This
        // matters most for order creation, where a stale retry is a write.
        if (auth.userId != requestUser ||
            workspace.currentId != requestWorkspace) {
          return handler.next(e);
        }
        final opts = e.requestOptions;
        opts.extra[_kRetried] = true;
        opts.headers['Authorization'] = 'Bearer $token';
        try {
          handler.resolve(await dio.fetch<dynamic>(opts));
        } on DioException catch (err) {
          handler.next(err);
        }
      },
    ),
  );
  return dio;
}

/// Bare client for the refresh round-trip itself (no auth interceptor, same
/// cookie jar) — avoids recursion, like the web's plain `fetch` refresh.
Dio buildRefreshDio({required CookieJar cookieJar}) {
  final dio = Dio(
    BaseOptions(
      baseUrl: Env.apiBase,
      contentType: 'application/json',
      connectTimeout: const Duration(seconds: 10),
    ),
  );
  dio.interceptors.add(CookieManager(cookieJar));
  return dio;
}
