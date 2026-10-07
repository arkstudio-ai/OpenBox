import 'package:flutter/material.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/widgets/spinner.dart';
import '../utils/knowledge_text.dart';

/// The knowledge pages' own look: inset-grouped lists of quiet rows on the
/// warm page background, small section headers, one-line banners, and the
/// pill buttons the skills centre uses for the few places that need one.

/// Rows sit this far in from the group's edges, top and bottom.
const knowledgeRowPadding = EdgeInsets.symmetric(horizontal: 16, vertical: 12);

enum PillTone { plain, primary, danger }

/// A pill button: outlined, solid ink, or soft red.
class KnowledgeButton extends StatelessWidget {
  const KnowledgeButton({
    super.key,
    required this.label,
    required this.onPressed,
    this.icon,
    this.tone = PillTone.plain,
    this.compact = false,
  });

  final String label;
  final VoidCallback? onPressed;
  final IconData? icon;
  final PillTone tone;
  final bool compact;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final enabled = onPressed != null;
    final (background, foreground, border) = switch (tone) {
      PillTone.plain => (t.card, t.ink, t.hair),
      PillTone.primary => (t.ink, t.bg, t.ink),
      PillTone.danger => (t.dangerSoft, t.dangerInk, t.dangerSoft),
    };
    return Opacity(
      opacity: enabled ? 1 : 0.5,
      child: Material(
        color: background,
        shape: StadiumBorder(side: BorderSide(color: border)),
        child: InkWell(
          customBorder: const StadiumBorder(),
          onTap: onPressed,
          child: Container(
            constraints: BoxConstraints(minHeight: compact ? 32 : 38),
            padding: EdgeInsets.symmetric(horizontal: compact ? 12 : 14),
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                if (icon != null) ...[
                  Icon(icon, size: compact ? 14 : 15, color: foreground),
                  const SizedBox(width: 6),
                ],
                Flexible(
                  child: Text(
                    label,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(
                      fontSize: compact ? FontSizes.sm : FontSizes.md,
                      fontWeight: tone == PillTone.plain
                          ? FontWeight.w400
                          : FontWeight.w500,
                      color: foreground,
                    ),
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

/// Text with the current search term marked, so a hit is visible at a glance.
class HighlightText extends StatelessWidget {
  const HighlightText(
    this.text, {
    super.key,
    required this.query,
    required this.style,
    this.maxLines,
  });

  final String text;
  final String query;
  final TextStyle style;
  final int? maxLines;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Text.rich(
      TextSpan(
        children: [
          for (final part in splitMatches(text, query))
            TextSpan(
              text: part.text,
              style: part.hit
                  ? TextStyle(backgroundColor: t.a300, color: t.ink)
                  : null,
            ),
        ],
      ),
      style: style,
      maxLines: maxLines,
      overflow: maxLines == null ? null : TextOverflow.ellipsis,
    );
  }
}

/// An inset-grouped list: rows in one white rounded container, divided by
/// hairlines that start where the rows' text does.
class KnowledgeGroup extends StatelessWidget {
  const KnowledgeGroup({
    super.key,
    required this.children,
    this.dividerIndent = 16,
  });

  final List<Widget> children;

  /// Where a divider starts: under the text, past any leading icon.
  final double dividerIndent;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Material(
      color: t.card,
      clipBehavior: Clip.antiAlias,
      shape: RoundedRectangleBorder(
        side: BorderSide(color: t.hair),
        borderRadius: BorderRadius.circular(Radii.lg),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          for (var i = 0; i < children.length; i++) ...[
            if (i > 0)
              Divider(
                height: 1,
                thickness: 1,
                indent: dividerIndent,
                color: t.hair,
              ),
            children[i],
          ],
        ],
      ),
    );
  }
}

/// A small heading over a group, with its way into the full list.
class SectionHeader extends StatelessWidget {
  const SectionHeader({
    super.key,
    required this.title,
    this.count,
    this.seeAll,
    this.onSeeAll,
  });

  final String title;
  final String? count;
  final String? seeAll;
  final VoidCallback? onSeeAll;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final link = onSeeAll != null && seeAll != null;
    // As tall as its link, with or without one, so every group sits the
    // same distance under its heading.
    return Container(
      constraints: const BoxConstraints(minHeight: 44),
      padding: const EdgeInsets.only(left: 4),
      child: Row(
        children: [
          Expanded(
            child: Text.rich(
              TextSpan(
                children: [
                  TextSpan(text: title),
                  if (count != null)
                    TextSpan(
                      text: '  $count',
                      style: TextStyle(
                        fontWeight: FontWeight.w400,
                        color: t.n500,
                        fontFeatures: const [FontFeature.tabularFigures()],
                      ),
                    ),
                ],
              ),
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(
                fontSize: FontSizes.md,
                fontWeight: FontWeight.w600,
                color: t.n800,
              ),
            ),
          ),
          if (link)
            TextButton(
              onPressed: onSeeAll,
              style: TextButton.styleFrom(
                foregroundColor: t.n600,
                padding: const EdgeInsets.only(left: 10, right: 2),
                minimumSize: const Size(44, 44),
                tapTargetSize: MaterialTapTargetSize.shrinkWrap,
              ),
              child: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Text(
                    seeAll!,
                    style: TextStyle(fontSize: FontSizes.sm, color: t.n600),
                  ),
                  Icon(Icons.chevron_right, size: 16, color: t.n500),
                ],
              ),
            ),
        ],
      ),
    );
  }
}

/// The quiet explanation under a group.
class GroupFooter extends StatelessWidget {
  const GroupFooter(this.text, {super.key});

