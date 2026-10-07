import 'dart:async';
import 'dart:typed_data';

import 'package:bossip_mobile/app/knowledge_routes.dart';
import 'package:bossip_mobile/features/memory/state/document_upload.dart';
import 'package:bossip_mobile/features/workspace/state/active_workspace_store.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/download/native_download.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/models/workspace.dart';
import 'package:bossip_mobile/shared/widgets/toast.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:intl/date_symbol_data_local.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// A refusal the fake server answers with, shaped like the backend's.
class FakeHttpError implements Exception {
  const FakeHttpError(this.status, [this.body]);

  final int status;
  final Object? body;
}

/// A request handler: returns the response body, or throws [FakeHttpError].
typedef FakeHandler = FutureOr<Object?> Function(RequestOptions request);

Map<String, dynamic> memoryJson(
  String id,
  String summary, {
  int revision = 1,
  String? projectId,
  String updatedAt = '2026-10-02T08:00:00Z',
}) => {
  'id': id,
  'summary': summary,
  'type': 'USER_NOTE',
  'scope': 'LONG_TERM',
  'status': 'ACTIVE',
  'revision': revision,
  'project_id': projectId,
  'created_at': '2026-09-30T08:00:00Z',
  'updated_at': updatedAt,
};

Map<String, dynamic> topicJson(
  String id,
  String title, {
  String excerpt = '',
  bool bodyAvailable = true,
  String? projectId,
  List<String> sourceIds = const ['s1'],
  String slug = '',
}) => {
  'id': id,
  'slug': slug.isEmpty ? id : slug,
  'title': title,
  'revision': 1,
  'status': bodyAvailable ? 'published' : 'stale',
  'body_available': bodyAvailable,
  'excerpt': excerpt,
  'source_count': sourceIds.length,
  'source_ids': sourceIds,
  'project_id': projectId,
  'updated_at': '2026-10-02T09:00:00Z',
};

Map<String, dynamic> documentJson(
  String id,
  String filename, {
  String status = 'ready',
  List<String> pageIds = const [],
  String? reasonCode,
}) => {
  'id': id,
  'filename': filename,
  'revision': 1,
  'status': status,
  'reason_code': reasonCode,
  'chunk_count': 1,
  'indexed_chunks': 1,
  'page_ids': pageIds,
  'bytes': 2048,
  'created_at': '2026-10-01T08:00:00Z',
};

/// The backend as the knowledge page sees it: the web suite's fixture data,
/// every request recorded, any route overridable.
class FakeKnowledgeServer {
  final requests = <RequestOptions>[];

  List<Map<String, dynamic>> memories = [
    memoryJson('memory-1', 'Use Shanghai timezone', revision: 3),
    memoryJson('memory-2', 'Weekly report goes out on Friday'),
  ];
  int? memoriesNextOffset;
  List<Map<String, dynamic>> pages = [
    topicJson(
      'page-1',
      'Working hours',
      slug: 'working-hours',
      excerpt: '## Hours\n\nWe work from **Shanghai**. [source:s1@1]',
    ),
    topicJson('doc-page', 'Venue guide', excerpt: 'Doors open at nine.'),
  ];
  List<Map<String, dynamic>> documents = [
    documentJson('doc-1', 'venue-guide.pdf', pageIds: ['doc-page']),
  ];
  int cleanupPending = 0;
  Map<String, dynamic> processing = {'pending': 0, 'failed': <Object>[]};
  bool uploadsEnabled = true;
  List<Map<String, dynamic>> sources = [
    {
      'id': 'snapshot-1',
      'source_kind': 'user_statement',
      'session_id': 'session-1',
      'body': 'Original immutable evidence',
      'body_available': true,
    },
  ];
  List<Map<String, dynamic>> history = [
    {
      'revision': 2,
      'summary': 'Earlier value',
      'body_available': true,
      'reason': 'user_corrected',
    },
  ];
  Map<String, dynamic> cleanup = {'status': 'active', 'stopped': false};

  /// `METHOD /path` → handler, consulted before the defaults.
  final handlers = <String, FakeHandler>{};

  /// What was asked for, as `METHOD /path?query` in the order sent.
  List<String> get calls => [
    for (final r in requests)
      '${r.method} ${r.path}${r.queryParameters.isEmpty ? '' : '?${[for (final e in r.queryParameters.entries) '${Uri.encodeQueryComponent(e.key)}=${Uri.encodeQueryComponent('${e.value}')}'].join('&')}'}',
  ];

