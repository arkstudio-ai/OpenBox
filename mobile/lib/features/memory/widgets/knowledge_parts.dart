import 'package:flutter/material.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../utils/knowledge_text.dart';

/// The knowledge page's own look (web `knowledge/ui.ts` + `parts.tsx`): the
/// same pill buttons and hairline cards as the skills centre.

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

enum StatusTone { ok, warn, danger, muted }

/// One status in one tone (web `StatusPill`).
class KnowledgeStatusPill extends StatelessWidget {
  const KnowledgeStatusPill({
    super.key,
    required this.label,
    required this.tone,
  });

  final String label;
  final StatusTone tone;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final (background, foreground) = switch (tone) {
      StatusTone.ok => (t.s100, t.s800),
      StatusTone.warn => (t.a200, t.n800),
      StatusTone.danger => (t.dangerSoft, t.dangerInk),
      StatusTone.muted => (t.hairSoft, t.n700),
    };
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
      decoration: BoxDecoration(
        color: background,
        borderRadius: BorderRadius.circular(Radii.full),
      ),
      child: Text(
        label,
        style: TextStyle(fontSize: FontSizes.xs2, color: foreground),
      ),
    );
  }
}

/// A titled block of the overview, with its way into the full list.
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
    return Padding(
      padding: const EdgeInsets.only(bottom: 10),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.center,
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
                        fontSize: FontSizes.sm,
                        fontWeight: FontWeight.w400,
                        color: t.n500,
                      ),
                    ),
                ],
              ),
              style: TextStyle(
                fontSize: FontSizes.xl,
                fontWeight: FontWeight.w600,
                color: t.ink,
              ),
            ),
          ),
          if (onSeeAll != null && seeAll != null)
            TextButton.icon(
              onPressed: onSeeAll,
              iconAlignment: IconAlignment.end,
              icon: Icon(Icons.arrow_forward, size: 14, color: t.n700),
              label: Text(
                seeAll!,
                style: TextStyle(fontSize: FontSizes.sm, color: t.n700),
              ),
            ),
        ],
      ),
    );
  }
}

/// The one-line explanation at the top of a full list.
class IntroText extends StatelessWidget {
  const IntroText(this.text, {super.key});

  final String text;

  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.only(bottom: 14),
    child: Text(
      text,
      style: TextStyle(
        fontSize: FontSizes.sm,
        height: 1.6,
        color: context.tokens.n600,
      ),
    ),
  );
}

/// A hairline card.
class KnowledgeCard extends StatelessWidget {
  const KnowledgeCard({super.key, required this.child, this.padding});

  final Widget child;
  final EdgeInsetsGeometry? padding;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Container(
      padding: padding,
      clipBehavior: Clip.antiAlias,
      decoration: BoxDecoration(
        color: t.card,
        border: Border.all(color: t.hair),
        borderRadius: BorderRadius.circular(Radii.xl),
      ),
      child: child,
    );
  }
}

/// A section with nothing in it yet: what will appear, and how to start.
class QuietCard extends StatelessWidget {
  const QuietCard({super.key, required this.text, this.action});

  final String text;
  final Widget? action;

  @override
  Widget build(BuildContext context) => KnowledgeCard(
    padding: const EdgeInsets.fromLTRB(16, 14, 16, 14),
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
        if (action != null) ...[const SizedBox(height: 10), action!],
      ],
    ),
  );
}

/// Grey blocks where rows are about to appear. Still, not animated: a list
/// that is loading should not compete with the one that is not.
class SkeletonRows extends StatelessWidget {
  const SkeletonRows({super.key, this.rows = 3});

  final int rows;

  @override
  Widget build(BuildContext context) => ExcludeSemantics(
    child: Column(
      children: [
        for (var i = 0; i < rows; i++)
          Container(
            height: 60,
            margin: const EdgeInsets.only(bottom: 8),
            decoration: BoxDecoration(
              color: context.tokens.hairSoft,
              borderRadius: BorderRadius.circular(Radii.xl),
            ),
          ),
      ],
    ),
  );
}

/// A soft red notice.
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

/// "Load more" under a server-paged list.
class LoadMoreButton extends StatelessWidget {
  const LoadMoreButton({
    super.key,
    required this.label,
    required this.pending,
    required this.onPressed,
  });

  final String label;
  final bool pending;
  final VoidCallback onPressed;

  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.only(top: 16),
    child: Center(
      child: KnowledgeButton(
        label: label,
        onPressed: pending ? null : onPressed,
      ),
    ),
  );
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
      borderRadius: BorderRadius.circular(Radii.md),
    ),
    child: Icon(icon, size: size * 0.48, color: foreground),
  );
}
