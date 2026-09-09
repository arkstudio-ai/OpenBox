import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_timezone/flutter_timezone.dart';
import 'package:go_router/go_router.dart';
import 'package:intl/intl.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/billing.dart';
import '../../../shared/router/paths.dart';
import '../../../shared/utils/format.dart';
import '../state/billing_providers.dart';
import 'billing_widgets.dart';

class BillingUsageTab extends ConsumerStatefulWidget {
  const BillingUsageTab({super.key});

  @override
  ConsumerState<BillingUsageTab> createState() => _BillingUsageTabState();
}

class _BillingUsageTabState extends ConsumerState<BillingUsageTab> {
  int _page = 1;
  DateTime? _from;
  DateTime? _to;
  String _timezone = 'Asia/Shanghai';

  @override
  void initState() {
    super.initState();
    FlutterTimezone.getLocalTimezone().then((zone) {
      if (mounted) setState(() => _timezone = zone.identifier);
    });
  }

  BillingUsageQuery get _query => BillingUsageQuery(
    page: _page,
    dateFrom: _from,
    dateTo: _to,
    timezone: _timezone,
  );

  Future<void> _pickDate({required bool from}) async {
    final initial = (from ? _from : _to) ?? DateTime.now();
    final selected = await showDatePicker(
      context: context,
      initialDate: initial,
      firstDate: DateTime(2020),
      lastDate: DateTime.now(),
    );
    if (selected == null || !mounted) return;
    setState(() {
      if (from) {
        _from = selected;
        if (_to != null && selected.isAfter(_to!)) _to = selected;
      } else {
        _to = selected;
        if (_from != null && selected.isBefore(_from!)) _from = selected;
      }
      _page = 1;
    });
  }

