import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/utils/error_text.dart';
import '../../../shared/widgets/media_viewer.dart';
import '../state/light_providers.dart';
import 'production_api.dart';

const silkRoadVideo = 'assets/light_production/silk-road.mp4';
const silkRoadPoster = 'assets/light_production/silk-road.jpg';
const sodaStoryVideo = 'assets/light_production/soda-story.mp4';
const sodaStoryPoster = 'assets/light_production/soda-story.jpg';
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
    return GridView.builder(
      shrinkWrap: true,
      physics: const NeverScrollableScrollPhysics(),
      gridDelegate: const SliverGridDelegateWithFixedCrossAxisCount(
        crossAxisCount: 2,
        mainAxisSpacing: 10,
        crossAxisSpacing: 10,
      ),
      itemCount: materials.length,
      itemBuilder: (context, index) {
        final media = materials[index];
        return Stack(
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
      },
    );
  }
}

class ProductionThumbnail extends StatelessWidget {
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
  Widget build(BuildContext context) {
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
    return ClipRRect(
      borderRadius: BorderRadius.circular(12),
      child: Stack(
        fit: StackFit.expand,
        children: [
          if (media.thumbnail case final url? when url.isNotEmpty)
            Image.network(
              url,
              fit: fit,
              errorBuilder: (_, _, _) => placeholder,
              frameBuilder: (_, child, frame, _) =>
                  frame == null ? placeholder : child,
            )
          else
            placeholder,
          if (showVideoBadge && media.video && media.thumbnail != null)
            const Positioned(
              left: 8,
              bottom: 8,
              child: Icon(
                Icons.play_circle_fill,
                color: Colors.white,
                size: 21,
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
    final t = context.tokens;
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
                        return Semantics(
                          selected: selected,
                          button: true,
                          label: media.name,
                          child: InkWell(
                            key: Key('production-library-${media.id}'),
                            borderRadius: BorderRadius.circular(14),
                            onTap:
                                !_selected.contains(media.id) &&
                                    _selected.length >= 10
                                ? null
                                : () => setState(() {
                                    if (!_selected.remove(media.id)) {
                                      _selected.add(media.id);
                                    }
                                  }),
                            child: Stack(
                              fit: StackFit.expand,
                              children: [
                                ProductionThumbnail(media: media),
                                Positioned(
                                  left: 5,
                                  right: 5,
                                  bottom: 5,
                                  child: Text(
                                    media.name,
                                    maxLines: 1,
                                    overflow: TextOverflow.ellipsis,
                                    style: TextStyle(
                                      color: media.thumbnail == null
                                          ? t.ink
                                          : Colors.white,
                                      backgroundColor: media.thumbnail == null
                                          ? null
                                          : Colors.black54,
                                      fontSize: 11,
                                    ),
                                  ),
                                ),
                                if (selected)
                                  Positioned(
                                    right: 5,
                                    top: 5,
                                    child: Icon(
                                      Icons.check_circle,
                                      color: t.a700,
                                    ),
                                  ),
                              ],
                            ),
                          ),
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
