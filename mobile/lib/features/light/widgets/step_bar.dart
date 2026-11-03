import 'package:flutter/material.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/models/light.dart';

/// The step bar over a topic chat (docs `LIGHT_MODE_PLAN.md` §4.4): the
/// entry's `steps[]`, lit from what the agent's cards say, never from a
/// guess. [activeIndex] is the step the latest card asked about; steps
/// before it are done. [workingIndex] is a step the agent is busy on after
/// the last answer, drawn as in progress. -1 means none.
class LightStepBar extends StatelessWidget {
  const LightStepBar({
    super.key,
    required this.steps,
    required this.activeIndex,
    this.workingIndex = -1,
  });

  final List<LightStep> steps;
  final int activeIndex;
  final int workingIndex;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return SingleChildScrollView(
      key: const Key('light-step-bar'),
      scrollDirection: Axis.horizontal,
      padding: const EdgeInsets.fromLTRB(16, 0, 16, 10),
      child: Row(
        children: [
          for (final (index, step) in steps.indexed) ...[
            if (index > 0) Container(width: 12, height: 1, color: t.hair),
            _Pill(
              key: Key('light-step-${step.id}'),
              number: index + 1,
              label: step.label,
              state: index < activeIndex
                  ? _PillState.done
                  : index == activeIndex
                  ? _PillState.current
                  : index == workingIndex
                  ? _PillState.working
                  : _PillState.todo,
            ),
          ],
        ],
      ),
    );
  }
}

enum _PillState { done, current, working, todo }

class _Pill extends StatelessWidget {
  const _Pill({
    super.key,
    required this.number,
    required this.label,
    required this.state,
  });

  final int number;
  final String label;
  final _PillState state;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final (bg, fg, border) = switch (state) {
      _PillState.done => (t.a700, t.bg, t.a700),
      _PillState.current => (t.a700, t.bg, t.a700),
      _PillState.working => (t.a200, t.a800, t.a300),
      _PillState.todo => (t.hairSoft, t.n600, t.hair),
    };
    return Semantics(
      label: label,
      selected: state == _PillState.current,
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 5),
        decoration: BoxDecoration(
          color: bg,
          borderRadius: BorderRadius.circular(Radii.full),
          border: Border.all(color: border),
        ),
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            if (state == _PillState.done)
              Icon(Icons.check, size: 12, color: fg)
            else if (state == _PillState.working)
              SizedBox(
                width: 10,
                height: 10,
                child: CircularProgressIndicator(strokeWidth: 1.6, color: fg),
              )
            else
              Text(
                '$number',
                style: TextStyle(
                  fontSize: FontSizes.xs,
                  fontWeight: FontWeight.w700,
                  color: fg,
                ),
              ),
            const SizedBox(width: 5),
            Text(
              label,
              style: TextStyle(
                fontSize: FontSizes.xs,
                fontWeight: state == _PillState.current
                    ? FontWeight.w700
                    : FontWeight.w500,
                color: fg,
              ),
            ),
          ],
        ),
      ),
    );
  }
}