  @override
  Widget build(BuildContext context) {
    final tokens = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final query = _query;
    final balance = ref.watch(billingBalanceProvider);
    final summary = ref.watch(billingSummaryProvider(query));
    final usage = ref.watch(billingUsageProvider(query));
    final result = usage.valueOrNull;
    final filtered = _from != null || _to != null;

    return I18nScope(
      state: i18n,
      child: RefreshIndicator(
        onRefresh: () async {
          ref.invalidate(billingBalanceProvider);
          ref.invalidate(billingSummaryProvider(query));
          ref.invalidate(billingUsageProvider(query));
        },
        child: ListView(
          padding: const EdgeInsets.fromLTRB(16, 12, 16, 32),
          children: [
            CreditBalanceCard(
              balance: balance.valueOrNull?.balance,
              footer: Row(
                children: [
                  Expanded(
                    child: _Metric(
                      label: i18n.t(
                        filtered
                            ? 'billing:usage.filteredTokens'
                            : 'billing:usage.totalTokens',
                      ),
                      value: summary.valueOrNull == null
                          ? '—'
                          : formatTokens(summary.valueOrNull!.totalTokens),
                    ),
                  ),
                  const SizedBox(width: 16),
                  Expanded(
                    child: _Metric(
                      label: i18n.t(
                        filtered
                            ? 'billing:usage.filteredCredits'
                            : 'billing:usage.totalCredits',
                      ),
                      value: formatCredits(summary.valueOrNull?.totalCredits),
                    ),
                  ),
                ],
              ),
            ),
            if (balance.hasError || summary.hasError || usage.hasError) ...[
              const SizedBox(height: 12),
              BillingErrorBar(
                label: i18n.t('billing:usage.loadError'),
                onRetry: () {
                  ref.invalidate(billingBalanceProvider);
                  ref.invalidate(billingSummaryProvider(query));
                  ref.invalidate(billingUsageProvider(query));
                },
              ),
            ],
            const SizedBox(height: 20),
            Row(
              children: [
                Expanded(
                  child: Text(
                    i18n.t(
                      'billing:usage.detailsCount',
                      vars: {'count': result?.total ?? 0},
                    ),
                    style: TextStyle(
                      fontSize: FontSizes.lg,
                      fontWeight: FontWeight.w500,
                      color: tokens.ink,
                    ),
                  ),
                ),
                if (usage.isLoading)
                  SizedBox(
                    width: 16,
                    height: 16,
                    child: CircularProgressIndicator(
                      strokeWidth: 2,
                      color: tokens.a700,
                    ),
                  ),
              ],
            ),
            const SizedBox(height: 10),
            BillingCard(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  Text(
                    i18n.t('billing:usage.dateRange'),
                    style: TextStyle(
                      fontSize: FontSizes.xs,
                      color: tokens.n600,
                    ),
                  ),
                  const SizedBox(height: 9),
                  Wrap(
                    spacing: 8,
                    runSpacing: 8,
                    children: [
                      _DateButton(
                        label: i18n.t('billing:usage.dateFrom'),
                        value: _from,
                        language: i18n.language,
                        onPressed: () => _pickDate(from: true),
                      ),
                      _DateButton(
                        label: i18n.t('billing:usage.dateTo'),
                        value: _to,
                        language: i18n.language,
                        onPressed: () => _pickDate(from: false),
                      ),
                      if (filtered)
                        TextButton(
                          onPressed: () => setState(() {
                            _from = null;
                            _to = null;
                            _page = 1;
                          }),
                          child: Text(i18n.t('billing:usage.resetFilter')),
                        ),
                    ],
                  ),
                ],
              ),
            ),
            const SizedBox(height: 12),
            if (result?.items.isNotEmpty ?? false)
              BillingCard(
                padding: EdgeInsets.zero,
                child: Column(
                  children: [
                    for (
                      var index = 0;
                      index < result!.items.length;
                      index++
                    ) ...[
                      _UsageRow(entry: result.items[index]),
                      if (index != result.items.length - 1)
                        Divider(height: 1, color: tokens.hair),
                    ],
                  ],
                ),
              )
            else if (!usage.isLoading && !usage.hasError)
              BillingCard(
                child: Padding(
                  padding: const EdgeInsets.symmetric(vertical: 18),
                  child: Text(
                    i18n.t(
                      filtered
                          ? 'billing:usage.noMatches'
                          : 'billing:usage.empty',
                    ),
                    textAlign: TextAlign.center,
                    style: TextStyle(
                      fontSize: FontSizes.sm,
                      color: tokens.n600,
                    ),
                  ),
                ),
              ),
            const SizedBox(height: 14),
            BillingPager(
              page: _page,
              pages: result?.totalPages ?? 1,
              busy: usage.isLoading,
              onPrevious: () => setState(() => _page--),
              onNext: () => setState(() => _page++),
            ),
          ],
        ),
      ),
    );
  }
}

class _Metric extends StatelessWidget {
  const _Metric({required this.label, required this.value});

  final String label;
  final String value;

  @override
  Widget build(BuildContext context) {
    final tokens = context.tokens;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          label,
          style: TextStyle(fontSize: FontSizes.xs, color: tokens.n600),
        ),
        const SizedBox(height: 4),
        Text(
          value,
          style: TextStyle(fontSize: FontSizes.xl, color: tokens.ink),
        ),
      ],
    );
  }
}

class _DateButton extends StatelessWidget {
  const _DateButton({
    required this.label,
    required this.value,
    required this.language,
    required this.onPressed,
  });

  final String label;
  final DateTime? value;
  final String language;
  final VoidCallback onPressed;

  @override
  Widget build(BuildContext context) {
    final tokens = context.tokens;
    return OutlinedButton.icon(
      onPressed: onPressed,
      style: billingSecondaryButton(tokens),
      icon: const Icon(Icons.calendar_today_outlined, size: 15),
      label: Text(
        value == null ? label : DateFormat.yMd(language).format(value!),
      ),
    );
  }
}

class _UsageRow extends StatelessWidget {
  const _UsageRow({required this.entry});

  final UsageEntry entry;

