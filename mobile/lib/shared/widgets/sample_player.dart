import 'package:flutter/material.dart';

import '../appearance/tokens.dart';
import '../appearance/type_scale.dart';
import 'media_viewer.dart';

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

/// Opens a sample clip in the shared media viewer, looping it by default.
Future<void> showSamplePlayer(
  BuildContext context,
  String url, {
  required String failedText,
  String? title,
}) => showMediaViewer(
  context,
  (_) => MediaViewer(
    title: title,
    child: MediaPreviewVideo(source: url, failedText: failedText, loop: true),
  ),
);
