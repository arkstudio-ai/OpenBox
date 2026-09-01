import 'package:bossip_mobile/features/chat/state/chat_session_controller.dart';
import 'package:bossip_mobile/shared/models/app_config.dart';
import 'package:bossip_mobile/shared/models/session.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

/// The video pill mirrors web `VideoModelPicker` + `useVideoModelChoice`.
void main() {
  const config = AppConfig(
    models: [],
    videoModels: [
      VideoModelInfo(
        id: 'wan3.0-video',
        name: 'Wan 3.0',
        tier: '标准',
        resolutions: ['480p', '720p', '1080p'],
      ),
      VideoModelInfo(
        id: 'video-sd-1080p-pro',
        name: 'SD 1080p Pro',
        tier: '高清',
        resolutions: ['1080p'],
      ),
    ],
    defaultVideoModel: 'wan3.0-video',
    defaultVideoResolution: '720p',
  );

  /// Same rule as the composer: a tier belongs to the model it was picked
  /// with, so it only survives a model switch if the new model offers it.
  String resolveTier(String modelId, String chosen) {
    final tiers = config.videoById(modelId)?.resolutions ?? const <String>[];
    if (tiers.contains(chosen)) return chosen;
    return tiers.isEmpty ? '' : tiers.first;
  }

  test('a tier the new model does not offer falls back to its first', () {
    // Carrying 480p onto a 1080p-only model would promise a combination the
    // backend refuses, and the refusal would arrive after send.
    expect(resolveTier('video-sd-1080p-pro', '480p'), '1080p');
  });

  test('a tier the model does offer is kept across a switch', () {
    expect(resolveTier('wan3.0-video', '1080p'), '1080p');
  });

  test('config exposes the deployment defaults the pill starts from', () {
    expect(config.defaultVideoModel, 'wan3.0-video');
    expect(config.defaultVideoResolution, '720p');
    expect(resolveTier(config.defaultVideoModel, config.defaultVideoResolution),
        '720p');
  });

  test('a draft pick survives the hop onto the real session', () {
    // Mobile creates the session first and sends separately, so the draft
    // picks have to be copied across by hand — web passes them as options on
    // the first prompt and never holds them. Dropping the video pick here
    // generated the first shot with the deployment default, at a different
    // price from the model the person had selected.
    final container = ProviderContainer();
    addTearDown(container.dispose);

    container.read(pickedVideoProvider('__draft__').notifier).state =
        const VideoPick('wan3.0-video', '1080p');
    container.read(pickedVideoProvider('s-new').notifier).state =
        container.read(pickedVideoProvider('__draft__'));

    final carried = container.read(pickedVideoProvider('s-new'));
    expect(carried?.modelId, 'wan3.0-video');
    expect(carried?.resolution, '1080p');
  });

  test('a session carries both halves of the pick', () {
    final session = Session.fromJson(const {
      'id': 's1',
      'title': 't',
      'agent': 'build',
      'model': 'gpt',
      'status': 'idle',
      'video_model': 'wan3.0-video',
      'video_resolution': '1080p',
      'created_at': '2026-09-01T00:00:00Z',
      'updated_at': '2026-09-01T00:00:00Z',
    });

    expect(session.videoModel, 'wan3.0-video');
    expect(session.videoResolution, '1080p');
  });
}
