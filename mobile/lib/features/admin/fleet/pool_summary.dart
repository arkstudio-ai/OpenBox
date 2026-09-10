import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../models/admin_data.dart';
import '../widgets/admin_widgets.dart';

class AdminPoolSummary extends ConsumerWidget {
  const AdminPoolSummary({
    super.key,
    required this.pool,
    required this.busy,
    required this.onState,
    required this.onEnsure,
    required this.onAdopt,
  });
  final AdminRecord pool;
  final bool busy;
  final ValueChanged<String> onState;
  final ValueChanged<bool> onEnsure;
  final VoidCallback onAdopt;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final i = ref.watch(i18nProvider);
    final t = context.tokens;
    final states = pool.record('states').data;
    final gates = pool.record('gates');
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        AdminCard(
          subtitle: i.t('admin:pool.title'),
          title: i.t(
            'admin:pool.watermark',
            vars: {
              'current': states['prewarm'] ?? 0,
              'target': pool.integer('target_prewarm'),
            },
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              for (final flag in ['auto_purchase', 'auto_renew'])
                Padding(
                  padding: const EdgeInsets.symmetric(vertical: 3),
                  child: Row(
                    children: [
                      Icon(
                        pool.flag(flag)
                            ? Icons.check_circle_outline
                            : Icons.pause_circle_outline,
                        size: 16,
                        color: pool.flag(flag) ? t.a700 : t.n600,
                      ),
                      const SizedBox(width: 8),
                      Expanded(
                        child: Text(
                          i.t(
                            flag == 'auto_purchase'
                                ? pool.flag(flag)
                                      ? 'admin:pool.autoOn'
                                      : 'admin:pool.autoOff'
                                : pool.flag(flag)
                                ? 'admin:pool.autoRenewOn'
                                : 'admin:pool.autoRenewOff',
                          ),
                          style: TextStyle(
                            color: t.n700,
                            fontSize: FontSizes.sm,
                          ),
                        ),
                      ),
                    ],
                  ),
                ),
            ],
          ),
        ),
        AdminSectionHeading(
          i.t('admin:mobile.desktopStates'),
          subtitle: i.t('admin:mobile.tapState'),
        ),
        LayoutBuilder(
          builder: (context, constraints) {
            final columns = constraints.maxWidth >= 520 ? 3 : 2;
            return Wrap(
              spacing: 12,
              runSpacing: 4,
              children: [
                for (final state in states.entries)
                  SizedBox(
                    width:
                        (constraints.maxWidth - (columns - 1) * 12) / columns,
                    child: Material(
                      color: t.bg,
                      child: InkWell(
                        onTap: busy ? null : () => onState(state.key),
                        child: Container(
                          padding: const EdgeInsets.symmetric(
                            vertical: 12,
                            horizontal: 6,
                          ),
                          decoration: BoxDecoration(
                            border: Border(bottom: BorderSide(color: t.hair)),
                          ),
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Row(
                                children: [
                                  Expanded(
                                    child: Text(
                                      state.value.toString(),
                                      style: TextStyle(
                                        color: t.ink,
                                        fontSize: FontSizes.xl3,
                                        fontWeight: FontWeight.w500,
                                      ),
                                    ),
                                  ),
                                  Icon(
                                    Icons.chevron_right,
                                    size: 16,
                                    color: t.n600,
                                  ),
                                ],
                              ),
                              Text(
                                adminLabel(i, 'admin:mobile.states', state.key),
                                style: TextStyle(
                                  color: t.n600,
                                  fontSize: FontSizes.sm,
                                ),
                              ),
                            ],
                          ),
                        ),
                      ),
                    ),
                  ),
              ],
            );
          },
        ),
        const SizedBox(height: 16),
        AdminSectionHeading(i.t('admin:mobile.capacity')),
        Text(
          i.t(
            'admin:pool.gates',
            vars: {
              'price': gates.string('max_unit_price_cny'),
              'tick': gates.integer('max_per_tick'),
              'day': gates.integer('max_per_day'),
              'multiple': gates.string('min_balance_multiple'),
            },
          ),
          style: TextStyle(color: t.n600, fontSize: FontSizes.sm, height: 1.5),
        ),
        const SizedBox(height: 12),
        Wrap(
          spacing: 10,
          runSpacing: 6,
          children: [
            OutlinedButton(
              onPressed: busy ? null : () => onEnsure(true),
              child: Text(i.t('admin:pool.dryRun')),
            ),
            FilledButton(
              onPressed: busy ? null : () => onEnsure(false),
              child: Text(i.t('admin:pool.ensure')),
            ),
          ],
        ),
        const SizedBox(height: 12),
        AdminRecordTile(
          title: i.t('admin:pool.adopt'),
          subtitle: i.t('admin:mobile.adoptHint'),
          leading: Icon(Icons.add_to_queue, color: t.n700, size: 22),
          onTap: busy ? null : onAdopt,
        ),
      ],
    );
  }
}
