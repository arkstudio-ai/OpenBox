import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:video_player/video_player.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../api/first_video_api.dart';

/// The finished video, portrait, playing on its own in a loop; a tap pauses
/// and resumes. Same playback shape as the chat gallery's video box, with
/// the asset URL fetched through this feature's own client.
class FirstVideoPlayer extends ConsumerStatefulWidget {
  const FirstVideoPlayer({super.key, required this.assetId});

  final String assetId;

  @override
  ConsumerState<FirstVideoPlayer> createState() => _FirstVideoPlayerState();
}

class _FirstVideoPlayerState extends ConsumerState<FirstVideoPlayer> {
  VideoPlayerController? _controller;
  bool _ready = false;
  bool _failed = false;

  @override
  void initState() {
    super.initState();
    _start();
  }

  Future<void> _start() async {
    String? url;
    try {
      url = await ref.read(firstVideoApiProvider).assetUrl(widget.assetId);
    } catch (_) {
      url = null;
    }
    if (!mounted) return;
    if (url == null || url.isEmpty) {
      setState(() => _failed = true);
      return;
    }
    final controller = VideoPlayerController.networkUrl(Uri.parse(url));
    _controller = controller;
    try {
      await controller.initialize();
      if (!mounted) return;
      await controller.setLooping(true);
      setState(() => _ready = true);
      await controller.play();
    } catch (_) {
      if (mounted) setState(() => _failed = true);
    }
  }

  @override
  void dispose() {
    _controller?.dispose();
    super.dispose();
  }

  void _toggle() {
    final controller = _controller;
    if (controller == null || !_ready) return;
    setState(() {
      controller.value.isPlaying ? controller.pause() : controller.play();
    });
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final controller = _controller;
    Widget inner;
    if (_failed) {
      inner = Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Icon(
              Icons.videocam_off_outlined,
              color: Colors.white54,
              size: 36,
            ),
            const SizedBox(height: 8),
            Text(
              i18n.t('first_video:result.playFailed'),
              style: const TextStyle(
                color: Colors.white70,
                fontSize: FontSizes.sm,
              ),
            ),
          ],
        ),
      );
    } else if (!_ready || controller == null) {
      inner = const Center(
        child: CircularProgressIndicator(strokeWidth: 2, color: Colors.white70),
      );
    } else {
      inner = GestureDetector(
        onTap: _toggle,
        child: Stack(
          fit: StackFit.expand,
          children: [
            FittedBox(
              fit: BoxFit.cover,
              clipBehavior: Clip.hardEdge,
              child: SizedBox(
                width: controller.value.size.width,
                height: controller.value.size.height,
                child: VideoPlayer(controller),
              ),
            ),
            if (!controller.value.isPlaying)
              const Center(
                child: Icon(
                  Icons.play_circle_fill,
                  size: 64,
                  color: Colors.white,
                ),
              ),
            Positioned(
              left: 0,
              right: 0,
              bottom: 0,
              child: VideoProgressIndicator(
                controller,
                allowScrubbing: true,
                colors: VideoProgressColors(
                  playedColor: t.bg,
                  bufferedColor: Colors.white38,
                  backgroundColor: Colors.white24,
                ),
                padding: const EdgeInsets.fromLTRB(12, 8, 12, 10),
              ),
            ),
          ],
        ),
      );
    }
    return AspectRatio(
      aspectRatio: 9 / 16,
      child: ClipRRect(
        borderRadius: BorderRadius.circular(Radii.xl2),
        child: ColoredBox(color: t.term, child: inner),
      ),
    );
  }
}