  final String text;

  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.fromLTRB(4, 8, 4, 0),
    child: Text(
      text,
      style: TextStyle(
        fontSize: FontSizes.xs,
        height: 1.55,
        color: context.tokens.n500,
      ),
    ),
  );
}

/// A group with nothing in it yet: what will appear, and how to start.
class EmptyGroup extends StatelessWidget {
  const EmptyGroup({super.key, required this.text, this.action});

  final String text;
  final Widget? action;

  @override
  Widget build(BuildContext context) => KnowledgeGroup(
    children: [
      Padding(
        padding: EdgeInsets.fromLTRB(16, 14, 16, action == null ? 14 : 6),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              text,
              style: TextStyle(
                fontSize: FontSizes.sm,
                height: 1.6,
                color: context.tokens.n600,
              ),
            ),
            if (action != null) ...[
              const SizedBox(height: 4),
              // Its icon lines up with the text above, not its own padding.
              Transform.translate(offset: const Offset(-8, 0), child: action),
            ],
          ],
        ),
      ),
    ],
  );
}

/// A small text button with a leading icon, for an empty group's first step.
class InlineAction extends StatelessWidget {
  const InlineAction({
    super.key,
    required this.icon,
    required this.label,
    required this.onPressed,
  });

  final IconData icon;
  final String label;
  final VoidCallback? onPressed;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return TextButton.icon(
      onPressed: onPressed,
      style: TextButton.styleFrom(
        foregroundColor: t.ink,
        padding: const EdgeInsets.symmetric(horizontal: 8),
        minimumSize: const Size(44, 36),
        visualDensity: VisualDensity.compact,
      ),
      icon: Icon(icon, size: 16),
      label: Text(
        label,
        style: const TextStyle(
          fontSize: FontSizes.md,
          fontWeight: FontWeight.w500,
        ),
      ),
    );
  }
}

/// "Load more" as the last row of a server-paged group.
class LoadMoreRow extends StatelessWidget {
  const LoadMoreRow({
    super.key,
    required this.label,
    required this.pending,
    required this.onPressed,
  });

