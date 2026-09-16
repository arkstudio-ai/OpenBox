import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/json.dart';
import '../api/settings_api.dart';

/// 视频发布 (web `PublishPage`): which way finished videos go to Douyin —
/// the creator centre on the cloud desktop (`desktop`) or the Open Platform
/// QR posting package (`api`). Nothing chosen follows the deployment default.
class PublishSection extends ConsumerWidget {
  const PublishSection({super.key});

  static const _routes = ['desktop', 'api'];

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final status = ref.watch(publishRouteProvider).valueOrNull ?? const {};
    final deploymentDefault =
        asString(status['deploymentDefault']) ?? 'desktop';
    final preference = asString(status['preference']);
    final effective = asString(status['effective']) ?? deploymentDefault;

    return ListView(
      padding: const EdgeInsets.all(16),
      children: [
        for (final route in _routes) ...[
          _RouteCard(
            key: ValueKey('publish-route-$route'),
            title: i18n.t('settings:publish.route.$route'),
            description: i18n.t('settings:publish.desc.$route'),
            defaultTag: route == deploymentDefault
                ? i18n.t('settings:publish.defaultTag')
                : null,
            active: effective == route,
            onTap: () => _update(ref, route),
          ),
          const SizedBox(height: 10),
        ],
        Wrap(
          crossAxisAlignment: WrapCrossAlignment.center,
          spacing: 12,
          runSpacing: 4,
          children: [
            Text(
              preference != null
                  ? i18n.t(
                      'settings:publish.chosen',
                      vars: {
                        'route': i18n.t('settings:publish.route.$preference'),
                      },
                    )
                  : i18n.t(
                      'settings:publish.followingDefault',
                      vars: {
                        'route': i18n.t(
                          'settings:publish.route.$deploymentDefault',
                        ),
                      },
                    ),
              style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
            ),
            if (preference != null)
              TextButton(
                key: const ValueKey('publish-route-reset'),
                onPressed: () => _update(ref, null),
                child: Text(i18n.t('settings:publish.resetToDefault')),
              ),
          ],
        ),
        const SizedBox(height: 8),
        Text(
          i18n.t('settings:publish.note'),
          style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
        ),
      ],
    );
  }

  Future<void> _update(WidgetRef ref, String? route) async {
    await ref.read(settingsApiProvider).setPublishRoute(route);
    ref.invalidate(publishRouteProvider);
  }
}

class _RouteCard extends StatelessWidget {
  const _RouteCard({
    super.key,
    required this.title,
    required this.description,
    required this.defaultTag,
    required this.active,
    required this.onTap,
  });

  final String title;
  final String description;
  final String? defaultTag;
  final bool active;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Material(
      color: t.card,
      borderRadius: BorderRadius.circular(Radii.xl),
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(Radii.xl),
        child: Container(
          width: double.infinity,
          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 14),
          decoration: BoxDecoration(
            borderRadius: BorderRadius.circular(Radii.xl),
            border: Border.all(color: active ? t.ink : t.hair),
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Text(
                    title,
                    style: TextStyle(fontSize: FontSizes.base, color: t.ink),
                  ),
                  if (defaultTag != null) ...[
                    const SizedBox(width: 8),
                    Container(
                      padding: const EdgeInsets.symmetric(
                        horizontal: 8,
                        vertical: 2,
                      ),
                      decoration: BoxDecoration(
                        color: t.n200,
                        borderRadius: BorderRadius.circular(999),
                      ),
                      child: Text(
                        defaultTag!,
                        style: TextStyle(fontSize: FontSizes.xs, color: t.n700),
                      ),
                    ),
                  ],
                ],
              ),
              const SizedBox(height: 4),
              Text(
                description,
                style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
