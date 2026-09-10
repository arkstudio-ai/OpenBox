import 'dart:async';
import 'dart:math' as math;

import 'package:bossip_mobile/features/chat/api/chat_api.dart';
import 'package:bossip_mobile/features/chat/state/config_providers.dart';
import 'package:bossip_mobile/shared/api/auth_store.dart';
import 'package:bossip_mobile/shared/api/containers_api.dart';
import 'package:bossip_mobile/shared/api/providers.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/models/app_config.dart';
import 'package:bossip_mobile/shared/models/auth_user.dart';
import 'package:bossip_mobile/shared/models/interaction.dart';
import 'package:bossip_mobile/shared/models/message.dart';
import 'package:bossip_mobile/shared/models/message_part.dart';
import 'package:bossip_mobile/shared/models/session.dart';
import 'package:bossip_mobile/shared/ws/ws_client.dart';
import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:shared_preferences/shared_preferences.dart';

const sendSuggestion = NextStepSuggestion(
  label: '补充自动化测试',
  prompt: '请为刚完成的功能补充自动化测试，覆盖正常流程与失败分支。',
  mode: SuggestionMode.send,
);
const draftSuggestion = NextStepSuggestion(
  label: '调整界面风格',
  prompt: '请将当前界面调整为以下风格：[补充风格与参考]。',
  mode: SuggestionMode.draft,
);
const testSuggestions = SuggestionsPart(
  id: 'part-suggestions',
  items: [
    sendSuggestion,
    draftSuggestion,
    NextStepSuggestion(
      label: '检查移动端布局',
      prompt: '请检查当前功能在原生移动端的布局与交互。',
      mode: SuggestionMode.send,
    ),
  ],
);

const layoutSuggestions = SuggestionsPart(
  id: 'layout-suggestions',
  items: [
    NextStepSuggestion(
      label: '调整简报关注领域',
      prompt: '调整简报关注领域。',
      mode: SuggestionMode.send,
    ),
    NextStepSuggestion(
      label: '微调推送格式为精简版',
      prompt: '请将推送格式调整为：[补充要求]。',
      mode: SuggestionMode.draft,
    ),
    NextStepSuggestion(
      label: '修改每日推送时间',
      prompt: '修改每日推送时间。',
      mode: SuggestionMode.send,
    ),
  ],
);

ChatMessage answer({
  String id = 'm001',
  String sessionId = 's1',
  String? finish = 'stop',
  List<MessagePart> parts = const [testSuggestions],
  Map<String, dynamic>? error,
}) => ChatMessage(
  id: id,
  sessionId: sessionId,
  role: 'assistant',
  finish: finish,
  error: error,
  parts: parts,
);

class SuggestionWs extends AgentWsClient {
  SuggestionWs() : super(Dio());
  final frames = StreamController<WsEvent>.broadcast(sync: true);
  @override
  Stream<WsEvent> get events => frames.stream;
  @override
  Future<void> connect() async {}
  Future<void> close() => frames.close();
}

/// All HTTP is intercepted, including prompt_async. These fixtures cannot
/// connect to production and never call a real model.
class SuggestionApi extends ChatApi {
  SuggestionApi(this.dio) : super(dio) {
    dio.interceptors.add(
      InterceptorsWrapper(
        onRequest: (options, handler) async {
          try {
            if (!options.path.endsWith('/prompt_async')) {
              throw StateError('Unexpected HTTP request: ${options.path}');
            }
            sends.add(options);
            await onPrompt?.call(options);
            handler.resolve(
              Response<dynamic>(requestOptions: options, data: {'ok': true}),
            );
          } catch (error) {
            handler.reject(DioException(requestOptions: options, error: error));
          }
        },
      ),
    );
  }
  final Dio dio;
  final sends = <RequestOptions>[];
  final offsets = <int>[];
  Future<void> Function(RequestOptions)? onPrompt;
  List<ChatMessage> messages = [answer()];
  String owner = 'owner';

  @override
  Future<Session> getSession(String sessionId) async => Session.fromJson({
    'id': sessionId,
    'status': 'idle',
    'user_id': owner,
    'model': 'test/chat',
  });
  @override
  Future<List<ChatMessage>> listMessages(
    String sessionId, {
    int offset = 0,
    int limit = 200,
  }) async {
    offsets.add(offset);
    final all = messages.where((m) => m.sessionId == sessionId).toList();
    return all.sublist(
      math.min(offset, all.length),
      math.min(offset + limit, all.length),
    );
  }

  @override
  Future<List<PermissionRequest>> listPermissions() async => [];
  @override
  Future<List<QuestionRequest>> listQuestions() async => [];
}

class SuggestionFixture {
  SuggestionFixture._(this.api, this.ws, this.container);
  final SuggestionApi api;
  final SuggestionWs ws;
  final ProviderContainer container;
  bool _disposed = false;

  void disposeContainer() {
    if (_disposed) return;
    _disposed = true;
    container.dispose();
  }

  static Future<SuggestionFixture> create({String language = 'zh-CN'}) async {
    SharedPreferences.setMockInitialValues({'bossip:lang': language});
    final prefs = await SharedPreferences.getInstance();
    final bundle = await I18nBundle.load();
    final api = SuggestionApi(
      Dio(BaseOptions(baseUrl: 'http://suggestions.invalid')),
    );
    final ws = SuggestionWs();
    final container = ProviderContainer(
      overrides: [
        prefsProvider.overrideWithValue(prefs),
        i18nProvider.overrideWith(() => I18nController(bundle, prefs)),
        chatApiProvider.overrideWithValue(api),
        apiDioProvider.overrideWithValue(api.dio),
        wsClientProvider.overrideWithValue(ws),
        runningContainerProvider.overrideWith((ref) async => null),
        appConfigProvider.overrideWith(
          (ref) async => const AppConfig(
            defaultModel: 'test/chat',
            models: [
              ModelInfo(
                id: 'test/chat',
                name: 'Chat',
                provider: 'Test',
                variants: ['low', 'high'],
              ),
            ],
          ),
        ),
      ],
    );
    container
        .read(authProvider.notifier)
        .setAuth(
          'fixture-token',
          const AuthUser(id: 'owner', username: 'Fixture'),
        );
    return SuggestionFixture._(api, ws, container);
  }

  Widget app(
    Widget child, {
    Brightness brightness = Brightness.light,
    double scale = 1,
    bool reduceMotion = false,
    TargetPlatform? platform,
  }) => _FixtureLifetime(
    onDispose: disposeContainer,
    child: UncontrolledProviderScope(
      container: container,
      child: MaterialApp(
        theme: ThemeData(
          brightness: brightness,
          platform: platform,
          extensions: [
            BossipTokens.resolve(BossipThemeName.default_, brightness),
          ],
        ),
        builder: (context, child) => MediaQuery(
          data: MediaQuery.of(context).copyWith(
            textScaler: TextScaler.linear(scale),
            disableAnimations: reduceMotion,
          ),
          child: child!,
        ),
        home: Scaffold(body: SafeArea(child: child)),
      ),
    ),
  );

  Future<void> dispose() async {
    disposeContainer();
    await ws.close();
    api.dio.close();
  }
}

class _FixtureLifetime extends StatefulWidget {
  const _FixtureLifetime({required this.child, required this.onDispose});
  final Widget child;
  final VoidCallback onDispose;
  @override
  State<_FixtureLifetime> createState() => _FixtureLifetimeState();
}

class _FixtureLifetimeState extends State<_FixtureLifetime> {
  @override
  void dispose() {
    widget.onDispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => widget.child;
}