  @override
  Widget build(BuildContext context) {
    final tokens = context.tokens;
    final i18n = I18nScope.of(context);
    final model = entry.modelId.split('/').last;
    return InkWell(
      onTap: entry.sessionAvailable && entry.sessionId.isNotEmpty
          ? () => context.go(Paths.chat(entry.sessionId))
          : null,
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Flexible(
                  child: Container(
                    padding: const EdgeInsets.symmetric(
                      horizontal: 8,
                      vertical: 5,
                    ),
                    decoration: BoxDecoration(
                      color: tokens.n200,
                      borderRadius: BorderRadius.circular(Radii.sm),
                    ),
                    child: Text(
                      model,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(
                        fontSize: FontSizes.xs,
                        fontFamily: 'monospace',
                        color: tokens.ink,
                      ),
                    ),
                  ),
                ),
                const SizedBox(width: 10),
                Text(
                  entry.credits == null
                      ? i18n.t('billing:usage.status.${entry.status}')
                      : i18n.t(
                          'billing:usage.points',
                          vars: {'value': formatCredits(entry.credits)},
                        ),
                  style: TextStyle(
                    fontSize: FontSizes.sm,
                    fontWeight: FontWeight.w500,
                    color: tokens.a700,
                  ),
                ),
              ],
            ),
            const SizedBox(height: 9),
            Text(
              entry.sessionTitle,
              maxLines: 2,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(fontSize: FontSizes.sm, color: tokens.ink),
            ),
            const SizedBox(height: 7),
            Wrap(
              spacing: 12,
              runSpacing: 4,
              children: _usageDetailTexts(i18n, entry).map((widget) {
                return DefaultTextStyle(
                  style: TextStyle(fontSize: FontSizes.xs, color: tokens.n600),
                  child: widget,
                );
              }).toList(),
            ),
            const SizedBox(height: 8),
            Row(
              children: [
                Expanded(
                  child: Text(
                    formatDateTime(entry.createdAt, i18n.language),
                    style: TextStyle(
                      fontSize: FontSizes.xs,
                      color: tokens.n600,
                    ),
                  ),
                ),
                Text(
                  '${i18n.t('billing:usage.kind.${entry.kind}')} · '
                  '${i18n.t('billing:usage.status.${entry.status}')}',
                  style: TextStyle(fontSize: FontSizes.xs, color: tokens.n600),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}

/// Usage events billed by media quantity show duration / units instead of
/// token counts (mirrors web `UsagePage.tsx` MEDIA_KINDS).
const _mediaKinds = {
  'video_compose',
  'video_generate',
  'video_transcribe',
  'image_gen',
};

List<Widget> _usageDetailTexts(I18nState i18n, UsageEntry entry) {
  final tokens = entry.tokens;
  if (!_mediaKinds.contains(entry.kind)) {
    return [
      Text(i18n.t('billing:usage.input', vars: {'value': tokens.input})),
      Text(i18n.t('billing:usage.output', vars: {'value': tokens.output})),
      Text(i18n.t('billing:usage.cache', vars: {'value': tokens.cache})),
    ];
  }
  final items = <Widget>[];
  final duration = tokens.durationSec;
  if (duration != null) {
    final rounded = (duration * 10).round() / 10;
    final shown = rounded == rounded.roundToDouble()
        ? rounded.toInt().toString()
        : rounded.toString();
    items.add(
      Text(i18n.t('billing:usage.media.duration', vars: {'value': shown})),
    );
  }
  if (tokens.images != null) {
    items.add(
      Text(
        i18n.t('billing:usage.media.images', vars: {'value': tokens.images}),
      ),
    );
  }
  if (tokens.minutesBilled != null) {
    items.add(
      Text(
        i18n.t(
          'billing:usage.media.minutesBilled',
          vars: {'value': tokens.minutesBilled},
        ),
      ),
    );
  }
  if (tokens.secondsBilled != null) {
    items.add(
      Text(
        i18n.t(
          'billing:usage.media.secondsBilled',
          vars: {'value': tokens.secondsBilled},
        ),
      ),
    );
  }
  if (tokens.tier != null) {
    items.add(
      Text(i18n.t('billing:usage.media.tier', vars: {'value': tokens.tier})),
    );
  }
  if (tokens.resolution != null) {
    items.add(
      Text(
        i18n.t(
          'billing:usage.media.resolution',
          vars: {'value': tokens.resolution},
        ),
      ),
    );
  }
  return items;
}
