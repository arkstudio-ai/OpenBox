import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/light.dart';
import '../../../shared/widgets/media_viewer.dart';
import '../api/light_api.dart';
import 'production_api.dart';
import 'production_media.dart';
import 'production_voice.dart' show VoiceFooter;
import 'production_widgets.dart';

final _examplesProvider = FutureProvider.autoDispose
    .family<List<LightExamplePreview>, String>((ref, entryId) {
      return ref.watch(lightApiProvider).productionExamples(entryId);
    });

class ProductionExamples extends ConsumerWidget {
  const ProductionExamples({
    super.key,
    required this.task,
    required this.enabled,
    required this.onChoose,
  });

  final LightProduction task;
  final bool enabled;
  final Future<bool> Function(String) onChoose;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final i18n = ref.watch(i18nProvider);
    final request = _examplesProvider(task.kind.id);
    final result = ref.watch(request);
    final content = result.when(
      data: (rows) {
        final available = rows
            .where(
              (row) =>
                  !row.exampleId.startsWith('mtb_') ||
                  task.availableTemplates.contains(row.exampleId.substring(4)),
            )
            .toList();
        if (available.isEmpty) {
          return Text(i18n.t('light:production.examples.emptyLibrary'));
        }
        return _ExampleCategories(
          rows: available,
          task: task,
          enabled: enabled,
          onChoose: onChoose,
        );
      },
      loading: () => const Center(
        child: Padding(
          padding: EdgeInsets.all(24),
          child: CircularProgressIndicator(),
        ),
      ),
      error: (_, _) => TextButton.icon(
        onPressed: () => ref.invalidate(request),
        icon: const Icon(Icons.refresh),
        label: Text(i18n.t('common:action.retry')),
      ),
    );
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Row(
          children: [
            Expanded(
              child: Text(
                i18n.t('light:production.examples.videoLibrary'),
                style: Theme.of(context).textTheme.titleMedium,
              ),
            ),
            IconButton(
              tooltip: i18n.t('resources:templates.refresh'),
              icon: const Icon(Icons.refresh),
              onPressed: enabled ? () => ref.invalidate(request) : null,
            ),
          ],
        ),
        content,
      ],
    );
  }
}

class ProductionExampleSummary extends ConsumerWidget {
  const ProductionExampleSummary({
    super.key,
    required this.task,
    required this.onChange,
  });

  final LightProduction task;
  final VoidCallback? onChange;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final i18n = ref.watch(i18nProvider);
    final rows = task.templateTrial
        ? null
        : ref.watch(_examplesProvider(task.kind.id)).value;
    final example = rows
        ?.where((row) => row.exampleId == task.referenceExampleId)
        .firstOrNull;
    return Material(
      color: context.tokens.hairSoft,
      borderRadius: BorderRadius.circular(16),
      child: ListTile(
        key: const Key('production-change-example'),
        leading: example == null
            ? const Icon(Icons.image_outlined)
            : _ExamplePreview(row: example),
        title: Text(task.referenceTitleFor(i18n.language)),
        subtitle: Text(i18n.t('light:production.poster.selectedExample')),
        trailing: onChange == null
            ? null
            : Text(i18n.t('light:production.poster.changeExample')),
        onTap: onChange,
      ),
    );
  }
}

class _ExampleCategories extends ConsumerWidget {
  const _ExampleCategories({
    required this.rows,
    required this.task,
    required this.enabled,
    required this.onChoose,
  });

  final List<LightExamplePreview> rows;
  final LightProduction task;
  final bool enabled;
  final Future<bool> Function(String) onChoose;

  String _categoryLabel(I18nState i18n, LightExamplePreview example) {
    final scene = example.scene;
    if (scene == null || example.sceneLabel.isEmpty) {
      return i18n.t('light:production.examples.unclassified');
    }
    final categories = switch (task.kind) {
      ProductionKind.poster => 'posterCategories',
      ProductionKind.video => 'videoCategories',
      ProductionKind.menu => 'menuCategories',
      ProductionKind.footage => 'footageCategories',
    };
    final path = 'production.examples.$categories.$scene';
    final shippedLabel = i18n.bundle.lookup('zh-CN', 'light', path);
    return shippedLabel == example.sceneLabel
        ? i18n.t('light:$path')
        : example.sceneLabel;
  }

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final i18n = ref.watch(i18nProvider);
    final byScene = <String?, List<LightExamplePreview>>{};
    for (final row in rows) {
      byScene.putIfAbsent(row.scene, () => []).add(row);
    }
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        if (task.referenceTitleFor(i18n.language).isNotEmpty) ...[
          ProductionTag(
            text: i18n.t(
              'light:production.examples.selected',
              vars: {'title': task.referenceTitleFor(i18n.language)},
            ),
          ),
          const SizedBox(height: 12),
        ],
        LayoutBuilder(
          builder: (context, constraints) {
            const spacing = 12.0;
            final width = task.kind.imagesOnly && constraints.maxWidth >= 300
                ? (constraints.maxWidth - spacing) / 2
                : constraints.maxWidth;
            return Wrap(
              spacing: spacing,
              runSpacing: spacing,
              children: [
                for (final category in byScene.entries)
                  SizedBox(
                    width: width,
                    child: _ExampleCategoryCard(
                      kind: task.kind,
                      scene: category.key,
                      title: _categoryLabel(i18n, category.value.first),
                      rows: category.value,
                      selectedId: task.referenceExampleId,
                      enabled: enabled,
                      onChoose: onChoose,
                    ),
                  ),
              ],
            );
          },
        ),
      ],
    );
  }
}

