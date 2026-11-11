import 'package:flutter/material.dart';
import 'package:video_player/video_player.dart';

import '../appearance/tokens.dart';
import '../appearance/type_scale.dart';

/// A sample tile (Light gallery, first-video templates): the still (or the
/// image sample itself), a play mark on clips, a tinted tile while there is
/// no picture.
class SampleThumb extends StatelessWidget {
  const SampleThumb({
    super.key,
    required this.thumbUrl,
    required this.isVideo,
    required this.shade,
    this.assetStill,
    this.width = 72,
    this.height = 108,
    this.onPlay,
  });

  final String? thumbUrl;
  final bool isVideo;
  final int shade;

  /// A bundled still, used when there is no [thumbUrl].
  final String? assetStill;
  final double width;
  final double height;
  final VoidCallback? onPlay;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final tint = ColoredBox(color: [t.n900, t.n800, t.n700][shade % 3]);
    return ClipRRect(
      borderRadius: BorderRadius.circular(Radii.md),
      child: SizedBox(
        width: width,
        height: height,
        child: Stack(
          fit: StackFit.expand,
          children: [
            if (thumbUrl != null)
              Image.network(
                thumbUrl!,
                fit: BoxFit.cover,
                // The tint holds the tile while the still downloads.
                frameBuilder: (_, child, frame, sync) =>
                    sync || frame != null ? child : tint,
                errorBuilder: (_, _, _) => tint,
              )
            else if (assetStill != null)
              Image.asset(
                assetStill!,
                fit: BoxFit.cover,
                errorBuilder: (_, _, _) => tint,
              )
            else
              tint,
            if (isVideo)
              Material(
                type: MaterialType.transparency,
                child: InkWell(
                  onTap: onPlay,
                  child: Center(
                    child: Icon(
                      Icons.play_circle_fill,
                      color: t.mediaLabel,
                      size: 30,
                      shadows: [Shadow(blurRadius: 8, color: t.mediaShadow)],
                    ),
                  ),
                ),
              ),
          ],
        ),
      ),
    );
  }
}

/// Plays a sample clip full screen over the page; tap pauses, the close
/// button (or back) returns. [failedText] shows when the clip will not load.
Future<void> showSamplePlayer(
  BuildContext context,
  String url, {
  required String failedText,
}) {
  return Navigator.of(context, rootNavigator: true).push(
    PageRouteBuilder<void>(
      opaque: false,
      barrierColor: context.tokens.term.withValues(alpha: 0.94),
      barrierDismissible: true,
      pageBuilder: (_, _, _) =>
          _SamplePlayerPage(url: url, failedText: failedText),
      transitionsBuilder: (_, animation, _, child) =>
          FadeTransition(opacity: animation, child: child),
    ),
  );
}

class _SamplePlayerPage extends StatefulWidget {
  const _SamplePlayerPage({required this.url, required this.failedText});

  final String url;
  final String failedText;

  @override
  State<_SamplePlayerPage> createState() => _SamplePlayerPageState();
}

class _SamplePlayerPageState extends State<_SamplePlayerPage> {
  late final VideoPlayerController _controller =
      VideoPlayerController.networkUrl(Uri.parse(widget.url));
  bool _ready = false;
  bool _failed = false;

  @override
  void initState() {
    super.initState();
    _start();
  }

  Future<void> _start() async {
    try {
      await _controller.initialize();
      if (!mounted) return;
      await _controller.setLooping(true);
      setState(() => _ready = true);
      await _controller.play();
    } catch (_) {
      if (mounted) setState(() => _failed = true);
    }
  }

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    Widget inner;
    if (_failed) {
      inner = Center(
        child: Text(
          widget.failedText,
          style: TextStyle(color: t.mediaLabel, fontSize: FontSizes.sm),
        ),
      );
    } else if (!_ready) {
      inner = Center(
        child: CircularProgressIndicator(strokeWidth: 2, color: t.mediaLabel),
      );
    } else {
      inner = GestureDetector(
        onTap: () => setState(() {
          _controller.value.isPlaying
              ? _controller.pause()
              : _controller.play();
        }),
        child: Stack(
          fit: StackFit.expand,
          children: [
            FittedBox(
              fit: BoxFit.contain,
              child: SizedBox(
                width: _controller.value.size.width,
                height: _controller.value.size.height,
                child: VideoPlayer(_controller),
              ),
            ),
            if (!_controller.value.isPlaying)
              Center(
                child: Icon(
                  Icons.play_circle_fill,
                  size: 64,
                  color: t.mediaLabel,
                ),
              ),
          ],
        ),
      );
    }
    return Scaffold(
      backgroundColor: Colors.transparent,
      body: SafeArea(
        child: Stack(
          children: [
            Positioned.fill(
              child: Padding(
                padding: const EdgeInsets.fromLTRB(16, 52, 16, 16),
                child: Center(
                  child: AspectRatio(
                    aspectRatio: 9 / 16,
                    child: ClipRRect(
                      borderRadius: BorderRadius.circular(Radii.xl2),
                      child: ColoredBox(color: t.term, child: inner),
                    ),
                  ),
                ),
              ),
            ),
            Positioned(
              top: 4,
              right: 8,
              child: IconButton(
                key: const Key('light-sample-player-close'),
                onPressed: () => Navigator.of(context).maybePop(),
                icon: Icon(Icons.close, color: t.mediaLabel, size: 28),
              ),
            ),
          ],
        ),
      ),
    );
  }
}
