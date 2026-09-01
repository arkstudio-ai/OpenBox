import 'package:bossip_mobile/features/workbench/utils/project_path.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  const root =
      '/workspace/openbox/users/u-abcd/projects/p-1234-browser-acceptance';

  test('display and clipboard paths are relative to the named project', () {
    const file = '$root/资料/中文 文件.txt';

    expect(projectRelativePath(root, file), '资料/中文 文件.txt');
    expect(
      projectDisplayPath(root: root, path: file, projectName: '浏览器验收项目'),
      '浏览器验收项目/资料/中文 文件.txt',
    );
    expect(
      projectDisplayPath(root: root, path: root, projectName: '浏览器验收项目'),
      '浏览器验收项目',
    );
  });

  test('physical namespace never becomes a relative display path', () {
    const outside = '/workspace/openbox/users/u-other/projects/p-secret/a.txt';

    expect(projectRelativePath(root, outside), isNull);
    expect(
      resolveProjectEntryPath(
        root: root,
        cwd: root,
        entryPath: outside,
        entryName: 'a.txt',
      ),
      isNull,
    );
    expect(isWithinProjectRoot(root, '$root-other/a.txt'), isFalse);
  });

  test('Unicode relative entries resolve without encoding or corruption', () {
    expect(
      resolveProjectEntryPath(
        root: root,
        cwd: '$root/素材',
        entryPath: '图片/封面 图.png',
        entryName: 'ignored.png',
      ),
      '$root/素材/图片/封面 图.png',
    );
    expect(projectParentPath(root, '$root/素材/图片'), '$root/素材');
    expect(projectParentPath(root, root), root);
  });

  test('dot segments cannot escape the project root', () {
    expect(
      resolveProjectEntryPath(
        root: root,
        cwd: root,
        entryPath: '../../u-other/secret.txt',
        entryName: 'secret.txt',
      ),
      isNull,
    );
  });
}
