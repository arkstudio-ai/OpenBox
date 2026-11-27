import 'package:bossip_mobile/features/chat/utils/turn_view.dart';
import 'package:bossip_mobile/features/chat/widgets/traces/tool_chain_trace.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:bossip_mobile/shared/models/message.dart';
import 'package:bossip_mobile/shared/models/message_part.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

void main() {
  testWidgets('one expansion shows gallery progress above the request', (
    tester,
  ) async {
    SharedPreferences.setMockInitialValues({'bossip:lang': 'zh-CN'});
    final prefs = await SharedPreferences.getInstance();
    final bundle = (await tester.runAsync(I18nBundle.load))!;
    const progress =
        '准备参考素材 · 已完成\n'
        'ComfyUI · 第 1 段 · R2V 7s · 已完成\n'
        'ComfyUI · 第 2 段 · R2V 7s · 已完成\n'
        'ComfyUI · 第 3 段 · I2V 7s · 已完成\n'
        'ComfyUI · 第 4 段 · R2V 7s · 已完成\n'
        '第 5 段宣传原画 · 已完成\n'
        'ComfyUI · 第 5 段 · FL2V 7s · 已完成\n'
        '阿里云 IMS 剪辑 · 等待结果';
    final turn = buildChatRows([
      const ChatMessage(
        id: 'assistant',
        sessionId: 'production',
        role: 'assistant',
        parts: [
          ToolPart(
            id: 'gallery',
            tool: 'light_gallery',
            status: ToolStatus.running,
            input: {'shots': []},
            output: progress,
          ),
        ],
      ),
    ]).whereType<AssistantTurnData>().single;
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
          home: Scaffold(
            body: SingleChildScrollView(
              child: ToolChainTrace(turn: turn, active: true),
            ),
          ),
        ),
      ),
    );
    final progressText = find.text(progress);
    expect(progressText.hitTestable(), findsNothing);
    await tester.tap(find.byIcon(Icons.expand_more));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 300));
    expect(progressText.hitTestable(), findsOneWidget);
    expect(tester.widget<Text>(progressText).maxLines, isNull);
    expect(
      tester.getTopLeft(progressText).dy,
      lessThan(tester.getTopLeft(find.textContaining('"shots"')).dy),
    );
  });
}