class _ExampleCategoryCard extends ConsumerWidget {
  const _ExampleCategoryCard({
    required this.kind,
    required this.scene,
    required this.title,
    required this.rows,
    required this.selectedId,
    required this.enabled,
    required this.onChoose,
  });

  final ProductionKind kind;
  final String? scene;
  final String title;
  final List<LightExamplePreview> rows;
  final String? selectedId;
  final bool enabled;
  final Future<bool> Function(String) onChoose;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final selected = rows.any((row) => row.exampleId == selectedId);
    return Material(
      color: selected ? t.hairSoft : t.card,
      clipBehavior: Clip.antiAlias,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(16),
        side: BorderSide(color: selected ? t.a700 : t.hair),
      ),
      child: InkWell(
        key: Key('production-${kind.id}-category-${scene ?? 'unclassified'}'),
        onTap: !enabled
            ? null
            : () async {
                final id = await Navigator.of(context).push<String>(
                  MaterialPageRoute(
                    builder: (context) => kind == ProductionKind.video
                        ? _VideoCategoryPage(
                            title: title,
                            rows: rows,
                            selected: selectedId,
                            onChoose: onChoose,
                          )
                        : Scaffold(
                            backgroundColor: context.tokens.bg,
                            appBar: AppBar(title: Text(title)),
                            body: SafeArea(
                              top: false,
                              child: _ExampleList(
                                rows: rows,
                                selected: selectedId,
                                showTags: true,
                              ),
                            ),
                          ),
                  ),
                );
                if (id != null && context.mounted) await onChoose(id);
              },
        child: kind == ProductionKind.video
            ? _videoRow(t, selected)
            : Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  AspectRatio(
                    aspectRatio: 1.65,
                    child: _ExampleThumbnail(row: rows.first),
                  ),
                  Padding(
                    padding: const EdgeInsets.fromLTRB(12, 10, 12, 12),
                    child: Row(
                      children: [
                        Expanded(
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Text(
                                title,
                                maxLines: 2,
                                overflow: TextOverflow.ellipsis,
                                style: TextStyle(
                                  color: t.ink,
                                  fontSize: 15,
                                  fontWeight: FontWeight.w700,
                                ),
                              ),
                              const SizedBox(height: 3),
                              Text(
                                i18n.t(
                                  'light:production.examples.count',
                                  count: rows.length,
                                ),
                                style: TextStyle(color: t.n800, fontSize: 12),
                              ),
                            ],
                          ),
                        ),
                        Icon(
                          selected ? Icons.check_circle : Icons.chevron_right,
                          color: selected ? t.a700 : t.n700,
                          size: 19,
                        ),
                      ],
                    ),
                  ),
                ],
              ),
      ),
    );
  }

  Widget _videoRow(BossipTokens t, bool selected) => Padding(
    padding: const EdgeInsets.all(10),
    child: Row(
      children: [
        ClipRRect(
          borderRadius: BorderRadius.circular(10),
          child: SizedBox(
            width: 124,
            height: 88,
            child: _ExampleThumbnail(row: rows.first),
          ),
        ),
        const SizedBox(width: 12),
        Expanded(
          child: Text(
            title,
            style: TextStyle(
              color: t.ink,
              fontWeight: FontWeight.w700,
              fontSize: 16,
            ),
          ),
        ),
        Icon(
          selected ? Icons.check_circle : Icons.chevron_right,
          color: selected ? t.a700 : t.n700,
          size: 20,
        ),
      ],
    ),
  );
}

class _VideoCategoryPage extends ConsumerStatefulWidget {
  const _VideoCategoryPage({
    required this.title,
    required this.rows,
    required this.selected,
    required this.onChoose,
  });
  final String title;
  final List<LightExamplePreview> rows;
  final String? selected;
  final Future<bool> Function(String) onChoose;

  @override
  ConsumerState<_VideoCategoryPage> createState() => _VideoCategoryPageState();
}

class _VideoCategoryPageState extends ConsumerState<_VideoCategoryPage> {
  late String? _selected =
      widget.rows.any((row) => row.exampleId == widget.selected)
      ? widget.selected
      : null;
  bool _saving = false;

  Future<void> _confirm() async {
    final selected = _selected;
    if (selected == null || _saving) return;
    setState(() => _saving = true);
    final saved = await widget.onChoose(selected);
    if (!mounted) return;
    setState(() => _saving = false);
    if (saved && ModalRoute.of(context)?.isCurrent == true) {
      Navigator.pop(context);
    }
  }

