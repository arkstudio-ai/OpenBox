import 'package:bossip_mobile/features/chat/utils/tool_map.dart';
import 'package:bossip_mobile/shared/models/message_part.dart';
import 'package:bossip_mobile/shared/utils/project_path.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  const root = '/workspace/openbox/users/u-a/projects/p-b-demo';
  const upload =
      '/workspace/openbox/users/u-a/.openbox/uploads/p-b-demo/a-123/image 你好.jpg';

  test('namespace paths display relative without corrupting Unicode', () {
    expect(projectScopedDisplayPath('$root/资料/你好😀.txt'), '资料/你好😀.txt');
    expect(projectScopedDisplayPath(root), '.');
    expect(projectScopedDisplayPath('users/资料/你好😀.txt'), 'users/资料/你好😀.txt');
    expect(
      projectScopedDisplayText('$root/资料/你好.ts:3:命中\n$root/README.md'),
      '资料/你好.ts:3:命中\nREADME.md',
    );
    expect(
      projectScopedDisplayPath(upload),
      '.openbox/uploads/a-123/image 你好.jpg',
    );
    expect(
      projectScopedDisplayText('asset_id=x; path=$upload; image/png'),
      'asset_id=x; path=.openbox/uploads/a-123/image 你好.jpg; image/png',
    );
    expect(
      projectScopedToolText('*** Update File: $root/资料/你好😀.txt\n+正文'),
      '*** Update File: 资料/你好😀.txt\n+正文',
    );
    expect(
      projectScopedToolText('Updated $root/资料/你好😀.txt'),
      'Updated 资料/你好😀.txt',
    );
  });

  test('file tool summaries never expose the physical namespace', () {
    const part = ToolPart(
      id: 'tool-1',
      tool: 'read',
      status: ToolStatus.completed,
      input: {'file_path': '$root/资料/你好😀.txt'},
      title: 'Error reading $root/资料/你好😀.txt',
    );

    expect(toolTarget(part), '资料/你好😀.txt');
    expect(toolDetail(part), '资料/你好😀.txt');
  });
}
