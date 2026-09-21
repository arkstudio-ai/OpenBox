import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/json.dart';
import '../../../shared/models/team.dart';
import 'team_bits.dart';

/// The roster link graph (web `TeamRosterGraph`), ported to the phone.
///
/// It answers two questions and nothing else: who is on the team, and who is
/// dealing with whom. It is not a canvas — no dragging, no zoom, no editing.
/// The layout is fixed (coordinator above, members in rows of up to three),
/// so there is no layout algorithm and no graph library: one painter draws
/// the lines, the nodes are ordinary buttons on top of it.
class TeamRosterGraph extends ConsumerWidget {
  const TeamRosterGraph({
    super.key,
    required this.snapshot,
    required this.selected,
    required this.onSelect,
    required this.onMessages,
  });

  final TeamSnapshot snapshot;
  final String? selected;
  final ValueChanged<String> onSelect;

  /// A pair of member ids, comma separated, for the messages filter.
  final ValueChanged<String> onMessages;

  static const _nodeHeight = 94.0;
  static const _rowGap = 74.0;
  static const _gap = 8.0;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final coordinator = snapshot.members
        .where((member) => asString(member['role']) == 'coordinator')
        .firstOrNull;
    final workers = snapshot.members
        .where((member) => asString(member['role']) != 'coordinator')
        .toList();

    return LayoutBuilder(
      builder: (context, constraints) {
        final width = constraints.maxWidth;
        final columns = workers.isEmpty
            ? 1
            : (workers.length < 3 ? workers.length : 3);
        final nodeWidth = ((width - (columns - 1) * _gap) / columns).clamp(
          92.0,
          150.0,
        );
        final rows = workers.isEmpty
            ? 0
            : (workers.length + columns - 1) ~/ columns;
        final height =
            _nodeHeight +
            (rows == 0
                ? 0
                : _rowGap + rows * _nodeHeight + (rows - 1) * _rowGap);

        // Centre of each node, and the same point for the coordinator under
        // the id the links use (they name the root session, not the member).
        final centres = <String, Offset>{};
        void place(Map<String, dynamic>? member, Offset centre) {
          final id = asString(member?['id']);
          if (id != null) centres[id] = centre;
        }

        final coordinatorCentre = Offset(width / 2, _nodeHeight / 2);
        place(coordinator, coordinatorCentre);
        centres[snapshot.run.rootSessionId] = coordinatorCentre;
        for (final (index, worker) in workers.indexed) {
          final row = index ~/ columns;
          final column = index % columns;
          final inRow = (workers.length - row * columns).clamp(1, columns);
          final rowWidth = inRow * nodeWidth + (inRow - 1) * _gap;
          place(
            worker,
            Offset(
              (width - rowWidth) / 2 +
                  column * (nodeWidth + _gap) +
                  nodeWidth / 2,
              _nodeHeight +
                  _rowGap +
                  row * (_nodeHeight + _rowGap) +
                  _nodeHeight / 2,
            ),
          );
        }

        final edges = [
          for (final link in snapshot.links)
            if (centres[link['from']] != null && centres[link['to']] != null)
              () {
                final start = centres[link['from']]!;
                final end = centres[link['to']]!;
                final message = link['kind'] == 'message';
                // Two members in the same row sit side by side, so a straight
                // line between them — and the number on it — would hide behind
                // their cards. Those curves dip below the row instead. A pair
                // in different rows only needs a sideways bow, to keep a
                // message line off the delegation line beside it.
                final sameRow = (start.dy - end.dy).abs() < 1;
                return _Edge(
                  from: '${link['from']}',
                  to: '${link['to']}',
                  message: message,
                  count: asInt(link['count']) ?? 0,
                  start: start,
                  end: end,
                  bow: !message
                      ? Offset.zero
                      : sameRow
                      ? const Offset(0, _nodeHeight + 34)
                      : Offset(width * 0.26, 0),
                );
              }(),
        ];
        // A dipping curve needs somewhere to dip into.
        final overflow = edges
            .map((edge) => edge.label.dy - height + 16)
            .fold(0.0, (most, value) => value > most ? value : most);

        Widget node(Map<String, dynamic> member) {
          final centre = centres[asString(member['id'])];
          if (centre == null) return const SizedBox.shrink();
          return Positioned(
            left: centre.dx - nodeWidth / 2,
            top: centre.dy - _nodeHeight / 2,
            width: nodeWidth,
            height: _nodeHeight,
            child: _Node(
              member: member,
              selected: asString(member['id']) == selected,
              onTap: () => onSelect(asString(member['id']) ?? ''),
            ),
          );
        }

        return Semantics(
          label: i18n.t('teams:rosterGraphHint'),
          child: SizedBox(
            height: height + overflow,
            child: Stack(
              clipBehavior: Clip.none,
              children: [
                Positioned.fill(
                  child: CustomPaint(
                    painter: _EdgePainter(
                      edges: edges,
                      selected: selected,
                      line: t.n400,
                      highlight: t.accent,
                    ),
                  ),
                ),
                for (final edge in edges)
                  Positioned(
                    left: edge.label.dx - 26,
                    top: edge.label.dy - 11,
                    width: 52,
                    height: 22,
                    child: _CountChip(
                      edge: edge,
                      active: edge.touches(selected),
                      onTap: () => onMessages('${edge.from},${edge.to}'),
                    ),
                  ),
                if (coordinator != null) node(coordinator),
                for (final worker in workers) node(worker),
              ],
            ),
          ),
        );
      },
    );
  }
}