  @override
  Widget build(BuildContext context) {
    final i18n = ref.watch(i18nProvider);
    final t = context.tokens;
    return Scaffold(
      appBar: AppBar(title: Text(ProductionKind.video.label(i18n))),
      body: ListView(
        padding: const EdgeInsets.all(16),
        children: [
          ProductionHeading(
            widget.title,
            description: i18n.t('light:production.examples.choose'),
          ),
          const SizedBox(height: 16),
          for (final row in widget.rows)
            Padding(
              padding: const EdgeInsets.only(bottom: 10),
              child: Material(
                color: _selected == row.exampleId ? t.hairSoft : t.card,
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(14),
                  side: BorderSide(
                    color: _selected == row.exampleId ? t.a700 : t.hair,
                  ),
                ),
                child: InkWell(
                  key: Key('production-example-${row.exampleId}'),
                  borderRadius: BorderRadius.circular(14),
                  onTap: _saving
                      ? null
                      : () => setState(() => _selected = row.exampleId),
                  child: Padding(
                    padding: const EdgeInsets.all(10),
                    child: Row(
                      children: [
                        _ExamplePreview(row: row, width: 126, height: 80),
                        const SizedBox(width: 12),
                        Expanded(
                          child: Text(
                            row.titleFor(i18n.language),
                            style: const TextStyle(
                              fontWeight: FontWeight.w700,
                              fontSize: 16,
                            ),
                          ),
                        ),
                        Icon(
                          _selected == row.exampleId
                              ? Icons.check_circle
                              : Icons.radio_button_unchecked,
                          color: _selected == row.exampleId ? t.a700 : t.n700,
                        ),
                      ],
                    ),
                  ),
                ),
              ),
            ),
        ],
      ),
      bottomNavigationBar: VoiceFooter(
        label: i18n.t('light:production.examples.useVideoTemplate'),
        onPressed: _selected == null || _saving ? null : _confirm,
      ),
    );
  }
}

class _ExampleList extends ConsumerWidget {
  const _ExampleList({
    required this.rows,
    required this.selected,
    this.showTags = false,
  });

  final List<LightExamplePreview> rows;
  final String? selected;
  final bool showTags;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final i18n = ref.watch(i18nProvider);
    final t = context.tokens;
    return ListView.builder(
      padding: const EdgeInsets.symmetric(horizontal: 8),
      itemCount: rows.length,
      itemBuilder: (context, index) {
        final row = rows[index];
        final checked = row.exampleId == selected;
        return ListTile(
          key: Key('production-example-${row.exampleId}'),
          leading: _ExamplePreview(row: row),
          title: Text(row.titleFor(i18n.language), maxLines: 2),
          selected: checked,
          trailing: Icon(
            checked ? Icons.check_circle : Icons.radio_button_unchecked,
          ),
          onTap: () => Navigator.pop(context, row.exampleId),
          subtitle: Text(
            showTags && row.tags.isNotEmpty
                ? row.tags.join(' · ')
                : i18n.t('light:production.examples.choose'),
            style: TextStyle(color: t.n800, fontSize: 12),
          ),
        );
      },
    );
  }
}

class _ExamplePreview extends ConsumerWidget {
  const _ExamplePreview({required this.row, this.width = 54, this.height = 54});

  final LightExamplePreview row;
  final double width, height;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final i18n = ref.watch(i18nProvider);
    final previewLabel = i18n.t('light:production.examples.preview');
    final media = row.mediaUrl;
    final title = row.titleFor(i18n.language);
    return Tooltip(
      message: previewLabel,
      child: InkWell(
        borderRadius: BorderRadius.circular(8),
        onTap: media == null
            ? null
            : () {
                if (row.isVideo) {
                  showProductionVideo(context, media, title: title);
                } else {
                  showMediaViewer(
                    context,
                    (_) => MediaViewer(
                      title: title,
                      child: MediaPreviewImage(
                        url: media,
                        failedText: i18n.t('light:example.viewFailed'),
                      ),
                    ),
                  );
                }
              },
        child: ClipRRect(
          borderRadius: BorderRadius.circular(8),
          child: SizedBox(
            width: width,
            height: height,
            child: _ExampleThumbnail(row: row),
          ),
        ),
      ),
    );
  }
}

class _ExampleThumbnail extends StatelessWidget {
  const _ExampleThumbnail({required this.row});

  final LightExamplePreview row;

  @override
  Widget build(BuildContext context) {
    final placeholder = ColoredBox(
      color: context.tokens.hairSoft,
      child: Icon(row.isVideo ? Icons.play_arrow : Icons.image_outlined),
    );
    final url = row.posterUrl;
    return url == null
        ? placeholder
        : Image.network(
            url,
            fit: BoxFit.cover,
            frameBuilder: (_, image, frame, _) =>
                frame == null ? placeholder : image,
            errorBuilder: (_, _, _) => placeholder,
          );
  }
}
