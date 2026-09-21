import 'package:flutter/material.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/models/json.dart';

/// The small shared pieces every team surface is built from. Kept together so
/// the progress card in the conversation and the run screen render the same
/// state with the same marks, rather than each inventing its own.

/// Icon names an Agent definition may carry (web `AgentAvatar`'s ICONS).
const _icons = <String, IconData>{
  'bot': Icons.smart_toy_outlined,
  'search': Icons.search,
  'file': Icons.description_outlined,
  'filetext': Icons.description_outlined,
  'shield': Icons.verified_user_outlined,
  'shieldcheck': Icons.verified_user_outlined,
  'calculator': Icons.calculate_outlined,
  'pen': Icons.edit_outlined,
  'code': Icons.code,
  'image': Icons.image_outlined,
  'mic': Icons.mic_none,
  'bookopen': Icons.menu_book_outlined,
  'clipboardcheck': Icons.assignment_turned_in_outlined,
  'circlecheck': Icons.check_circle_outline,
  'checkcircle': Icons.check_circle_outline,
};

/// Agent avatar (web `AgentAvatar`): the definition's icon on its colour, with
/// the execution-state dot the rest of the app uses for live state.
class TeamAvatar extends StatelessWidget {
  const TeamAvatar({
    super.key,
    this.display,
    this.dotColor,
    this.size = 34,
    this.circular = false,
  });

  final Map<String, dynamic>? display;

  /// Null draws no dot — a lineup being proposed has no execution state yet.
  final Color? dotColor;
  final double size;

  /// Overlapping avatars read as separate faces only when they are round
  /// (web uses `rounded-full` for the roster stack).
  final bool circular;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final name = (asString(display?['icon']) ?? 'bot')
        .replaceAll(RegExp('[-_]'), '')
        .toLowerCase();
    final (background, foreground) = switch (asString(display?['color'])) {
      'green' || 'sage' => (t.s100, t.sage),
      'red' => (t.dangerSoft, t.dangerInk),
      'gray' => (t.n200, t.n700),
      'blue' || 'amber' || 'gold' => (t.a100, t.a700),
      _ => (t.hairSoft, t.n700),
    };
    return SizedBox(
      width: size,
      height: size,
      child: Stack(
        clipBehavior: Clip.none,
        children: [
          Container(
            width: size,
            height: size,
            decoration: BoxDecoration(
              color: background,
              borderRadius: BorderRadius.circular(
                circular ? size / 2 : Radii.md,
              ),
              border: Border.all(color: t.hair),
            ),
            child: Icon(
              _icons[name] ?? Icons.smart_toy_outlined,
              size: size * 0.5,
              color: foreground,
            ),
          ),
          if (dotColor != null)
            PositionedDirectional(
              end: -1,
              bottom: -1,
              child: Container(
                width: 8,
                height: 8,
                decoration: BoxDecoration(
                  color: dotColor,
                  shape: BoxShape.circle,
                  border: Border.all(color: t.card, width: 1.5),
                ),
              ),
            ),
        ],
      ),
    );
  }
}

/// The global meaning of the 6px state dot (§13.3): running accent, done
/// sage, failed danger, idle n400.
Color teamStateColor(BossipTokens t, String state) => switch (state) {
  'running' ||
  'waiting' ||
  'provisioning' ||
  'pausing' ||
  'completing' => t.accent,
  'succeeded' || 'completed' || 'delivered' || 'recorded' => t.sage,
  'failed' || 'blocked' || 'outcome_unknown' => t.danger,
  _ => t.n400,
};

/// A member's state is its membership state unless it is still in the team,
/// in which case what it is doing right now is the useful one.
String teamMemberState(Map<String, dynamic> member) =>
    asString(member['membership_state']) == 'active'
    ? asString(member['execution_state']) ?? 'idle'
    : asString(member['membership_state']) ?? 'idle';

/// A task owned by a queued member is itself queued, whatever the task row
/// says (web `taskDisplayState`).
String teamTaskState(
  Map<String, dynamic> task,
  List<Map<String, dynamic>> members,
) {
  final state = asString(task['state']) ?? '';
  if (state != 'running') return state;
  final owner = members
      .where((member) => member['id'] == task['owner_member_id'])
      .firstOrNull;
  return asString(owner?['execution_state']) == 'queued' ? 'queued' : 'running';
}

/// Run state as a quiet chip (web: the `bg-hairsoft` rounded-full span).
class TeamStatePill extends StatelessWidget {
  const TeamStatePill({super.key, required this.label});

  final String label;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
      decoration: BoxDecoration(
        color: t.hairSoft,
        borderRadius: BorderRadius.circular(Radii.full),
      ),
      child: ConstrainedBox(
        constraints: const BoxConstraints(maxWidth: 96),
        child: Text(
          label,
          maxLines: 1,
          overflow: TextOverflow.ellipsis,
          style: TextStyle(fontSize: FontSizes.xs, color: t.n700),
        ),
      ),
    );
  }
}

/// Task status circle — the same mark the todo card uses, so a team task and
/// an ordinary task read alike.
class TeamStatusMark extends StatelessWidget {
  const TeamStatusMark({super.key, required this.state});

  final String state;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final done = state == 'succeeded';
    final failed = state == 'failed' || state == 'blocked';
    final running = state == 'running';
    return Container(
      width: 18,
      height: 18,
      decoration: BoxDecoration(
        shape: BoxShape.circle,
        color: done ? t.s600 : null,
        border: done
            ? null
            : Border.all(
                color: failed
                    ? t.danger
                    : running
                    ? t.accent
                    : t.n400,
                width: 2.5,
              ),
      ),
      child: done ? Icon(Icons.check, size: 12, color: t.bg) : null,
    );
  }
}

/// The round tick the todo card and the Skill selector use, so a selection
/// reads the same way everywhere in the app.
class TeamCheckMark extends StatelessWidget {
  const TeamCheckMark({super.key, required this.value});

  final bool value;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Container(
      width: 18,
      height: 18,
      decoration: BoxDecoration(
        shape: BoxShape.circle,
        color: value ? t.s600 : null,
        border: value ? null : Border.all(color: t.n400, width: 2),
      ),
      child: value ? Icon(Icons.check, size: 12, color: t.bg) : null,
    );
  }
}

/// Inline text action, matching the "停止" link on the todo card. Team
/// controls are links inside a card, not buttons with their own chrome.
class TeamActionLink extends StatelessWidget {
  const TeamActionLink({
    super.key,
    required this.label,
    required this.onTap,
    this.muted = false,
  });

  final String label;
  final VoidCallback? onTap;
  final bool muted;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Opacity(
      opacity: onTap == null ? 0.4 : 1,
      child: GestureDetector(
        onTap: onTap,
        child: Padding(
          padding: const EdgeInsets.symmetric(vertical: 4),
          child: Text(
            label,
            style: TextStyle(
              fontSize: FontSizes.sm,
              color: muted ? t.n600 : t.a700,
            ),
          ),
        ),
      ),
    );
  }
}

/// A labelled line of secondary detail inside a team card.
class TeamMetaLine extends StatelessWidget {
  const TeamMetaLine({
    super.key,
    required this.label,
    required this.value,
    this.selectable = false,
  });

  final String label, value;
  final bool selectable;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    if (value.trim().isEmpty) return const SizedBox.shrink();
    final style = TextStyle(fontSize: FontSizes.xs, color: t.n600, height: 1.6);
    return Padding(
      padding: const EdgeInsets.only(top: 4),
      child: selectable
          ? SelectableText('$label: $value', style: style)
          : Text('$label: $value', style: style),
    );
  }
}