  List<RequestOptions> writes() =>
      requests.where((r) => r.method != 'GET').toList();

  Future<Object?> _route(RequestOptions o) async {
    final custom = handlers['${o.method} ${o.path}'];
    if (custom != null) return custom(o);
    final path = o.path;
    final query = o.queryParameters;
    if (o.method != 'GET') return const {'ok': true};
    if (path == '/api/agent/project') {
      return [
        {'id': 'p1', 'name': 'Project One'},
        {'id': 'p2', 'name': 'Project Two'},
      ];
    }
    if (path == '/api/memory-wiki/capabilities') {
      return {'enabled': uploadsEnabled};
    }
    if (path == '/api/memory-wiki/memory-groups') {
      return {
        'groups': [
          {
            'id': 'g1',
            'title': 'Working hours',
            'page_id': 'page-1',
            'memory_ids': ['memory-1'],
          },
        ],
      };
    }
    if (path == '/api/memory-wiki/library') {
      final q = '${query['query'] ?? ''}'.toLowerCase();
      return {
        'pages': [
          for (final page in pages)
            if (q.isEmpty || '${page['title']}'.toLowerCase().contains(q)) page,
        ],
        'next_offset': null,
      };
    }
    if (path == '/api/memory-documents') {
      return {
        'documents': documents,
        'next_offset': null,
        'cleanup_pending': cleanupPending,
      };
    }
    if (path == '/api/memories/processing') return processing;
    if (path == '/api/memories/settings') {
      return {'auto_save': true, 'session_paused': false};
    }
    if (path == '/api/memories') {
      final q = '${query['query'] ?? ''}'.toLowerCase();
      return {
        'memories': [
          for (final memory in memories)
            if (q.isEmpty || '${memory['summary']}'.toLowerCase().contains(q))
              memory,
        ],
        'next_offset': memoriesNextOffset,
      };
    }
    if (path.endsWith('/sources')) return {'sources': sources};
    if (path.endsWith('/history')) return {'revisions': history};
    if (path.endsWith('/cleanup')) return cleanup;
    final single = RegExp(r'^/api/memories/([^/]+)$').firstMatch(path);
    if (single != null) {
      final id = Uri.decodeComponent(single[1]!);
      final found = memories.where((m) => m['id'] == id).firstOrNull;
      if (found == null) throw const FakeHttpError(404, {'detail': 'gone'});
      return {...found, 'body_available': true};
    }
    throw FakeHttpError(404, {'detail': 'unexpected GET $path'});
  }

  Dio dio() {
    final dio = Dio(BaseOptions(baseUrl: 'https://test.invalid'));
    dio.interceptors.add(
      InterceptorsWrapper(
        onRequest: (options, handler) async {
          requests.add(options);
          try {
            final data = await _route(options);
            handler.resolve(
              Response<dynamic>(
                requestOptions: options,
                statusCode: 200,
                data: data,
                headers: Headers.fromMap({
                  'content-disposition': [
                    "attachment; filename*=UTF-8''venue-guide.pdf",
                  ],
                }),
              ),
            );
          } on FakeHttpError catch (error) {
            handler.reject(
              DioException(
                requestOptions: options,
                type: error.status == 0
                    ? DioExceptionType.connectionError
                    : DioExceptionType.badResponse,
                response: error.status == 0
                    ? null
                    : Response<dynamic>(
                        requestOptions: options,
                        statusCode: error.status,
                        data: error.body,
                      ),
              ),
            );
          }
        },
      ),
    );
    return dio;
  }
}

class FakeWorkspace extends ActiveWorkspaceController {
  @override
  Future<ActiveWorkspaceState> build() async {
    ref.read(workspaceScopeProvider).currentId = 'home';
    return const ActiveWorkspaceState(
      currentId: 'home',
      items: [
        WorkspaceSummary(
          id: 'home',
          name: 'Home',
          ownerUserId: 'user',
          kind: WorkspaceKind.personal,
          role: WorkspaceRole.owner,
        ),
        WorkspaceSummary(
          id: 'team',
          name: 'Team',
          ownerUserId: 'boss',
          kind: WorkspaceKind.team,
          role: WorkspaceRole.member,
        ),
      ],
    );
  }

  @override
  Future<void> select(String workspaceId) async {
    ref.read(workspaceScopeProvider).currentId = workspaceId;
    state = AsyncData(state.requireValue.copyWith(currentId: workspaceId));
  }
}

