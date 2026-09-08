import 'package:bossip_mobile/features/auth_center/state/auth_center_providers.dart';
import 'package:bossip_mobile/features/chat/utils/content_view.dart';
import 'package:bossip_mobile/shared/models/message.dart';
import 'package:bossip_mobile/shared/models/platform_account.dart';
import 'package:bossip_mobile/shared/models/resource.dart';
import 'package:bossip_mobile/shared/platforms/platform_links.dart';
import 'package:bossip_mobile/shared/router/paths.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test('chat QR files remain visible results despite being transient', () {
    final message = ChatMessage.fromJson({
      'id': 'm',
      'session_id': 's',
      'role': 'assistant',
      'finish': 'stop',
      'parts': [
        {
          'id': 'tool',
          'type': 'tool',
          'tool': 'douyin_publish',
          'status': 'completed',
          'metadata': {'asset_id': 'qr-asset', 'job_id': 'job-a'},
        },
        {
          'id': 'qr',
          'type': 'file',
          'asset_id': 'qr-asset',
          'mime_type': 'image/png',
          'path': '/workspace/douyin/qr.png',
          'transient': true,
          'relation': {
            'source_part_id': 'tool',
            'group_id': 'tool:tool',
            'role': 'result',
            'kind': 'qr_code',
            'label': '抖音投稿二维码',
          },
        },
      ],
    });
    final view = buildAssistantContentView([message], false);
    expect(view.resultGroups.single.artifactKind, 'qr_code');
    expect(view.resultGroups.single.role, 'result');
    expect(view.resultGroups.single.parts.single.assetId, 'qr-asset');
  });
  test('camelCase contracts preserve authorization expiry and job results', () {
    final account = PlatformAccount.fromJson({
      'id': 'a',
      'platform': 'douyin',
      'status': 'bound',
      'nickname': '账号',
      'refreshExpiresAt': '2026-10-01T00:00:00Z',
      'estimatedExpiresAt': '2026-10-31T00:00:00Z',
      'renewalsLeft': 1,
    });
    expect(account.expectedExpiry, DateTime.utc(2026, 10, 31));
    expect(account.statusAt(DateTime.utc(2026, 10, 24)), 'expiring');
    expect(account.statusAt(DateTime.utc(2026, 10, 31)), 'expired');
    final result = PublishResult.fromJson({
      'job': {
        'id': 'j',
        'status': 'pending',
        'expiresAt': '2026-10-01T00:00:00Z',
      },
      'schema': 'snssdk1128://openplatform/share?share_type=h5',
    });
    expect(result.job.linkValidAt(DateTime.utc(2026, 9, 30)), isTrue);
    expect(result.job.linkValidAt(DateTime.utc(2026, 10, 1)), isFalse);
    expect(PublishJob.fromJson({}).pending, isFalse);
    expect(PlatformInfo.fromJson({}).configured, isFalse);
  });

  test('video eligibility and hashtags match the server contract', () {
    Resource video(int size, String mime, {String status = 'ready'}) =>
        Resource.fromJson({'size': size, 'mime': mime, 'status': status});
    expect(isPublishableVideo(video(128 * 1024 * 1024, 'video/mp4')), isTrue);
    expect(
      isPublishableVideo(video(128 * 1024 * 1024 + 1, 'video/mp4')),
      isFalse,
    );
    expect(
      isPublishableVideo(video(1, 'video/quicktime; charset=binary')),
      isTrue,
    );
    expect(
      isPublishableVideo(video(1, 'video/mp4', status: 'uploading')),
      isFalse,
    );
    expect(isPublishableVideo(video(0, 'video/mp4')), isFalse);
    expect(isPublishableVideo(video(1, 'image/png')), isFalse);
    expect(parsePublishHashtags('#装修， 好物,#装修  #生活'), ['装修', '好物', '生活']);
    expect(
      parsePublishHashtags(List.generate(12, (n) => '#话题$n').join(' ')),
      hasLength(10),
    );
  });

  test(
    'phone authorization keeps state and callback while requesting app launch',
    () {
      final source = Uri.https('open.douyin.com', '/platform/oauth/connect/', {
        'state': 'nonce',
        'redirect_uri':
            'https://ai.bossipai.com.cn/api/platform-accounts/douyin/callback',
        'scope': 'user_info',
        'client_key': 'public-key',
      });
      final target = douyinAuthorizationUri(source.toString())!;
      expect(target.queryParameters['state'], 'nonce');
      expect(
        target.queryParameters['redirect_uri'],
        source.queryParameters['redirect_uri'],
      );
      expect(target.queryParameters['is_call_app'], '1');
    },
  );

  test('platform buttons reject arbitrary URLs and encode job route input', () {
    for (final bad in [
      'javascript:alert(1)',
      'file:///etc/passwd',
      'http://open.douyin.com/',
      'intent://openplatform/share',
      'snssdk1128://other/path',
      'https://open.douyin.com.evil.test/',
      'https://user@open.douyin.com/',
    ]) {
      expect(douyinAuthorizationUri(bad), isNull);
      expect(douyinPublishUri(bad), isNull);
    }
    expect(
      douyinPublishUri(
        'snssdk1128://webview?url=https%3A%2F%2Fopen.douyin.com',
      ),
      isNotNull,
    );
    expect(
      Uri.parse(Paths.authCenter(jobId: 'one&other=two')).queryParameters,
      {'job': 'one&other=two'},
    );
  });
}
