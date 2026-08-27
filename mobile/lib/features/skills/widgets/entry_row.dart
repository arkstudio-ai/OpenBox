import 'package:flutter/material.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';

/// The row shape every list in the centre uses, so a skill and an MCP server
/// read as the same kind of thing — which is what they are to the person
/// installing them (web `EntryRow.tsx`).

/// The icon column shared by every row.
///
/// Icons are emoji rather than image assets: they need no upload path, no
/// serving route and no cache busting, and they survive the sandbox →
/// backend → app hop as plain text. A skill that declares none still needs to
/// be distinguishable at a glance, so it falls back to its initial over a
/// tint derived from the name — stable per skill, and never a blank square.
class EntryIcon extends StatelessWidget {
  const EntryIcon({super.key, required this.name, this.icon, this.small = false});

  final String name;
  final String? icon;
  final bool small;

  static (Color, Color) _tint(BossipTokens t, String seed) {
    final tints = <(Color, Color)>[
      (t.a200, t.n800),
      (t.s100, t.sage),
      (t.n200, t.n700),
      (t.dangerSoft, t.dangerInk),
      (t.s300, t.n800),
      (t.a100, t.n700),
    ];
    var hash = 0;
    for (final unit in seed.codeUnits) {
      hash = (hash * 31 + unit) & 0x7fffffff;
    }
    return tints[hash % tints.length];
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final box = small ? 32.0 : 40.0;
    final emoji = icon?.trim() ?? '';
    if (emoji.isNotEmpty) {
      return Container(
        width: box,
        height: box,
        alignment: Alignment.center,
        decoration: BoxDecoration(
          color: t.hairSoft,
          borderRadius: BorderRadius.circular(Radii.lg),
        ),
        child: Text(
          emoji,
          style: TextStyle(fontSize: small ? FontSizes.base : FontSizes.xl),
        ),
      );
    }
    final (background, foreground) = _tint(t, name);
    final initial =
        (name.trim().isEmpty ? '?' : name.trim()[0]).toUpperCase();
    return Container(
      width: box,
      height: box,
      alignment: Alignment.center,
      decoration: BoxDecoration(
        color: background,
        borderRadius: BorderRadius.circular(Radii.lg),
      ),
      child: Text(
        initial,
        style: TextStyle(
          fontSize: small ? FontSizes.base : FontSizes.xl,
          fontWeight: FontWeight.w500,
          color: foreground,
        ),
      ),
    );
  }
}

enum BadgeTone { muted, ok, warn }

class SkillBadge extends StatelessWidget {
  const SkillBadge({super.key, required this.text, this.tone = BadgeTone.muted});

  final String text;
  final BadgeTone tone;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final (background, foreground) = switch (tone) {
      BadgeTone.muted => (t.n200, t.n700),
      BadgeTone.ok => (t.s100, t.sage),
      BadgeTone.warn => (t.a200, t.n800),
    };
    return Container(
      constraints: const BoxConstraints(maxWidth: 150),
      padding: const EdgeInsets.symmetric(horizontal: 5, vertical: 1),
      decoration: BoxDecoration(
        color: background,
        borderRadius: BorderRadius.circular(Radii.sm),
      ),
      child: Text(
        text,
        maxLines: 1,
        overflow: TextOverflow.ellipsis,
        style: TextStyle(fontSize: FontSizes.xs2, color: foreground),
      ),
    );
  }
}

class IconAction extends StatelessWidget {
  const IconAction({
    super.key,
    required this.icon,
    required this.tooltip,
    required this.onTap,
    this.disabled = false,
    this.danger = false,
  });

  final IconData icon;
  final String tooltip;
  final VoidCallback onTap;
  final bool disabled;
  final bool danger;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return IconButton(
      onPressed: disabled ? null : onTap,
      icon: Icon(icon, size: 17),
      color: danger ? t.danger : t.n600,
      tooltip: tooltip,
      visualDensity: VisualDensity.compact,
      constraints: const BoxConstraints.tightFor(width: 34, height: 34),
      padding: EdgeInsets.zero,
    );
  }
}

class EntryRow extends StatelessWidget {
  const EntryRow({
    super.key,
    required this.name,
    this.icon,
    this.description,
    this.badges = const [],
    this.actions = const [],
    this.warning,
    this.onFixWarning,
    this.fixLabel,
    this.fixDisabled = false,
    this.onTap,
  });

  final String name;
  final String? icon;
  final String? description;
  final List<Widget> badges;
  final List<Widget> actions;
  final String? warning;

  /// Offered beside the warning so the gap is closed here, not elsewhere.
  final VoidCallback? onFixWarning;
  final String? fixLabel;
  final bool fixDisabled;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final body = Container(
      constraints: const BoxConstraints(minHeight: 60),
      padding: const EdgeInsets.fromLTRB(11, 9, 6, 9),
      decoration: BoxDecoration(
        color: t.hairSoft.withValues(alpha: 0.4),
        borderRadius: BorderRadius.circular(Radii.lg),
      ),
      child: Row(
        children: [
          EntryIcon(name: name, icon: icon),
          const SizedBox(width: 11),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              mainAxisSize: MainAxisSize.min,
              children: [
                Wrap(
                  spacing: 5,
                  runSpacing: 3,
                  crossAxisAlignment: WrapCrossAlignment.center,
                  children: [
                    ConstrainedBox(
                      constraints: const BoxConstraints(maxWidth: 210),
                      child: Text(
                        name,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: TextStyle(
                          fontSize: FontSizes.sm,
                          fontWeight: FontWeight.w500,
                          color: t.ink,
                        ),
                      ),
                    ),
                    ...badges,
                  ],
                ),
                if (description != null && description!.isNotEmpty)
                  Padding(
                    padding: const EdgeInsets.only(top: 2),
                    child: Text(
                      description!,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
                    ),
                  ),
                if (warning != null)
                  Padding(
                    padding: const EdgeInsets.only(top: 3),
                    child: Wrap(
                      spacing: 5,
                      runSpacing: 4,
                      crossAxisAlignment: WrapCrossAlignment.center,
                      children: [
                        Icon(Icons.warning_amber_outlined,
                            size: 12, color: t.sage),
                        ConstrainedBox(
                          constraints: const BoxConstraints(maxWidth: 230),
                          child: Text(
                            warning!,
                            style: TextStyle(
                              fontSize: FontSizes.xs,
                              height: 1.5,
                              color: t.sage,
                            ),
                          ),
                        ),
                        if (onFixWarning != null && fixLabel != null)
                          GestureDetector(
                            onTap: fixDisabled ? null : onFixWarning,
                            child: Opacity(
                              opacity: fixDisabled ? 0.4 : 1,
                              child: Container(
                                padding: const EdgeInsets.symmetric(
                                    horizontal: 8, vertical: 2),
                                decoration: BoxDecoration(
                                  color: t.ink,
                                  borderRadius:
                                      BorderRadius.circular(Radii.full),
                                ),
                                child: Text(
                                  fixLabel!,
                                  style: TextStyle(
                                      fontSize: FontSizes.xs2, color: t.bg),
                                ),
                              ),
                            ),
                          ),
                      ],
                    ),
                  ),
              ],
            ),
          ),
          if (actions.isNotEmpty)
            Row(mainAxisSize: MainAxisSize.min, children: actions),
        ],
      ),
    );
    if (onTap == null) return body;
    return GestureDetector(onTap: onTap, child: body);
  }
}
