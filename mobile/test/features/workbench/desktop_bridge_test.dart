import 'package:bossip_mobile/features/workbench/widgets/desktop_bridge.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test('the desktop frame refuses picture-in-picture', () {
    final html = desktopBootstrapHtml({
      'ticket': 'ticket-1',
      'desktopId': 'ecd-1',
      'regionId': 'cn-shanghai',
    });
    final frame = RegExp(r'<iframe[^>]*>').firstMatch(html)?.group(0);

    // No floating "desktop inside the desktop": the stream's video may not
    // enter picture-in-picture.
    expect(frame, contains("picture-in-picture 'none'"));
    expect(frame, contains('clipboard-read; clipboard-write; fullscreen'));
  });
}
