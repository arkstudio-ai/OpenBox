import 'package:bossip_mobile/features/chat/utils/task_status.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test('drops markdown marks and keeps the words', () {
    expect(
      plainSummary(
        '当前项目文件夹（`/workspace/project-01`）下暂无文件。\n\n*（注：上一级有 `snake.html`）*',
      ),
      '当前项目文件夹（/workspace/project-01）下暂无文件。\n\n（注：上一级有 snake.html）',
    );
    expect(
      plainSummary('## 结果\n**已完成**，见 [报告](/app/s/x)\n- 第一项\n* 第二项'),
      '结果\n已完成，见 报告\n• 第一项\n• 第二项',
    );
    expect(
      plainSummary('| 元素 | 颜色 |\n| --- | --- |\n| 蛇头 | 翡翠绿 |'),
      '元素 · 颜色\n\n蛇头 · 翡翠绿',
    );
  });

  test('leaves identifiers with underscores and plain text alone', () {
    expect(plainSummary('snake_case_name stays'), 'snake_case_name stays');
    expect(plainSummary('  plain words  '), 'plain words');
    expect(plainSummary(''), '');
  });
}
