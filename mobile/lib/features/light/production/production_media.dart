import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/utils/error_text.dart';
import '../../../shared/widgets/media_viewer.dart';
import '../../chat/api/assets_api.dart';
import '../state/light_providers.dart';
import 'production_api.dart';

const headphoneAdVideo = 'assets/light_production/headphone-ad.mp4';
const headphoneAdPoster = 'assets/light_production/headphone-ad.jpg';
const silkRoadVideo = 'assets/light_production/silk-road.mp4';
const silkRoadPoster = 'assets/light_production/silk-road.jpg';
const sodaStoryVideo = 'assets/light_production/soda-story.mp4';
const sodaStoryPoster = 'assets/light_production/soda-story.jpg';
const shortcutStoryVideo = 'assets/light_production/shortcut-story.mp4';
const shortcutStoryPoster = 'assets/light_production/shortcut-story.jpg';
const wanStoryVideo = 'assets/light_production/wan-story.mp4';
const wanStoryPoster = 'assets/light_production/wan-story.jpg';
const crawfishCaseVideo = 'assets/light_production/crawfish-remake.mp4';
const crawfishCasePoster = 'assets/light_production/crawfish-remake.jpg';

class ProductionSelectionGrid extends ConsumerWidget {
  const ProductionSelectionGrid({
    super.key,
    required this.materials,
    required this.onRemove,
  });

  final List<ProductionMedia> materials;
  final ValueChanged<ProductionMedia>? onRemove;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    Widget tile(ProductionMedia media) => Stack(
      key: ValueKey(media.id),
      fit: StackFit.expand,
      children: [
        InkWell(
          onTap: () => openProductionMedia(context, media),
          child: ProductionThumbnail(media: media),
        ),
        Positioned(
          top: 4,
          right: 4,
          child: IconButton.filled(
            tooltip: i18n.t('light:production.remove'),
            onPressed: onRemove == null ? null : () => onRemove!(media),
            style: IconButton.styleFrom(
              backgroundColor: t.bg,
              foregroundColor: t.ink,
            ),
            icon: const Icon(Icons.close, size: 17),
          ),
        ),
      ],
    );

    if (materials.length == 1) {
      return Align(
        alignment: Alignment.topCenter,
        heightFactor: 1,
        child: FractionallySizedBox(
          widthFactor: .5,
          child: AspectRatio(aspectRatio: 1, child: tile(materials.single)),
        ),
      );
    }
    return GridView.builder(
      shrinkWrap: true,
      physics: const NeverScrollableScrollPhysics(),
      gridDelegate: const SliverGridDelegateWithFixedCrossAxisCount(
        crossAxisCount: 2,
        mainAxisSpacing: 10,
        crossAxisSpacing: 10,
      ),
      itemCount: materials.length,
      itemBuilder: (context, index) => tile(materials[index]),
    );
  }
}

class ProductionThumbnail extends StatefulWidget {
  const ProductionThumbnail({
    super.key,
    required this.media,
    this.fit = BoxFit.cover,
    this.showVideoBadge = true,
  });

  final ProductionMedia media;
  final BoxFit fit;
  final bool showVideoBadge;

  @override
  State<ProductionThumbnail> createState() => _ProductionThumbnailState();
}

class _ProductionThumbnailState extends State<ProductionThumbnail> {
  String? _source;
  String? _failedSource;

  String? get _incoming => switch (widget.media.thumbnail) {
    final url? when url.isNotEmpty => url,
    _ => null,
  };

  @override
  void initState() {
    super.initState();
    _source = _incoming;
  }

  @override
  void didUpdateWidget(ProductionThumbnail oldWidget) {
    super.didUpdateWidget(oldWidget);
    final previous = _source;
    final incoming = _incoming;
    if (oldWidget.media.id != widget.media.id ||
        oldWidget.media.video != widget.media.video) {
      _source = incoming;
      _failedSource = null;
    } else if (incoming != null &&
        (previous == null ||
            previous == _failedSource ||
            _thumbnailIdentity(previous) != _thumbnailIdentity(incoming))) {
      _source = incoming;
    }
  }

  // The production poll and draft save re-sign the same OSS object. Keep
  // the active ImageProvider until the object changes or the request fails.
  Uri? _thumbnailIdentity(String source) {
    final uri = Uri.tryParse(source);
    if (uri == null || !uri.queryParameters.containsKey('OSSAccessKeyId')) {
      return uri;
    }
    return uri.replace(
      queryParameters: Map.of(uri.queryParameters)
        ..remove('OSSAccessKeyId')
        ..remove('Expires')
        ..remove('Signature'),
    );
  }