  final String label;
  final bool pending;
  final VoidCallback onPressed;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Semantics(
      button: true,
      enabled: !pending,
      child: InkWell(
        onTap: pending ? null : onPressed,
        child: Container(
          constraints: const BoxConstraints(minHeight: 46),
          alignment: Alignment.center,
          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              if (pending) ...[
                const Spinner(size: 12),
                const SizedBox(width: 8),
              ],
              Flexible(
                child: Text(
                  label,
                  textAlign: TextAlign.center,
                  style: TextStyle(
                    fontSize: FontSizes.md,
                    color: pending ? t.n600 : t.n800,
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

/// Grey bars where rows are about to appear. Still, not animated: a list
/// that is loading should not compete with the one that is not.
class SkeletonRows extends StatelessWidget {
  const SkeletonRows({super.key, this.rows = 3});

  final int rows;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    Widget bar(double widthFactor, double height) => FractionallySizedBox(
      alignment: Alignment.centerLeft,
      widthFactor: widthFactor,
      child: Container(
        height: height,
        decoration: BoxDecoration(
          color: t.hair,
          borderRadius: BorderRadius.circular(Radii.sm),
        ),
      ),
    );
    return ExcludeSemantics(
      child: KnowledgeGroup(
        children: [
          for (var i = 0; i < rows; i++)
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 16, 16, 16),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  bar(i.isEven ? 0.78 : 0.62, 11),
                  const SizedBox(height: 9),
                  bar(0.3, 8),
                ],
              ),
            ),
        ],
      ),
    );
  }
}

enum BannerTone { plain, danger }

/// One line about the page as a whole — something being saved, something
/// that could not be read — with the one thing to do about it.
class KnowledgeBanner extends StatelessWidget {
  const KnowledgeBanner({
    super.key,
    required this.leading,
    required this.text,
    this.action,
    this.onAction,
    this.tone = BannerTone.plain,
    this.live = false,
  });

  final Widget leading;
  final String text;
  final String? action;
  final VoidCallback? onAction;
  final BannerTone tone;

  /// Announce changes to assistive technology as they happen.
  final bool live;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final danger = tone == BannerTone.danger;
    final foreground = danger ? t.dangerInk : t.n800;
    return Semantics(
      liveRegion: live,
      container: true,
      child: Container(
        constraints: const BoxConstraints(minHeight: 44),
        padding: EdgeInsets.only(left: 12, right: action == null ? 12 : 0),
        decoration: BoxDecoration(
          color: danger ? t.dangerSoft : t.card,
          border: danger ? null : Border.all(color: t.hair),
          borderRadius: BorderRadius.circular(Radii.md),
        ),
        child: Row(
          children: [
            SizedBox(width: 18, child: Center(child: leading)),
            const SizedBox(width: 10),
            Expanded(
              child: Padding(
                padding: const EdgeInsets.symmetric(vertical: 11),
                child: Text(
                  text,
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(
                    fontSize: FontSizes.sm,
                    height: 1.45,
                    color: foreground,
                  ),
                ),
              ),
            ),
            if (action != null)
              TextButton(
                onPressed: onAction,
                style: TextButton.styleFrom(
                  foregroundColor: danger ? t.dangerInk : t.ink,
                  padding: const EdgeInsets.symmetric(horizontal: 14),
                  // The banner's own height, inside its hairline.
                  minimumSize: const Size(56, 42),
                  tapTargetSize: MaterialTapTargetSize.shrinkWrap,
                ),
                child: Text(
                  action!,
                  style: const TextStyle(
                    fontSize: FontSizes.sm,
                    fontWeight: FontWeight.w500,
                  ),
                ),
              ),
          ],
        ),
      ),
    );
  }
}

/// A soft red notice, for the dialogs and sheets that report a failed write.
class ErrorNotice extends StatelessWidget {
  const ErrorNotice({super.key, required this.text, this.action});

  final String text;
  final Widget? action;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.fromLTRB(14, 10, 8, 10),
      decoration: BoxDecoration(
        color: t.dangerSoft,
        borderRadius: BorderRadius.circular(Radii.lg),
      ),
      child: Row(
        children: [
          Expanded(
            child: Text(
              text,
              style: TextStyle(
                fontSize: FontSizes.sm,
                height: 1.5,
                color: t.dangerInk,
              ),
            ),
          ),
          ?action,
        ],
      ),
    );
  }
}

/// A round-cornered square holding an icon.
class IconTile extends StatelessWidget {
  const IconTile({
    super.key,
    required this.icon,
    required this.background,
    required this.foreground,
    this.size = 36,
  });

  final IconData icon;
  final Color background;
  final Color foreground;
  final double size;

  @override
  Widget build(BuildContext context) => Container(
    width: size,
    height: size,
    decoration: BoxDecoration(
      color: background,
      borderRadius: BorderRadius.circular(size >= 40 ? Radii.lg : Radii.md),
    ),
    child: Icon(icon, size: size * 0.5, color: foreground),
  );
}

/// A small coloured dot that marks a state in a line of text.
WidgetSpan statusDot(Color color) => WidgetSpan(
  alignment: PlaceholderAlignment.middle,
  child: Padding(
    padding: const EdgeInsets.only(right: 5),
    child: Container(
      width: 6,
      height: 6,
      decoration: BoxDecoration(color: color, shape: BoxShape.circle),
    ),
  ),
);