class _Edge {
  const _Edge({
    required this.from,
    required this.to,
    required this.message,
    required this.count,
    required this.start,
    required this.end,
    required this.bow,
  });

  final String from, to;
  final bool message;
  final int count;
  final Offset start, end;

  /// How far the curve's control point is pushed off the straight midpoint.
  final Offset bow;

  Offset get control => (start + end) / 2 + bow;

  /// Where the curve actually passes at t=0.5 — the chip sits on the line,
  /// not on the straight midpoint.
  Offset get label => (start + control * 2 + end) / 4;

  bool touches(String? id) => id != null && (from == id || to == id);
}

class _EdgePainter extends CustomPainter {
  const _EdgePainter({
    required this.edges,
    required this.selected,
    required this.line,
    required this.highlight,
  });

  final List<_Edge> edges;
  final String? selected;
  final Color line, highlight;

  @override
  void paint(Canvas canvas, Size size) {
    for (final edge in edges) {
      final paint = Paint()
        ..style = PaintingStyle.stroke
        ..strokeWidth = 1.5
        ..color = edge.touches(selected) ? highlight : line;
      final path = Path()
        ..moveTo(edge.start.dx, edge.start.dy)
        ..quadraticBezierTo(
          edge.control.dx,
          edge.control.dy,
          edge.end.dx,
          edge.end.dy,
        );
      // Solid means the coordinator delegated work; dashed means two members
      // exchanged messages (§13.3).
      canvas.drawPath(edge.message ? _dashed(path) : path, paint);
    }
  }

  static Path _dashed(Path source) {
    final dashed = Path();
    for (final metric in source.computeMetrics()) {
      var distance = 0.0;
      while (distance < metric.length) {
        final next = (distance + 4).clamp(0.0, metric.length);
        dashed.addPath(metric.extractPath(distance, next), Offset.zero);
        distance = next + 4;
      }
    }
    return dashed;
  }

  @override
  bool shouldRepaint(_EdgePainter old) =>
      old.selected != selected ||
      old.edges.length != edges.length ||
      old.line != line;
}

/// One member on the graph: avatar, name, and what it is doing.
class _Node extends ConsumerWidget {
  const _Node({
    required this.member,
    required this.selected,
    required this.onTap,
  });

  final Map<String, dynamic> member;
  final bool selected;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final state = teamMemberState(member);
    final retired = asString(member['membership_state']) == 'retired';
    return Opacity(
      opacity: retired ? 0.6 : 1,
      child: Semantics(
        selected: selected,
        button: true,
        child: Material(
          color: t.card,
          borderRadius: BorderRadius.circular(Radii.lg),
          child: InkWell(
            borderRadius: BorderRadius.circular(Radii.lg),
            onTap: onTap,
            child: Container(
              padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 8),
              decoration: BoxDecoration(
                borderRadius: BorderRadius.circular(Radii.lg),
                border: Border.all(
                  color: selected ? t.accent : t.hair,
                  width: selected ? 2 : 1,
                ),
              ),
              // Scale down rather than overflow: the two lines below the
              // avatar grow with the reader's type size.
              child: FittedBox(
                fit: BoxFit.scaleDown,
                child: Column(
                  mainAxisAlignment: MainAxisAlignment.center,
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    TeamAvatar(
                      display: asMap(member['display']),
                      dotColor: teamStateColor(t, state),
                      size: 30,
                      circular: true,
                    ),
                    const SizedBox(height: 5),
                    Text(
                      asString(member['name']) ??
                          asString(member['alias']) ??
                          '',
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      textAlign: TextAlign.center,
                      style: TextStyle(
                        fontSize: FontSizes.xs,
                        fontWeight: FontWeight.w500,
                        color: t.ink,
                      ),
                    ),
                    Text(
                      i18n.t('teams:state.${retired ? 'retired' : state}'),
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(fontSize: FontSizes.xs2, color: t.n600),
                    ),
                  ],
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}

/// The number on a line. Tapping it opens the messages between that pair.
class _CountChip extends ConsumerWidget {
  const _CountChip({
    required this.edge,
    required this.active,
    required this.onTap,
  });

  final _Edge edge;
  final bool active;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return Center(
      child: Semantics(
        label: i18n.t(
          edge.message ? 'teams:link.message' : 'teams:link.task',
          count: edge.count,
        ),
        button: true,
        child: Material(
          color: t.card,
          borderRadius: BorderRadius.circular(Radii.full),
          child: InkWell(
            borderRadius: BorderRadius.circular(Radii.full),
            onTap: onTap,
            child: Container(
              padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
              decoration: BoxDecoration(
                borderRadius: BorderRadius.circular(Radii.full),
                border: Border.all(color: active ? t.accent : t.hair),
              ),
              // An icon, not an arrow character: iOS renders a bare ↔ as a
              // colour emoji, which reads as a badge rather than a line label.
              child: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Icon(
                    edge.message ? Icons.swap_horiz : Icons.south,
                    size: 12,
                    color: active ? t.a700 : t.n600,
                  ),
                  const SizedBox(width: 3),
                  Text(
                    '${edge.count}',
                    style: TextStyle(
                      fontSize: FontSizes.xs2,
                      color: active ? t.a700 : t.n700,
                    ),
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}
