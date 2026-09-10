import 'package:bossip_mobile/features/chat/utils/content_view.dart';
import 'package:flutter_test/flutter_test.dart';

import '../../fixtures/direct_video_transcript.dart';

List<dynamic> parts(VideoTranscript data, int index) =>
    data[index]['parts'] as List<dynamic>;
Map<String, dynamic> part(VideoTranscript data, int message, int index) =>
    parts(data, message)[index] as Map<String, dynamic>;
Map<String, dynamic> relation(VideoTranscript data, int message) =>
    part(data, message, 1)['relation'] as Map<String, dynamic>;
Map<String, dynamic> generator(VideoTranscript data) => part(data, 0, 0);
Map<String, dynamic> sharing(VideoTranscript data) => part(data, 1, 0);
Map<String, dynamic> shared(VideoTranscript data) => part(data, 1, 1);

void main() {
  List<String> kinds(
    VideoTranscript data, {
    bool streaming = false,
    bool awaiting = false,
  }) => buildAssistantContentView(
    directVideoMessages(data),
    streaming,
    awaitingInput: awaiting,
  ).resultGroups.map((g) => g.artifactKind).toList();

  test(
    'recognizes the same production-shaped fixture as Web without rewriting stored relations',
    () {
      final messages = directVideoMessages();
      final view = buildAssistantContentView(messages, false);
      expect(view.resultGroups.map((g) => [g.artifactKind, g.role]), [
        ['video_segment', 'intermediate'],
        ['video_final', 'final'],
      ]);
      expect(view.resultGroups.last.parts.single.relation?.kind, 'shared_file');
      expect(view.resultGroups.last.parts.single.assetId, 'asset-shared');
    },
  );

  test('supports shared asset identity and legacy transcripts', () {
    final data = directVideoJson();
    shared(data)['asset_id'] = 'asset-generated';
    generator(data)['output'] = 'status=completed';
    expect(kinds(data), contains('video_final'));
    for (final message in data) {
      message.remove('finish');
    }
    part(data, 2, 0).remove('channel');
    expect(kinds(data), contains('video_final'));
  });

  final negatives = <String, void Function(VideoTranscript)>{
    'unrelated same basename': (d) =>
        shared(d)['path'] = '/another/segment-video_fixture.mp4',
    'missing provenance': (d) => generator(d)['output'] = 'status=completed',
    'failed generation': (d) => generator(d)['output'] = 'status=failed',
    'running generation': (d) => generator(d)['status'] = 'running',
    'failed share': (d) => sharing(d)['status'] = 'error',
    'non-attached share': (d) => sharing(d)['metadata'] = {'attached': false},
    'explicitly disabled attachment': (d) =>
        sharing(d)['input'] = {'attach': false},
    'explicit preview': (d) => relation(d, 1)['role'] = 'intermediate',
    'storyboard preview': (d) =>
        relation(d, 0)['metadata'] = {'production_id': 'production-1'},
    'non-video': (d) => shared(d)['mime_type'] = 'image/png',
    'missing asset': (d) => shared(d).remove('asset_id'),
    'no attachment': (d) => parts(d, 1).removeLast(),
    'no final reply': (d) => d.removeLast(),
    'aborted': (d) => d[2]['finish'] = 'aborted',
    'waiting': (d) => d[2]['finish'] = 'waiting_input',
    'message error': (d) => d[2]['error'] = {'message': 'Disconnected'},
    'share before generation': (d) {
      final first = d[0];
      d[0] = d[1];
      d[1] = first;
    },
    'ambiguous materials': (d) => parts(d, 0).add({
      ...part(d, 0, 1),
      'id': 'other',
      'relation': {'kind': 'video_segment', 'group_id': 'other-material'},
    }),
  };
  for (final entry in negatives.entries) {
    test('does not promote ${entry.key}', () {
      final data = directVideoJson();
      entry.value(data);
      expect(kinds(data), isNot(contains('video_final')));
    });
  }
  test(
    'active and suspended turns do not turn ordinary attachments into final videos',
    () {
      expect(
        kinds(directVideoJson(), streaming: true),
        isNot(contains('video_final')),
      );
      expect(
        kinds(directVideoJson(), awaiting: true),
        isNot(contains('video_final')),
      );
    },
  );
}
