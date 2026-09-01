import 'dart:convert';

import 'package:bossip_mobile/features/workbench/utils/terminal_protocol.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test('terminal URL carries the exact current session and project', () {
    final uri = terminalWebSocketUri(
      wsBase: 'ws://127.0.0.1:8080/base/',
      containerId: '容器 / 1',
      ticket: 'ticket+a/b?c=d',
      sessionId: '会话/一',
      projectId: '项目 二',
    );

    expect(uri.pathSegments, ['base', 'ws', 'terminal', '容器 / 1']);
    expect(uri.queryParameters, {
      'ticket': 'ticket+a/b?c=d',
      'session_id': '会话/一',
      'project_id': '项目 二',
    });
  });

  test('switching only the session changes terminal connection identity', () {
    const first = TerminalConnectionIdentity(
      containerId: 'shared-wuying',
      sessionId: 'session-a',
      projectId: 'project-a',
    );
    const second = TerminalConnectionIdentity(
      containerId: 'shared-wuying',
      sessionId: 'session-b',
      projectId: 'project-b',
    );

    expect(first, isNot(second));
    expect(
      first,
      const TerminalConnectionIdentity(
        containerId: 'shared-wuying',
        sessionId: 'session-a',
        projectId: 'project-a',
      ),
    );
  });

  test('split UTF-8 PTY frames preserve Chinese text exactly', () {
    final output = StringBuffer();
    final decoder = TerminalUtf8StreamDecoder(output.write);
    final bytes = utf8.encode('项目/资料/中文文件.txt\n终端正常');

    // Deliberately split every multi-byte scalar across frame boundaries.
    for (final byte in bytes) {
      decoder.add([byte]);
    }
    decoder.close();

    expect(output.toString(), '项目/资料/中文文件.txt\n终端正常');
    expect(output.toString(), isNot(contains('\uFFFD')));
  });
}
