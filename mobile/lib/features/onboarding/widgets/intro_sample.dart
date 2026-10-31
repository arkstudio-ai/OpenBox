import 'package:flutter/material.dart';
import 'package:flutter/services.dart' show rootBundle;
import 'package:video_player/video_player.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';

/// Bundled sample clip for the intro (portrait, muted, looping). Drop the
/// file at this path — `assets/onboarding/` is already in the bundle — and
/// the intro plays it; until then the poster stands in.
const introSampleVideoAsset = 'assets/onboarding/intro-sample.mp4';
const introSamplePosterAsset = 'assets/onboarding/intro-1.jpg';

/// A finished-looking 9:16 card: the sample video when the app ships one,
/// the poster otherwise, with the shop-name overlay the real videos carry.
class IntroSample extends StatefulWidget {
  const IntroSample({
    super.key,
    required this.tag,
    required this.shopName,
    required this.line,
  });

  final String tag;
  final String shopName;
  final String line;

  @override
  State<IntroSample> createState() => _IntroSampleState();
}

class _IntroSampleState extends State<IntroSample> {
  VideoPlayerController? _controller;
  bool _playing = false;

  @override
  void initState() {
    super.initState();
    _tryVideo();
  }

  Future<void> _tryVideo() async {
    try {
      // The bundle refuses unknown assets with an exception: that is the
      // "no sample yet" signal, and the poster stays.
      await rootBundle.load(introSampleVideoAsset);
    } catch (_) {
      return;
    }
    if (!mounted) return;
    final controller = VideoPlayerController.asset(introSampleVideoAsset);
    _controller = controller;
    try {
      await controller.initialize();
      await controller.setLooping(true);
      await controller.setVolume(0);
      if (!mounted) return;
      setState(() => _playing = true);
      await controller.play();
    } catch (_) {
      // A broken clip is no worse than none.
    }
  }

  @override
  void dispose() {
    _controller?.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final controller = _controller;
    return AspectRatio(
      aspectRatio: 9 / 16,
      child: ClipRRect(
        borderRadius: BorderRadius.circular(Radii.xl2),
        child: Stack(
          fit: StackFit.expand,
          children: [
            ColoredBox(color: t.term),
            if (_playing && controller != null)
              FittedBox(
                fit: BoxFit.cover,
                clipBehavior: Clip.hardEdge,
                child: SizedBox(
                  width: controller.value.size.width,
                  height: controller.value.size.height,
                  child: VideoPlayer(controller),
                ),
              )
            else
              Image.asset(
                introSamplePosterAsset,
                fit: BoxFit.cover,
                errorBuilder: (_, _, _) => const SizedBox.shrink(),
              ),
            // A soft foot so the overlay reads on any frame.
            Positioned(
              left: 0,
              right: 0,
              bottom: 0,
              height: 140,
              child: DecoratedBox(
                decoration: BoxDecoration(
                  gradient: LinearGradient(
                    begin: Alignment.topCenter,
                    end: Alignment.bottomCenter,
                    colors: [Colors.transparent, t.mediaShadow],
                  ),
                ),
              ),
            ),
            Positioned(
              top: 14,
              left: 16,
              child: Text(
                widget.tag,
                style: TextStyle(
                  fontSize: FontSizes.sm,
                  fontWeight: FontWeight.w600,
                  color: t.mediaLabel.withValues(alpha: 0.85),
                ),
              ),
            ),
            if (!_playing)
              Center(
                child: Container(
                  width: 56,
                  height: 56,
                  decoration: BoxDecoration(
                    color: t.mediaLabel,
                    shape: BoxShape.circle,
                  ),
                  child: Icon(Icons.play_arrow_rounded, size: 30, color: t.ink),
                ),
              ),
            Positioned(
              left: 16,
              right: 16,
              bottom: 16,
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    widget.shopName,
                    style: TextStyle(
                      fontSize: FontSizes.xl2,
                      fontWeight: FontWeight.w800,
                      color: t.mediaLabel,
                    ),
                  ),
                  const SizedBox(height: 4),
                  Text(
                    widget.line,
                    style: TextStyle(
                      fontSize: FontSizes.sm,
                      color: t.mediaLabel.withValues(alpha: 0.85),
                    ),
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}
