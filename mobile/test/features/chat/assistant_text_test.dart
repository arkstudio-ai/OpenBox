import 'package:bossip_mobile/features/chat/utils/assistant_text.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test('drops labelled ids and raw field values the platform returned', () {
    expect(
      hideInternalIds(
        '项目「贪吃蛇」中的任务「收尾自检」（ID: `01M48Y8NP3008Z51ZE6QEH50VZ`）已执行完成。',
      ),
      '项目「贪吃蛇」中的任务「收尾自检」已执行完成。',
    );
    expect(hideInternalIds('- 执行结果：成功（outcome: succeeded）'), '- 执行结果：成功');
    expect(
      hideInternalIds('每日简报已为您成功关闭（`enabled: false`），后续不会再发送。'),
      '每日简报已为您成功关闭，后续不会再发送。',
    );
    expect(
      hideInternalIds('会话 `session_7YBVQF0QR0ZKR59BAEH1DDDQBH` 已关注'),
      '会话 已关注',
    );
    expect(hideInternalIds('已提交，任务编号：01M48Y8NP3008Z51ZE6QEH50VZ'), '已提交');
  });

  test('leaves ordinary parentheses, code and words alone', () {
    for (final text in [
      '主色 `#10b981`（荧光翡翠绿）',
      '（注：在父级工作区 `/workspace` 目录下存在文件 `snake.html`）',
      '宽度 (width: 100px) 与比例 16:9',
      'status 这个词本身没关系',
      '用 `npm test` 跑一下',
    ]) {
      expect(hideInternalIds(text), text);
    }
  });
}
