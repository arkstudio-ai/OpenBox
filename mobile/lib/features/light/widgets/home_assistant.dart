import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/json.dart';
import '../../../shared/router/paths.dart';
import '../../chat/api/assistant_api.dart';
import '../../chat/state/assistant_overview.dart';
import '../../chat/widgets/composer/composer.dart';
import '../production/production_api.dart';
import '../utils/activity_date.dart';
import 'home_header.dart';

class LightHomeAssistant extends ConsumerWidget {
  const LightHomeAssistant({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final scope = ref.watch(assistantScopeProvider);
    final overview = scope == null
        ? null
        : ref.watch(assistantOverviewProvider(scope)).valueOrNull;
    final finished = [
      for (final task in overview?.tasks ?? const <AssistantTask>[])
        if (task.id.isNotEmpty &&
            task.executionId.isNotEmpty &&
            task.title.isNotEmpty &&
            !task.archived &&
            task.result['outcome'] == 'succeeded' &&
            ProductionKind.fromId(task.lightEntry) == null)
          if (asDate(task.result['created_at']) ?? task.updatedAt
              case final completedAt?)
            (task, completedAt),
    ]..sort((a, b) => b.$2.compareTo(a.$2));

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        LightHomeHeading(i18n.t('light:home.assistant.title')),
        const SizedBox(height: 12),
        Container(
          key: const Key('light-home-assistant'),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(Radii.xl),
            border: Border.all(color: t.hair),
            color: t.hairSoft,
          ),
          padding: const EdgeInsets.all(14),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              LayoutBuilder(
                builder: (context, constraints) {
                  final headline = Text(
                    i18n.t('light:home.assistant.slogan'),
                    style: TextStyle(
                      fontSize: FontSizes.xl2,
                      height: 1.2,
                      fontWeight: FontWeight.w800,
                      color: t.ink,
                    ),
                  );
                  final button = FilledButton(
                    key: const Key('light-ask'),
                    style: FilledButton.styleFrom(
                      backgroundColor: t.ink,
                      foregroundColor: t.bg,
                      minimumSize: const Size(0, 44),
                      padding: const EdgeInsets.symmetric(horizontal: 13),
                      textStyle: const TextStyle(
                        fontSize: FontSizes.sm,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                    onPressed: () => context.go(Paths.lightAssistant),
                    child: Text(i18n.t('light:home.assistant.open')),
                  );
                  if (constraints.maxWidth < 295 ||
                      MediaQuery.textScalerOf(context).scale(14) > 17) {
                    return Wrap(
                      spacing: 10,
                      runSpacing: 8,
                      crossAxisAlignment: WrapCrossAlignment.center,
                      children: [headline, button],
                    );
                  }
                  return Row(
                    children: [
                      Expanded(child: headline),
                      const SizedBox(width: 10),
                      button,
                    ],
                  );
                },
              ),
              const SizedBox(height: 9),
              Text(
                i18n.t('light:home.assistant.suggestions'),
                style: TextStyle(fontSize: FontSizes.sm, color: t.n700),
              ),
              Wrap(
                spacing: 8,
                children: [
                  for (final (id, icon) in const [
                    ('reviews', Icons.chat_bubble_outline_rounded),
                    ('copy', Icons.auto_awesome_outlined),
                  ])
                    ActionChip(
                      key: Key('light-assistant-prompt-$id'),
                      avatar: Icon(icon, size: 17, color: t.ink),
                      backgroundColor: t.card,
                      side: BorderSide(color: t.hair),
                      shape: const StadiumBorder(),
                      label: Text(i18n.t('light:home.assistant.$id.label')),
                      onPressed: () => context.go(
                        Paths.lightAssistant,
                        extra: ComposerDraft(
                          i18n.t('light:home.assistant.$id.prompt'),
                          DateTime.now().microsecondsSinceEpoch,
                        ),
                      ),
                    ),
                ],
              ),
              const SizedBox(height: 10),
              Material(
                color: t.card,
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(Radii.lg),
                  side: BorderSide(color: t.hair),
                ),
                clipBehavior: Clip.antiAlias,
                child: ListTile(
                  key: const Key('light-home-schedules'),
                  contentPadding: const EdgeInsets.symmetric(horizontal: 14),
                  leading: Icon(Icons.schedule, color: t.ink),
                  title: Text(
                    i18n.t('light:home.assistant.schedules'),
                    style: TextStyle(color: t.ink, fontWeight: FontWeight.w700),
                  ),
                  subtitle: Text(
                    i18n.t('light:home.assistant.scheduleHint'),
                    style: TextStyle(fontSize: FontSizes.sm, color: t.n700),
                  ),
                  trailing: Icon(Icons.chevron_right, size: 19, color: t.n700),
                  onTap: () => context.push(Paths.lightAutopilot),
                ),
              ),
              if (finished.isNotEmpty) ...[
                const SizedBox(height: 15),
                Text(
                  i18n.t('light:home.assistant.recent'),
                  key: const Key('light-assistant-recent'),
                  style: TextStyle(fontSize: FontSizes.sm, color: t.n700),
                ),
                const SizedBox(height: 5),
                Material(
                  color: t.card,
                  shape: RoundedRectangleBorder(
                    borderRadius: BorderRadius.circular(Radii.lg),
                    side: BorderSide(color: t.hair),
                  ),
                  clipBehavior: Clip.antiAlias,
                  child: Column(
                    children: [
                      for (final (index, (task, date))
                          in finished.take(2).indexed) ...[
                        if (index > 0)
                          Divider(
                            height: 1,
                            indent: 12,
                            endIndent: 12,
                            color: t.hair,
                          ),
                        ListTile(
                          key: Key('light-assistant-recent-${task.id}'),
                          minTileHeight: 44,
                          contentPadding: const EdgeInsets.symmetric(
                            horizontal: 12,
                          ),
                          title: Text(
                            task.title,
                            maxLines: 2,
                            overflow: TextOverflow.ellipsis,
                            style: TextStyle(
                              fontSize: FontSizes.sm,
                              color: t.ink,
                            ),
                          ),
                          trailing: Text(
                            lightActivityDate(date, i18n),
                            style: TextStyle(
                              fontSize: FontSizes.xs,
                              color: t.n700,
                            ),
                          ),
                          onTap: () =>
                              context.push(Paths.chat(task.executionId)),
                        ),
                      ],
                    ],
                  ),
                ),
              ],
            ],
          ),
        ),
      ],
    );
  }
}
