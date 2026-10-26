import 'package:bossip_mobile/features/chat/widgets/assistant_persona_turn.dart';
import 'package:bossip_mobile/shared/api/api_error.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/utils/error_text.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

const _receipt =
    '个人助理本轮回复超时，已停止；已提交的任务指令不会因此撤销。\n'
    '「水果店视频」：已接收，正在等待原任务中的确认，尚未开始执行这条指令。\n'
    '请先查看原任务状态，避免重复提交。';

void main() {
  for (final code in ['SANDBOX_NOT_READY', 'SANDBOX_SUBSCRIPTION_REQUIRED']) {
    testWidgets(
      '$code shows the retained request cause without resend advice',
      (tester) async {
        SharedPreferences.setMockInitialValues({'bossip:lang': 'zh-CN'});
        final prefs = await SharedPreferences.getInstance();
        final bundle = (await tester.runAsync(I18nBundle.load))!;
        final i18n = I18nState(language: 'zh-CN', bundle: bundle);
        const message = '原请求和参考附件已保留；恢复后继续原任务，无需重新上传。';
        await tester.pumpWidget(
          ProviderScope(
            overrides: [
              i18nProvider.overrideWith(() => I18nController(bundle, prefs)),
            ],
            child: MaterialApp(
              theme: ThemeData(
                extensions: [
                  BossipTokens.resolve(
                    BossipThemeName.default_,
                    Brightness.light,
                  ),
                ],
              ),
              home: Scaffold(
                body: AssistantErrorNotice(
                  error: {'code': code, 'message': message},
                ),
              ),
            ),
          ),
        );
        await tester.pumpAndSettle();
        expect(find.text(message), findsOneWidget);
        expect(
          find.text(i18n.t('chat:assistant.continueAfterError')),
          findsNothing,
        );
        expect(find.text(i18n.t('chat:meta.errorDetails')), findsNothing);
      },
    );
  }
  testWidgets('budget receipt stays visible without generic resend advice', (
    tester,
  ) async {
    SharedPreferences.setMockInitialValues({'bossip:lang': 'zh-CN'});
    final prefs = await SharedPreferences.getInstance();
    final bundle = (await tester.runAsync(I18nBundle.load))!;
    final i18n = I18nState(language: 'zh-CN', bundle: bundle);
    const error = {'code': 'ASSISTANT_TURN_BUDGET', 'message': _receipt};
    expect(runFailureText(i18n, error), _receipt);
    expect(
      errorText(
        i18n,
        ApiError(status: 500, code: error['code']!, message: _receipt),
      ),
      _receipt,
    );
    expect(
      runFailureText(i18n, {'code': 'ASSISTANT_TURN_BUDGET', 'message': ''}),
      contains('请先查看相关任务状态'),
    );
    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          i18nProvider.overrideWith(() => I18nController(bundle, prefs)),
        ],
        child: MaterialApp(
          theme: ThemeData(
            extensions: [
              BossipTokens.resolve(BossipThemeName.default_, Brightness.light),
            ],
          ),
          home: const Scaffold(body: AssistantErrorNotice(error: error)),
        ),
      ),
    );
    await tester.pumpAndSettle();
    expect(find.text('这轮回复未完成'), findsOneWidget);
    expect(find.text(_receipt), findsOneWidget);
    expect(
      find.text(i18n.t('chat:assistant.continueAfterError')),
      findsNothing,
    );
    expect(find.text(i18n.t('chat:meta.errorDetails')), findsNothing);
    expect(find.text(i18n.t('chat:meta.regenerate')), findsNothing);
    expect(find.text(i18n.t('chat:meta.dismissTurn')), findsNothing);
  });
}
