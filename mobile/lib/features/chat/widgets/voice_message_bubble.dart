import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:video_player/video_player.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/message.dart';
import '../../../shared/models/message_part.dart';
import '../api/assets_api.dart';

Map<String, dynamic>? recordedVoice(ChatMessage message) {
  for (final part in message.parts.whereType<TextPart>()) {
    final value = part.originRef['voice_message'];
    if (value is Map<String, dynamic> &&
        value['asset_id'] is String &&
        value['duration_ms'] is num) {
      return value;
    }
  }
  return null;
}

class VoiceMessageBubble extends ConsumerStatefulWidget {
  const VoiceMessageBubble({
    super.key,
    required this.assetId,
    required this.durationMs,
    required this.transcript,
  });
  final String assetId;
  final int durationMs;
  final String transcript;

  @override
  ConsumerState<VoiceMessageBubble> createState() => _VoiceMessageBubbleState();
}

class _VoiceMessageBubbleState extends ConsumerState<VoiceMessageBubble>
    with WidgetsBindingObserver {
  VideoPlayerController? _player;
  bool _loading = false, _expanded = false, _failed = false;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state != AppLifecycleState.resumed) _player?.pause();
  }

  void _tick() {
    if (mounted) setState(() {});
  }

  Future<void> _toggle() async {
    if (_loading) return;
    setState(() {
      _loading = true;
      _failed = false;
    });
    try {
      if (_player == null) {
        final asset = await ref.read(assetUrlProvider(widget.assetId).future);
        if (!mounted) return;
        final player = VideoPlayerController.networkUrl(Uri.parse(asset.url));
        _player = player;
        player.addListener(_tick);
        await player.initialize();
      }
      if (!mounted) return;
      final player = _player!;
      if (player.value.isPlaying) {
        await player.pause();
      } else {
        if (player.value.position >= player.value.duration) {
          await player.seekTo(Duration.zero);
        }
        await player.play();
      }
    } catch (_) {
      final player = _player;
      _player = null;
      player?.removeListener(_tick);
      await player?.dispose();
      if (mounted) {
        ref.invalidate(assetUrlProvider(widget.assetId));
        setState(() => _failed = true);
      }
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _player?.removeListener(_tick);
    _player?.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final playing = _player?.value.isPlaying ?? false;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.end,
      mainAxisSize: MainAxisSize.min,
      children: [
        Material(
          color: t.n200.withValues(alpha: .6),
          borderRadius: BorderRadius.circular(Radii.xl),
          child: InkWell(
            onTap: _toggle,
            borderRadius: BorderRadius.circular(Radii.xl),
            child: Padding(
              padding: const EdgeInsets.symmetric(horizontal: 18, vertical: 12),
              child: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Text(
                    '${(widget.durationMs / 1000).ceil()}″',
                    style: TextStyle(color: t.ink, fontSize: 17),
                  ),
                  const SizedBox(width: 28),
                  if (_loading)
                    const SizedBox(
                      width: 20,
                      height: 20,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    )
                  else
                    Icon(
                      playing
                          ? Icons.pause
                          : _failed
                          ? Icons.refresh
                          : Icons.volume_up_outlined,
                      color: t.ink,
                    ),
                ],
              ),
            ),
          ),
        ),
        TextButton(
          onPressed: () => setState(() => _expanded = !_expanded),
          child: Text(
            i18n.t(
              _failed
                  ? 'chat:voiceMessage.playFailed'
                  : _expanded
                  ? 'chat:voiceMessage.hideText'
                  : 'chat:voiceMessage.showText',
            ),
          ),
        ),
        if (_expanded)
          Padding(
            padding: const EdgeInsets.only(bottom: 8),
            child: Text(
              widget.transcript,
              style: TextStyle(color: t.n700, height: 1.5),
            ),
          ),
      ],
    );
  }
}
