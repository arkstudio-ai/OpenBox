import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../shared/appearance/tokens.dart';
import '../../shared/appearance/type_scale.dart';
import '../../shared/i18n/i18n.dart';
import '../../shared/models/first_video.dart';
import '../../shared/router/paths.dart';
import 'api/first_video_api.dart';
import 'state/first_video_flow.dart';
import 'widgets/flow_chrome.dart';

/// Template rows to show: the server's labels when it answered, the bundled
/// copy otherwise. Shared with the making page's subtitle.
List<FirstVideoOption> firstVideoTemplateOptions(
  I18nState i18n,
  FirstVideoCatalog? catalog,
) {
  final remote = catalog?.templates ?? const <FirstVideoOption>[];
  if (remote.isNotEmpty) return remote;
  return [
    for (final id in firstVideoTemplateIds)
      FirstVideoOption(
        id: id,
        label: i18n.t('first_video:template.fallback.$id.label'),
        description: i18n.t('first_video:template.fallback.$id.description'),
      ),
  ];
}

/// ② 选模板: three cards, one preselected, "就这个" moves on.
class FirstVideoTemplatePage extends ConsumerWidget {
  const FirstVideoTemplatePage({super.key});

  void _back(BuildContext context) =>
      context.canPop() ? context.pop() : context.go(Paths.firstVideoShop);

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final draft = ref.watch(firstVideoDraftProvider);
    final catalog = ref.watch(firstVideoCatalogProvider).valueOrNull;
    final options = firstVideoTemplateOptions(i18n, catalog);
    return FirstVideoScaffold(
      step: 2,
      leading: FirstVideoTopIcon(
        key: const Key('fv-back'),
        icon: Icons.arrow_back_ios_new,
        tooltip: i18n.t('first_video:back'),
        onTap: () => _back(context),
      ),
      footer: FirstVideoPrimaryButton(
        key: const Key('fv-confirm'),
        label: i18n.t('first_video:template.confirm'),
        onTap: () => context.push(Paths.firstVideoPhoto),
      ),
      body: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          FirstVideoHeading(
            title: i18n.t('first_video:template.title'),
            lede: i18n.t('first_video:template.lede'),
          ),
          const SizedBox(height: 20),
          for (final (index, option) in options.indexed) ...[
            if (index > 0) const SizedBox(height: 10),
            _TemplateCard(
              key: Key('fv-template-${option.id}'),
              option: option,
              shade: index,
              selected: draft.template == option.id,
              selectedLabel: i18n.t('first_video:template.selected'),
              onTap: () => ref
                  .read(firstVideoDraftProvider.notifier)
                  .setTemplate(option.id),
            ),
          ],
          const SizedBox(height: 16),
          Text(
            i18n.t('first_video:template.note'),
            style: TextStyle(fontSize: FontSizes.sm, color: t.n600),
          ),
          const SizedBox(height: 16),
        ],
      ),
    );
  }
}

class _TemplateCard extends StatelessWidget {
  const _TemplateCard({
    super.key,
    required this.option,
    required this.shade,
    required this.selected,
    required this.selectedLabel,
    required this.onTap,
  });

  final FirstVideoOption option;
  final int shade;
  final bool selected;
  final String selectedLabel;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Material(
      color: selected ? t.a100 : t.card,
      shape: RoundedRectangleBorder(
        side: BorderSide(
          color: selected ? t.a700 : t.hair,
          width: selected ? 2 : 1,
        ),
        borderRadius: BorderRadius.circular(Radii.xl),
      ),
      child: InkWell(
        borderRadius: BorderRadius.circular(Radii.xl),
        onTap: onTap,
        child: Padding(
          padding: const EdgeInsets.all(12),
          child: Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              // Sample stills slot in here once they exist
              // (`assets/first_video/<template>.jpg`); until then a tinted
              // portrait tile keeps the card's shape.
              Container(
                width: 76,
                height: 128,
                decoration: BoxDecoration(
                  color: [t.n900, t.n800, t.n700][shade % 3],
                  borderRadius: BorderRadius.circular(Radii.md),
                ),
                child: const Icon(
                  Icons.play_arrow_rounded,
                  color: Colors.white,
                  size: 28,
                ),
              ),
              const SizedBox(width: 14),
              Expanded(
                child: SizedBox(
                  height: 128,
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    mainAxisAlignment: MainAxisAlignment.spaceBetween,
                    children: [
                      Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            option.label,
                            style: TextStyle(
                              fontSize: FontSizes.xl,
                              fontWeight: FontWeight.w700,
                              color: t.ink,
                            ),
                          ),
                          const SizedBox(height: 4),
                          Text(
                            option.description,
                            style: TextStyle(
                              fontSize: FontSizes.md,
                              height: 1.4,
                              color: t.n700,
                            ),
                          ),
                        ],
                      ),
                      if (selected)
                        Row(
                          children: [
                            Icon(Icons.check, size: 14, color: t.a800),
                            const SizedBox(width: 6),
                            Text(
                              selectedLabel,
                              style: TextStyle(
                                fontSize: FontSizes.sm,
                                fontWeight: FontWeight.w600,
                                color: t.a800,
                              ),
                            ),
                          ],
                        ),
                    ],
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
