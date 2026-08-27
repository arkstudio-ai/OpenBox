import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:video_player/video_player.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/models/message_part.dart';
import '../api/assets_api.dart';

/// Inline audio player for a produced sound file (web `AudioPreview`, which
/// is a bare `<audio controls>`). `video_player` is the platform's own
/// AVPlayer/ExoPlayer and plays audio-only URLs, so there is no second
/// playback dependency to keep in step with the video one.
class AudioPreview extends ConsumerWidget {
  const AudioPreview({super.key, required this.part});

  final FilePart part;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final assetId = part.assetId;
    if (assetId == null) return const SizedBox.shrink();
    final asset = ref.watch(assetUrlProvider(assetId));
    return asset.maybeWhen(
      data: (info) => info.url.isEmpty
          ? const SizedBox.shrink()
          : _AudioBar(url: info.url, name: part.path.split('/').last),
      orElse: () => const SizedBox.shrink(),
    );
  }
}

class _AudioBar extends StatefulWidget {
  const _AudioBar({required this.url, required this.name});

  final String url;
  final String name;

  @override
  State<_AudioBar> createState() => _AudioBarState();
}

class _AudioBarState extends State<_AudioBar> {
  late final VideoPlayerController _controller;
  bool _ready = false;
  bool _failed = false;

  @override
  void initState() {
    super.initState();
    _controller = VideoPlayerController.networkUrl(Uri.parse(widget.url))
      ..addListener(_onTick)
      ..initialize().then((_) {
        if (mounted) setState(() => _ready = true);
      }).catchError((Object _) {
        if (mounted) setState(() => _failed = true);
      });
  }

  void _onTick() {
    if (mounted) setState(() {});
  }

  @override
  void dispose() {
    _controller.removeListener(_onTick);
    _controller.dispose();
    super.dispose();
  }

  String _clock(Duration d) {
    final minutes = d.inMinutes.remainder(60).toString().padLeft(2, '0');
    final seconds = d.inSeconds.remainder(60).toString().padLeft(2, '0');
    return '$minutes:$seconds';
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    if (_failed) return const SizedBox.shrink();
    final value = _controller.value;
    final playing = _ready && value.isPlaying;
    return Container(
      margin: const EdgeInsets.only(top: 6),
      padding: const EdgeInsets.fromLTRB(6, 6, 12, 6),
      decoration: BoxDecoration(
        border: Border.all(color: t.hair),
        borderRadius: BorderRadius.circular(Radii.full),
        color: t.card,
      ),
      child: Row(
        children: [
          InkWell(
            customBorder: const CircleBorder(),
            onTap: !_ready
                ? null
                : () => setState(() {
                      playing ? _controller.pause() : _controller.play();
                    }),
            child: Container(
              width: 30,
              height: 30,
              decoration: BoxDecoration(color: t.n200, shape: BoxShape.circle),
              child: _ready
                  ? Icon(
                      playing ? Icons.pause : Icons.play_arrow,
                      size: 18,
                      color: t.ink,
                    )
                  : Padding(
                      padding: const EdgeInsets.all(8),
                      child: CircularProgressIndicator(
                        strokeWidth: 1.6,
                        color: t.n600,
                      ),
                    ),
            ),
          ),
          const SizedBox(width: 10),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(
                  widget.name,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(fontSize: FontSizes.xs, color: t.ink),
                ),
                const SizedBox(height: 4),
                if (_ready)
                  VideoProgressIndicator(
                    _controller,
                    allowScrubbing: true,
                    padding: EdgeInsets.zero,
                    colors: VideoProgressColors(
                      playedColor: t.accent,
                      bufferedColor: t.n300,
                      backgroundColor: t.n200,
                    ),
                  ),
              ],
            ),
          ),
          if (_ready) ...[
            const SizedBox(width: 10),
            Text(
              '${_clock(value.position)} / ${_clock(value.duration)}',
              style: TextStyle(fontSize: FontSizes.xs2, color: t.n600),
            ),
          ],
        ],
      ),
    );
  }
}
