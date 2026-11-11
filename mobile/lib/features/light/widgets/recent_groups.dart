import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/light.dart';
import '../../../shared/router/paths.dart';
import '../../../shared/utils/format.dart';
import '../state/light_providers.dart';

/// 「最近做的」 (docs `LIGHT_MODE_PLAN.md` §4.7 #15): one row per
/// conversation, its images and videos mixed, opening the conversation.
class LightRecentSection extends ConsumerWidget {
  const LightRecentSection({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final recent = ref.watch(lightRecentProvider);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          i18n.t('light:home.section.recent'),
          style: TextStyle(
            fontSize: FontSizes.sm,
            fontWeight: FontWeight.w600,
            color: t.n700,
          ),
        ),
        const SizedBox(height: 10),
        recent.when(
          loading: () => const Padding(
            padding: EdgeInsets.symmetric(vertical: 20),
            child: Center(
              child: SizedBox(
                width: 18,
                height: 18,
                child: CircularProgressIndicator(strokeWidth: 2),
              ),
            ),
          ),
          error: (_, _) => _Note(
            key: const Key('light-recent-failed'),
            text: i18n.t('light:home.recentFailed'),
            action: i18n.t('common:action.retry'),
            onAction: () => ref.invalidate(lightRecentProvider),
          ),
          data: (groups) => groups.isEmpty
              ? _Note(
                  key: const Key('light-recent-empty'),
                  text: i18n.t('light:home.recentEmpty'),
                )
              : Column(
                  children: [
                    for (final group in groups)
                      Padding(
                        padding: const EdgeInsets.only(bottom: 8),
                        child: _GroupTile(group: group),
                      ),
                  ],
                ),
        ),
      ],
    );
  }
}

class _GroupTile extends ConsumerWidget {
  const _GroupTile({required this.group});

  final LightRecentGroup group;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final entry = ref.watch(lightEntryProvider(group.entryId ?? ''));
    final title = group.title.trim().isNotEmpty
        ? group.title
        : entry?.title ?? i18n.t('light:conversations.untitled');
    final when = group.updatedAt == null
        ? null
        : formatSince(group.updatedAt!, i18n.language);
    return Material(
      color: t.card,
      borderRadius: BorderRadius.circular(Radii.lg),
      child: InkWell(
        borderRadius: BorderRadius.circular(Radii.lg),
        onTap: () => context.push(
          group.entryId != null
              ? Paths.lightChat(group.sessionId)
              : Paths.chat(group.sessionId),
        ),
        child: Container(
          padding: const EdgeInsets.all(10),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(Radii.lg),
            border: Border.all(color: t.hair),
          ),
          child: Row(
            children: [
              for (final output in group.outputs.take(3)) ...[
                _OutputThumb(output: output),
                const SizedBox(width: 6),
              ],
              const SizedBox(width: 6),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      title,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(
                        fontSize: FontSizes.base,
                        fontWeight: FontWeight.w600,
                        color: t.ink,
                      ),
                    ),
                    const SizedBox(height: 2),
                    Text(
                      [
                        if (entry != null) entry.title,
                        i18n.t(
                          'light:home.outputs',
                          count: group.outputs.length,
                        ),
                        ?when,
                      ].join(' · '),
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
                    ),
                  ],
                ),
              ),
              Icon(Icons.chevron_right, size: 18, color: t.n500),
            ],
          ),
        ),
      ),
    );
  }
}

/// A result's tile: the image itself when the link is in, a dark play tile
/// for a video (the first frame would need the player stack).
class _OutputThumb extends ConsumerWidget {
  const _OutputThumb({required this.output});

  final LightRecentOutput output;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final url = output.isImage
        ? ref.watch(lightAssetUrlProvider(output.assetId)).valueOrNull
        : null;
    return ClipRRect(
      borderRadius: BorderRadius.circular(Radii.md),
      child: SizedBox(
        width: 52,
        height: 52,
        child: url != null
            ? Image.network(
                url,
                fit: BoxFit.cover,
                errorBuilder: (_, _, _) => _placeholder(t),
              )
            : _placeholder(t),
      ),
    );
  }

  Widget _placeholder(BossipTokens t) => ColoredBox(
    color: output.isVideo ? t.n900 : t.n300,
    child: Icon(
      output.isVideo ? Icons.play_arrow_rounded : Icons.image_outlined,
      size: 22,
      color: output.isVideo ? t.n100 : t.n600,
    ),
  );
}

class _Note extends StatelessWidget {
  const _Note({super.key, required this.text, this.action, this.onAction});

  final String text;
  final String? action;
  final VoidCallback? onAction;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 18),
      decoration: BoxDecoration(
        color: t.hairSoft,
        borderRadius: BorderRadius.circular(Radii.lg),
      ),
      child: Column(
        children: [
          Text(
            text,
            textAlign: TextAlign.center,
            style: TextStyle(fontSize: FontSizes.sm, color: t.n600),
          ),
          if (action != null)
            TextButton(onPressed: onAction, child: Text(action!)),
        ],
      ),
    );
  }
}