  @override
  Widget build(BuildContext context) {
    final media = widget.media;
    final source = _source;
    final t = context.tokens;
    final placeholder = ColoredBox(
      color: t.surface,
      child: Center(
        child: Icon(
          media.video ? Icons.movie_outlined : Icons.image_outlined,
          color: t.n700,
        ),
      ),
    );
    final fallback = media.video && media.id.isNotEmpty
        ? Consumer(
            builder: (context, ref, _) {
              final frame = ref
                  .watch(videoThumbnailProvider(media.id))
                  .valueOrNull;
              return frame == null
                  ? placeholder
                  : Image.memory(frame, fit: widget.fit, gaplessPlayback: true);
            },
          )
        : placeholder;
    return ClipRRect(
      borderRadius: BorderRadius.circular(12),
      child: Stack(
        fit: StackFit.expand,
        children: [
          if (source != null)
            Image.network(
              source,
              key: ValueKey(media.id),
              fit: widget.fit,
              gaplessPlayback: true,
              frameBuilder: (_, child, frame, _) => frame != null
                  ? child
                  : Stack(
                      fit: StackFit.expand,
                      children: [
                        _failedSource == null ? placeholder : fallback,
                        child,
                      ],
                    ),
              errorBuilder: (_, _, _) {
                _failedSource = source;
                return fallback;
              },
            )
          else
            fallback,
          if (widget.showVideoBadge && media.video)
            const Positioned(
              left: 5,
              top: 5,
              child: CircleAvatar(
                radius: 11,
                backgroundColor: Colors.black54,
                child: Icon(Icons.play_arrow, color: Colors.white, size: 17),
              ),
            ),
        ],
      ),
    );
  }
}

Future<void> showProductionVideo(
  BuildContext context,
  String path, {
  bool bundled = false,
  String? title,
}) => showMediaViewer(
  context,
  (_) => Consumer(
    builder: (context, ref, _) => MediaViewer(
      title: title,
      child: MediaPreviewVideo(
        source: path,
        bundled: bundled,
        failedText: ref.watch(i18nProvider).t('first_video:result.playFailed'),
      ),
    ),
  ),
);

class ProductionResultPreviewButton extends ConsumerWidget {
  const ProductionResultPreviewButton({
    super.key,
    required this.media,
    required this.child,
  });

  final ProductionMedia media;
  final Widget child;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final label = media.video
        ? ref.watch(i18nProvider).t('light:production.playPreview')
        : media.name;
    return Semantics(
      button: true,
      label: label,
      child: InkWell(
        key: const Key('production-result-preview'),
        borderRadius: BorderRadius.circular(12),
        onTap: () => openProductionMedia(context, media),
        child: ExcludeSemantics(child: child),
      ),
    );
  }
}

Future<void> openProductionMedia(BuildContext context, ProductionMedia media) =>
    showMediaViewer(
      context,
      (_) => Consumer(
        builder: (context, ref, _) {
          final i18n = ref.watch(i18nProvider);
          final url = ref.watch(lightAssetUrlProvider(media.id));
          final failed = MediaPreviewError(
            i18n.t('light:production.previewUnavailable'),
          );
          return MediaViewer(
            title: media.name,
            assetId: media.id,
            mimeType: media.mime,
            child: url.when(
              loading: MediaPreviewLoading.new,
              error: (_, _) => failed,
              data: (source) {
                if (source == null || source.isEmpty) return failed;
                return media.video
                    ? MediaPreviewVideo(
                        key: ValueKey(source),
                        source: source,
                        subtitles: media.subtitles,
                        failedText: i18n.t('first_video:result.playFailed'),
                      )
                    : MediaPreviewImage(
                        url: source,
                        failedText: failed.message,
                      );
              },
            ),
          );
        },
      ),
    );

class ProductionLibrary extends ConsumerStatefulWidget {
  const ProductionLibrary({
    super.key,
    required this.imagesOnly,
    required this.selected,
  });

  final bool imagesOnly;
  final List<String> selected;

  @override
  ConsumerState<ProductionLibrary> createState() => _ProductionLibraryState();
}

