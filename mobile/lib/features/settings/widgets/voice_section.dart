import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:video_player/video_player.dart';

import '../../../shared/api/assistant_profile.dart';
import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/json.dart';
import '../../../shared/widgets/toast.dart';
import '../api/settings_api.dart';

/// 语音通话 (web `VoicePage`): the voice the personal assistant speaks with
/// in a call, grouped as 中文女声 / 中文男声 / 英文, each with a short
/// preview. A pick is stored on the server; the next call uses it.
class VoiceSection extends ConsumerStatefulWidget {
  const VoiceSection({super.key, this.playerFactory});

  /// Tests pass a fake; the app plays the sample with the platform player
  /// (`video_player`, as the chat's audio preview does).
  final VoicePreviewPlayer Function(Uri uri)? playerFactory;

  @override
  ConsumerState<VoiceSection> createState() => _VoiceSectionState();
}

class _VoiceSectionState extends ConsumerState<VoiceSection> {
  static const _groups = ['zhFemale', 'zhMale', 'en'];
  VoicePreviewPlayer? _player;
  String? _playing;

  @override
  void dispose() {
    _player?.dispose();
    super.dispose();
  }

  static String _group(Map<String, dynamic> voice) {
    if (asString(voice['lang']) == 'en') return 'en';
    return asString(voice['gender']) == 'male' ? 'zhMale' : 'zhFemale';
  }

  Future<void> _preview(String id) async {
    final old = _player;
    _player = null;
    await old?.dispose();
    if (_playing == id) {
      setState(() => _playing = null);
      return;
    }
    final uri = ref.read(settingsApiProvider).voiceSampleUri(id);
    final player = (widget.playerFactory ?? VideoPreviewPlayer.new)(uri);
    _player = player;
    setState(() => _playing = id);
    try {
      await player.play(
        onDone: () {
          if (mounted && _playing == id) setState(() => _playing = null);
        },
      );
    } catch (_) {
      if (!mounted) return;
      setState(() => _playing = null);
      ref
          .read(toastProvider.notifier)
          .error(ref.read(i18nProvider).t('settings:voice.previewFailed'));
    }
  }

  Future<void> _pick(Map<String, dynamic> voice, String? selected) async {
    final id = asString(voice['id']) ?? '';
    if (id.isEmpty || id == selected) return;
    final i18n = ref.read(i18nProvider);
    try {
      await ref.read(settingsApiProvider).setAssistantVoice(id);
      ref.invalidate(assistantVoicesProvider);
      if (!mounted) return;
      ref
          .read(toastProvider.notifier)
          .success(
            i18n.t('settings:voice.saved', vars: {'name': _name(voice, i18n)}),
          );
    } catch (_) {
      if (!mounted) return;
      ref
          .read(toastProvider.notifier)
          .error(i18n.t('settings:voice.saveFailed'));
    }
  }

  static bool _english(I18nState i18n) => i18n.language.startsWith('en');

  static String _name(Map<String, dynamic> voice, I18nState i18n) =>
      (_english(i18n) ? asString(voice['id']) : asString(voice['name'])) ?? '';

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final status = ref.watch(assistantVoicesProvider);
    if (status.hasError) {
      return ListView(
        padding: const EdgeInsets.all(16),
        children: [
          Text(
            i18n.t('settings:voice.loadFailed'),
            style: TextStyle(fontSize: FontSizes.sm, color: t.n600),
          ),
          const SizedBox(height: 18),
          const CallHabits(),
        ],
      );
    }
    final data = status.valueOrNull ?? const {};
    final voices = (data['voices'] as List? ?? const [])
        .whereType<Map<String, dynamic>>()
        .toList();
    final selected = asString(data['selected']);
    final fallback = asString(data['default']);
    return ListView(
      padding: const EdgeInsets.all(16),
      children: [
        for (final group in _groups)
          if (voices.any((voice) => _group(voice) == group)) ...[
            Padding(
              padding: const EdgeInsets.only(bottom: 8, top: 4),
              child: Text(
                i18n.t('settings:voice.group.$group'),
                style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
              ),
            ),
            for (final voice in voices.where((v) => _group(v) == group)) ...[
              _VoiceCard(
                key: ValueKey('voice-${asString(voice['id'])}'),
                title: _name(voice, i18n),
                subtitle: _english(i18n)
                    ? (asString(voice['name']) ?? '')
                    : (asString(voice['id']) ?? ''),
                description:
                    (_english(i18n)
                        ? asString(voice['description_en'])
                        : asString(voice['description'])) ??
                    '',
                active: asString(voice['id']) == selected,
                isDefault: asString(voice['id']) == fallback,
                playing: _playing == asString(voice['id']),
                onTap: () => _pick(voice, selected),
                onPreview: () => _preview(asString(voice['id']) ?? ''),
              ),
              const SizedBox(height: 10),
            ],
            const SizedBox(height: 6),
          ],
        Text(
          i18n.t('settings:voice.note'),
          style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
        ),
        const SizedBox(height: 18),
        const CallHabits(),
      ],
    );
  }
}

/// How calls go (web `CallHabits`): whether the greeting brings up the last
/// call, whether finished work nobody asked about on the call is told, and
/// how much an answer says. Saved at once; a call in progress follows it from
/// its next answer.
class CallHabits extends ConsumerStatefulWidget {
  const CallHabits({super.key});

  @override
  ConsumerState<CallHabits> createState() => _CallHabitsState();
}

class _CallHabitsState extends ConsumerState<CallHabits> {
  bool _saving = false;