/// Records what would have been handed to the system save sheet.
class FakeDownloads extends NativeDownloadService {
  final saved = <({String name, String mimeType, Uint8List bytes})>[];

  @override
  Future<bool> saveBytes({
    required Uint8List bytes,
    required String suggestedName,
    String mimeType = 'image/png',
  }) async {
    saved.add((name: suggestedName, mimeType: mimeType, bytes: bytes));
    return true;
  }
}

late I18nBundle knowledgeBundle;

/// Loads the real zh-CN/en-US bundle and Chinese date symbols once.
void setUpKnowledgeTests() {
  setUpAll(() async {
    TestWidgetsFlutterBinding.ensureInitialized();
    knowledgeBundle = await I18nBundle.load();
    await initializeDateFormatting('zh_CN');
  });
}

class KnowledgeHarness {
  KnowledgeHarness(this.tester, this.router, this.downloads);

  final WidgetTester tester;
  final GoRouter router;
  final FakeDownloads downloads;

  /// Where the person is: a pushed page's location, else the router's.
  String get location {
    final configuration = router.routerDelegate.currentConfiguration;
    final last = configuration.last;
    return last is ImperativeRouteMatch
        ? last.matches.uri.toString()
        : configuration.uri.toString();
  }

  /// The scope's container. A controlled [ProviderScope] disposes it with
  /// the tree, cancelling every poll before the pending-timer check.
  ProviderContainer get container =>
      ProviderScope.containerOf(tester.element(find.byType(MaterialApp)));

  List<String> get toasts => [
    for (final toast in container.read(toastProvider)) toast.text,
  ];
}

/// Mounts the knowledge routes at [location] on a phone-sized screen.
Future<KnowledgeHarness> mountKnowledge(
  WidgetTester tester,
  FakeKnowledgeServer server, {
  String location = '/app/wiki',
  List<PickedDocument> picked = const [],
  Brightness brightness = Brightness.light,
}) async {
  tester.view.physicalSize = const Size(390 * 3, 844 * 3);
  tester.view.devicePixelRatio = 3;
  addTearDown(tester.view.reset);
  SharedPreferences.setMockInitialValues({'bossip:lang': 'zh-CN'});
  final prefs = await SharedPreferences.getInstance();
  final downloads = FakeDownloads();
  final router = GoRouter(
    initialLocation: location,
    routes: [
      ...knowledgeRoutes,
      GoRoute(path: '/app', builder: (_, _) => const Text('new chat')),
      GoRoute(
        path: '/app/s/:sessionId',
        builder: (_, state) =>
            Text('chat ${state.pathParameters['sessionId']}'),
      ),
    ],
  );
  addTearDown(router.dispose);
  await tester.pumpWidget(
    ProviderScope(
      overrides: [
        i18nProvider.overrideWith(() => I18nController(knowledgeBundle, prefs)),
        apiDioProvider.overrideWithValue(server.dio()),
        activeWorkspaceProvider.overrideWith(FakeWorkspace.new),
        documentPickerProvider.overrideWithValue(() async => picked),
        nativeDownloadProvider.overrideWithValue(downloads),
      ],
      // Like the app's WorkspaceBootstrap: nothing renders until the
      // workspace scope is known, so every read goes out once, scoped.
      child: Consumer(
        builder: (context, ref, _) =>
            ref.watch(activeWorkspaceProvider).hasValue
            ? MaterialApp.router(
                routerConfig: router,
                theme: ThemeData(
                  brightness: brightness,
                  extensions: [
                    BossipTokens.resolve(BossipThemeName.default_, brightness),
                  ],
                ),
              )
            : const SizedBox.shrink(),
      ),
    ),
  );
  await settle(tester);
  return KnowledgeHarness(tester, router, downloads);
}

/// Lets fake responses land without waiting on animations that never end.
Future<void> settle(WidgetTester tester, [int frames = 12]) async {
  for (var i = 0; i < frames; i++) {
    await tester.pump(const Duration(milliseconds: 50));
  }
}

/// Scrolls [finder] into view, then taps it.
Future<void> tapVisible(WidgetTester tester, Finder finder) async {
  await tester.ensureVisible(finder);
  await tester.pump();
  await tester.tap(finder);
  await settle(tester);
}

/// The body sent with the last write to [path].
Map<String, dynamic> lastBody(FakeKnowledgeServer server, String path) =>
    server.requests.lastWhere((r) => r.path == path && r.method != 'GET').data
        as Map<String, dynamic>;