class _ProductionLibraryState extends ConsumerState<ProductionLibrary> {
  late final _selected = widget.selected.toSet();
  final _media = <ProductionMedia>[];
  String _kind = 'image';
  bool _loading = false;
  bool _hasMore = true;
  Object? _error;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load({String? kind}) async {
    if (_loading) return;
    setState(() {
      _loading = true;
      _error = null;
      if (kind != null) {
        _kind = kind;
        _media.clear();
        _hasMore = true;
      }
    });
    try {
      final page = await ref
          .read(lightProductionApiProvider)
          .library(kind: _kind, offset: _media.length);
      if (mounted) {
        setState(() {
          _media.addAll(page.items.map(ProductionMedia.fromResource));
          _hasMore = page.hasMore;
        });
      }
    } catch (error) {
      if (mounted) setState(() => _error = error);
    } finally {
      if (mounted) setState(() => _loading = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final i18n = ref.watch(i18nProvider);
    return SafeArea(
      child: FractionallySizedBox(
        heightFactor: .82,
        child: Padding(
          padding: const EdgeInsets.fromLTRB(16, 8, 16, 12),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Text(
                i18n.t('light:production.library'),
                style: Theme.of(context).textTheme.titleLarge,
              ),
              if (!widget.imagesOnly) ...[
                const SizedBox(height: 12),
                SegmentedButton<String>(
                  segments: [
                    ButtonSegment(
                      value: 'image',
                      label: Text(i18n.t('light:production.photos')),
                    ),
                    ButtonSegment(
                      value: 'video',
                      label: Text(i18n.t('light:production.videos')),
                    ),
                  ],
                  selected: {_kind},
                  onSelectionChanged: _loading
                      ? null
                      : (values) => _load(kind: values.first),
                ),
              ],
              const SizedBox(height: 12),
              Expanded(
                child: CustomScrollView(
                  slivers: [
                    SliverGrid.builder(
                      gridDelegate:
                          const SliverGridDelegateWithFixedCrossAxisCount(
                            crossAxisCount: 3,
                            mainAxisSpacing: 8,
                            crossAxisSpacing: 8,
                          ),
                      itemCount: _media.length,
                      itemBuilder: (context, index) {
                        final media = _media[index];
                        final selected = _selected.contains(media.id);
                        return _LibraryTile(
                          media: media,
                          selected: selected,
                          onToggle: !selected && _selected.length >= 10
                              ? null
                              : () => setState(() {
                                  if (!_selected.remove(media.id)) {
                                    _selected.add(media.id);
                                  }
                                }),
                        );
                      },
                    ),
                    SliverToBoxAdapter(
                      child: Padding(
                        padding: const EdgeInsets.symmetric(vertical: 12),
                        child: _error != null
                            ? Column(
                                children: [
                                  Text(errorText(i18n, _error!)),
                                  TextButton(
                                    onPressed: _load,
                                    child: Text(i18n.t('common:action.retry')),
                                  ),
                                ],
                              )
                            : _loading
                            ? const LinearProgressIndicator()
                            : _media.isEmpty
                            ? Text(
                                i18n.t('light:production.emptyLibrary'),
                                textAlign: TextAlign.center,
                              )
                            : _hasMore
                            ? TextButton(
                                onPressed: _load,
                                child: Text(i18n.t('light:production.more')),
                              )
                            : null,
                      ),
                    ),
                  ],
                ),
              ),
              FilledButton(
                onPressed: () => Navigator.pop(context, _selected.toList()),
                child: Text(
                  i18n.t(
                    'light:production.confirmMaterials',
                    vars: {'count': _selected.length},
                  ),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _LibraryTile extends ConsumerWidget {
  const _LibraryTile({
    required this.media,
    required this.selected,
    required this.onToggle,
  });

  final ProductionMedia media;
  final bool selected;
  final VoidCallback? onToggle;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final i18n = ref.watch(i18nProvider);
    const radius = BorderRadius.all(Radius.circular(12));
    return Stack(
      fit: StackFit.expand,
      children: [
        Semantics(
          selected: selected,
          button: true,
          label: media.name,
          child: InkWell(
            key: Key('production-library-${media.id}'),
            borderRadius: radius,
            onTap: onToggle,
            child: ClipRRect(
              borderRadius: radius,
              child: Stack(
                fit: StackFit.expand,
                children: [
                  ProductionThumbnail(media: media, showVideoBadge: false),
                  Positioned(
                    left: 0,
                    right: 0,
                    bottom: 0,
                    child: DecoratedBox(
                      decoration: const BoxDecoration(
                        gradient: LinearGradient(
                          begin: Alignment.bottomCenter,
                          end: Alignment.topCenter,
                          colors: [Colors.black87, Colors.transparent],
                        ),
                      ),
                      child: Padding(
                        padding: const EdgeInsets.fromLTRB(5, 12, 5, 5),
                        child: Text(
                          media.name,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: const TextStyle(
                            color: Colors.white,
                            fontSize: 11,
                          ),
                        ),
                      ),
                    ),
                  ),
                  if (selected)
                    PositionedDirectional(
                      end: 5,
                      top: 5,
                      child: Icon(
                        Icons.check_circle,
                        color: context.tokens.a700,
                      ),
                    ),
                ],
              ),
            ),
          ),
        ),
        PositionedDirectional(
          start: 0,
          top: 0,
          child: SizedBox.square(
            dimension: 48,
            child: IconButton(
              key: Key('production-library-preview-${media.id}'),
              padding: EdgeInsets.zero,
              tooltip: i18n.t(
                'light:production.previewMaterial',
                vars: {'name': media.name},
              ),
              onPressed: () => openProductionMedia(context, media),
              icon: CircleAvatar(
                radius: 14,
                backgroundColor: Colors.black54,
                foregroundColor: Colors.white,
                child: Icon(
                  media.video
                      ? Icons.play_arrow_rounded
                      : Icons.fullscreen_rounded,
                  size: 19,
                ),
              ),
            ),
          ),
        ),
      ],
    );
  }
}