  Future<void> _save(Map<String, Object> patch) async {
    final i18n = ref.read(i18nProvider);
    final toast = ref.read(toastProvider.notifier);
    setState(() => _saving = true);
    try {
      await ref.read(assistantProfileProvider.notifier).save(patch);
      toast.success(i18n.t('settings:voice.call.saved'));
    } catch (_) {
      toast.error(i18n.t('settings:voice.call.saveFailed'));
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final profile =
        ref.watch(assistantProfileProvider).valueOrNull ??
        const AssistantProfile();
    Widget habit(String key, String name, bool on) => SwitchListTile.adaptive(
      key: ValueKey('call-$name'),
      contentPadding: EdgeInsets.zero,
      value: on,
      onChanged: _saving ? null : (value) => _save({name: value}),
      title: Text(
        i18n.t('settings:voice.call.$key'),
        style: TextStyle(fontSize: FontSizes.base, color: t.ink),
      ),
      subtitle: Text(
        i18n.t('settings:voice.call.${key}Hint'),
        style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
      ),
    );
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          i18n.t('settings:voice.call.title'),
          style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
        ),
        habit('recap', 'call_recap', profile.callRecap),
        habit('reports', 'call_reports', profile.callReports),
        const SizedBox(height: 6),
        Text(
          i18n.t('settings:voice.call.detail'),
          style: TextStyle(fontSize: FontSizes.sm, color: t.n700),
        ),
        const SizedBox(height: 8),
        Wrap(
          spacing: 8,
          children: [
            for (final detail in AssistantProfile.callDetails)
              Semantics(
                button: true,
                selected: detail == profile.callDetail,
                child: InkWell(
                  key: ValueKey('call-detail-$detail'),
                  onTap: _saving || detail == profile.callDetail
                      ? null
                      : () => _save({'call_detail': detail}),
                  borderRadius: BorderRadius.circular(Radii.md),
                  child: Container(
                    constraints: const BoxConstraints(
                      minWidth: 72,
                      minHeight: 38,
                    ),
                    alignment: Alignment.center,
                    padding: const EdgeInsets.symmetric(horizontal: 14),
                    decoration: BoxDecoration(
                      color: t.card,
                      borderRadius: BorderRadius.circular(Radii.md),
                      border: Border.all(
                        color: detail == profile.callDetail ? t.ink : t.hair,
                      ),
                    ),
                    child: Text(
                      i18n.t('settings:voice.call.detailOption.$detail'),
                      style: TextStyle(fontSize: FontSizes.sm, color: t.ink),
                    ),
                  ),
                ),
              ),
          ],
        ),
      ],
    );
  }
}

class _VoiceCard extends ConsumerWidget {
  const _VoiceCard({
    super.key,
    required this.title,
    required this.subtitle,
    required this.description,
    required this.active,
    required this.isDefault,
    required this.playing,
    required this.onTap,
    required this.onPreview,
  });

  final String title;
  final String subtitle;
  final String description;
  final bool active;
  final bool isDefault;
  final bool playing;
  final VoidCallback onTap;
  final VoidCallback onPreview;

  Widget _tag(BuildContext context, String text, {bool strong = false}) {
    final t = context.tokens;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
      decoration: BoxDecoration(
        color: strong ? null : t.n200,
        border: strong ? Border.all(color: t.ink) : null,
        borderRadius: BorderRadius.circular(999),
      ),
      child: Text(
        text,
        style: TextStyle(
          fontSize: FontSizes.xs,
          color: strong ? t.ink : t.n700,
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return Material(
      color: t.card,
      borderRadius: BorderRadius.circular(Radii.xl),
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(Radii.xl),
        child: Container(
          width: double.infinity,
          padding: const EdgeInsets.fromLTRB(16, 12, 8, 12),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(Radii.xl),
            border: Border.all(color: active ? t.ink : t.hair),
          ),
          child: Row(
            children: [
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Wrap(
                      spacing: 8,
                      runSpacing: 4,
                      crossAxisAlignment: WrapCrossAlignment.center,
                      children: [
                        Text(
                          title,
                          style: TextStyle(
                            fontSize: FontSizes.base,
                            color: t.ink,
                          ),
                        ),
                        Text(
                          subtitle,
                          style: TextStyle(
                            fontSize: FontSizes.xs,
                            color: t.n600,
                          ),
                        ),
                        if (isDefault)
                          _tag(context, i18n.t('settings:voice.defaultTag')),
                        if (active)
                          _tag(
                            context,
                            i18n.t('settings:voice.current'),
                            strong: true,
                          ),
                      ],
                    ),
                    const SizedBox(height: 4),
                    Text(
                      description,
                      style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
                    ),
                  ],
                ),
              ),
              TextButton(
                key: ValueKey('voice-preview-$subtitle'),
                onPressed: onPreview,
                child: Text(
                  playing
                      ? i18n.t('settings:voice.stop')
                      : i18n.t('settings:voice.listen'),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

/// One preview playback.
abstract class VoicePreviewPlayer {
  /// Starts playing; [onDone] runs when it reaches the end.
  Future<void> play({required VoidCallback onDone});

  Future<void> dispose();
}

class VideoPreviewPlayer implements VoicePreviewPlayer {
  VideoPreviewPlayer(Uri uri)
    : _controller = VideoPlayerController.networkUrl(uri);

  final VideoPlayerController _controller;
  VoidCallback? _listener;

  @override
  Future<void> play({required VoidCallback onDone}) async {
    await _controller.initialize();
    _listener = () {
      final value = _controller.value;
      if (value.isInitialized &&
          !value.isPlaying &&
          value.duration > Duration.zero &&
          value.position >= value.duration) {
        onDone();
      }
    };
    _controller.addListener(_listener!);
    await _controller.play();
  }

  @override
  Future<void> dispose() async {
    if (_listener != null) _controller.removeListener(_listener!);
    await _controller.dispose();
  }
}
