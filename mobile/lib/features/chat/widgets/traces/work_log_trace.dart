import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../../shared/appearance/tokens.dart';
import '../../../../shared/appearance/type_scale.dart';
import '../../../../shared/i18n/i18n.dart';
import '../../utils/content_view.dart';
import '../attachment_gallery.dart';
import '../markdown_view.dart';
import 'trace_shell.dart';

/// Work log (web `WorkLogTrace`): tool-step narration and working evidence,
/// in the order they happened, folded away behind one row.
///
/// Computer-use produces a screenshot per action; keeping every one of them
/// turns the log into a flip-book, so only the first, middle and last frames
/// are kept and the count of what was dropped is stated rather than implied.
class WorkLogTrace extends ConsumerWidget {
  const WorkLogTrace({
    super.key,
    required this.events,
    required this.active,
    required this.autoCollapseReady,
    this.defaultOpen = false,
  });

  final List<WorkEvent> events;
  final bool active;
  final bool autoCollapseReady;

  /// A turn that ended without an answer opens its log, because the log is
  /// then the only account of what happened.
  final bool defaultOpen;

  static const _maxComputerCheckpoints = 3;

  Set<String> _keyComputerIds() {
    final screenshots = events
        .whereType<ArtifactGroup>()
        .where((g) => g.artifactKind == 'computer_screenshot')
        .toList();
    if (screenshots.length <= _maxComputerCheckpoints) {
      return {for (final item in screenshots) item.id};
    }
    final middle = ((screenshots.length - 1) / 2).round();
    return {
      screenshots.first.id,
      screenshots[middle].id,
      screenshots.last.id,
    };
  }

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    if (events.isEmpty) return const SizedBox.shrink();
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final keyIds = _keyComputerIds();
    final narrationCount = events.whereType<WorkNarration>().length;
    final evidenceCount = events.whereType<ArtifactGroup>().length;
    final displayed = events
        .where((event) =>
            event is! ArtifactGroup ||
            event.artifactKind != 'computer_screenshot' ||
            keyIds.contains(event.id))
        .toList();
    final hidden = events.length - displayed.length;

    return TraceShell(
      title: active
          ? i18n.t('chat:trace.work.titleActive')
          : i18n.t('chat:trace.work.titleDone'),
      summary: i18n.t('chat:trace.work.summary', vars: {
        'messages': narrationCount,
        'screenshots': evidenceCount,
      }),
      active: active,
      autoCollapseReady: autoCollapseReady,
      defaultOpen: defaultOpen,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          for (final (index, event) in displayed.indexed)
            _WorkRow(
              event: event,
              isFirst: index == 0,
              isLast: index == displayed.length - 1,
              streaming: active,
            ),
          if (hidden > 0)
            Padding(
              padding: EdgeInsets.only(
                top: 4,
                left: displayed.isEmpty ? 0 : 22,
              ),
              child: Text(
                i18n.t('chat:trace.work.omitted', count: hidden),
                style: TextStyle(fontSize: FontSizes.xs2, color: t.n600),
              ),
            ),
        ],
      ),
    );
  }
}

/// One entry with the connector rail the tool chain uses, so the two traces
/// read as the same kind of timeline.
class _WorkRow extends ConsumerWidget {
  const _WorkRow({
    required this.event,
    required this.isFirst,
    required this.isLast,
    required this.streaming,
  });

  final WorkEvent event;
  final bool isFirst;
  final bool isLast;
  final bool streaming;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return IntrinsicHeight(
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          SizedBox(
            width: 14,
            child: Column(
              children: [
                SizedBox(
                  height: 7,
                  child: isFirst
                      ? null
                      : Center(
                          child: Container(width: 1, color: t.hair),
                        ),
                ),
                Container(
                  width: 6,
                  height: 6,
                  decoration: BoxDecoration(color: t.n500, shape: BoxShape.circle),
                ),
                if (!isLast)
                  Expanded(child: Center(child: Container(width: 1, color: t.hair))),
              ],
            ),
          ),
          const SizedBox(width: 8),
          Expanded(
            child: Padding(
              padding: const EdgeInsets.only(bottom: 8),
              child: switch (event) {
                WorkNarration(:final text) => MarkdownView(
                    text,
                    variant: MarkdownVariant.thinking,
                    streaming: streaming,
                  ),
                final ArtifactGroup group => Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(
                        group.sourceTool?.title?.trim().isNotEmpty == true
                            ? group.sourceTool!.title!
                            : group.label ?? i18n.t('chat:trace.work.checkpoint'),
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: TextStyle(
                          fontSize: FontSizes.xs,
                          fontWeight: FontWeight.w500,
                          color: t.n700,
                        ),
                      ),
                      const SizedBox(height: 5),
                      AttachmentGallery(
                        parts: group.parts.where(isGalleryMedia).toList(),
                        compact: true,
                      ),
                    ],
                  ),
              },
            ),
          ),
        ],
      ),
    );
  }
}
