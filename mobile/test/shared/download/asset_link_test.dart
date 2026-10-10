import 'package:bossip_mobile/shared/download/asset_download.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test('asset download links resolve to their asset id', () {
    const id = 'asset_01M4CS5QBVAR93CR095J3KA7A0';
    for (final url in [
      '/api/assets/$id/download?token=eyJhbGciOi.expired.sig',
      'https://ai.bossipai.com.cn/api/assets/$id/download?token=x',
      '/api/assets/$id/url',
      ' /api/assets/$id/download ',
    ]) {
      expect(assetIdFromLink(url), id, reason: url);
    }
  });

  test('other links are left alone', () {
    for (final url in [
      '/app/s/session_1',
      'https://example.com/api/assets/asset_1/download/../../x',
      '/api/assets/asset_1/text',
      '/api/assets?project=all',
      'javascript:alert(1)',
      '/api/assets/not-an-asset/download',
    ]) {
      expect(assetIdFromLink(url), isNull, reason: url);
    }
  });
}
