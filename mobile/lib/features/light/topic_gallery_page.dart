import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../shared/appearance/tokens.dart';
import '../../shared/appearance/type_scale.dart';
import '../../shared/i18n/i18n.dart';
import '../../shared/models/light.dart';
import '../../shared/router/paths.dart';
import '../../shared/utils/error_text.dart';
import '../../shared/widgets/section_tabs.dart';
import 'state/light_providers.dart';
import 'state/light_start.dart';
import 'widgets/entry_card.dart';

/// The front page of a topic (docs `LIGHT_MODE_PLAN.md` §4.7 #16): the
/// sample gallery, by subtype, with a one-line description and what to
/// prepare for each look. 「就做这种」 starts the chat with the pick written
/// into its opening card; 「不挑了，直接开始」 starts it plain. An entry with
/// no samples yet (every one today — `sample_media` is still empty) shows
/// its steps and price instead and starts straight away.
class LightTopicGalleryPage extends ConsumerStatefulWidget {
  const LightTopicGalleryPage({super.key, required this.entryId});

  final String entryId;

  @override
  ConsumerState<LightTopicGalleryPage> createState() =>
      _LightTopicGalleryPageState();
}

class _LightTopicGalleryPageState extends ConsumerState<LightTopicGalleryPage> {
  String? _subtypeId;
  String? _sampleId;
  bool _starting = false;

  void _back() =>
      context.canPop() ? context.pop() : context.go(Paths.home);

  Future<void> _start(
    LightEntry entry, {
    LightSubtype? subtype,
    LightSample? sample,
  }) async {
    if (_starting) return;
    setState(() => _starting = true);
    await startLightTopic(
      context,
      ref,
      entry,
      subtype: sample == null ? null : subtype?.id,
      sample: sample?.id,
      replace: true,
    );
    if (mounted) setState(() => _starting = false);
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final entries = ref.watch(lightEntriesProvider);
    final view = entries.valueOrNull;
    final entry = view?.byId(widget.entryId);
    if (entry == null) {
      return Scaffold(
        backgroundColor: t.bg,
        appBar: AppBar(leading: BackButton(onPressed: _back)),
        body: Center(
          child: entries.isLoading
              ? const CircularProgressIndicator(strokeWidth: 2)
              : Padding(
                  padding: const EdgeInsets.all(24),
                  child: Column(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      Text(
                        entries.hasError
                            ? errorText(i18n, entries.error!)
                            : i18n.t('light:gallery.missing'),
                        textAlign: TextAlign.center,
                        style: TextStyle(fontSize: FontSizes.sm, color: t.n700),
                      ),
                      TextButton(
                        onPressed: () => ref.invalidate(lightEntriesProvider),
                        child: Text(i18n.t('common:action.retry')),
                      ),
                    ],
                  ),
                ),
        ),
      );
    }
    final subtypes = entry.subtypes.where((s) => s.samples.isNotEmpty).toList();
    final subtype = subtypes.isEmpty
        ? null
        : subtypes.firstWhere(
            (s) => s.id == _subtypeId,
            orElse: () => subtypes.first,
          );
    final sample = subtype?.samples
        .where((s) => s.id == _sampleId)
        .firstOrNull;
    final gallery = subtype != null;
    return Scaffold(
      backgroundColor: t.bg,
      appBar: AppBar(
        leading: BackButton(key: const Key('light-gallery-back'), onPressed: _back),
        titleSpacing: 0,
        title: Text(
          entry.title,
          style: TextStyle(
            fontSize: FontSizes.lg,
            fontWeight: FontWeight.w600,
            color: t.ink,
          ),
        ),
      ),
      body: Column(
        children: [
          if (subtypes.length > 1)
            SectionTabs(
              labels: {for (final s in subtypes) s.id: s.label},
              value: subtype!.id,
              onChanged: (value) => setState(() {
                _subtypeId = value;
                _sampleId = null;
              }),
            ),
          Expanded(
            child: ListView(
              padding: const EdgeInsets.fromLTRB(16, 16, 16, 16),
              children: [
                Text(
                  gallery ? i18n.t('light:gallery.lede') : entry.subtitle,
                  style: TextStyle(
                    fontSize: FontSizes.base,
                    height: 1.45,
                    color: t.n700,
                  ),
                ),
                const SizedBox(height: 14),
                if (gallery)
                  for (final (index, item) in subtype.samples.indexed) ...[
                    if (index > 0) const SizedBox(height: 10),
                    _SampleCard(
                      key: Key('light-sample-${item.id}'),
                      sample: item,
                      shade: index,
                      selected: _sampleId == item.id,
                      needsLabel: i18n.t(
                        'light:gallery.needs',
                        vars: {'needs': item.needs},
                      ),
                      onTap: () => setState(() => _sampleId = item.id),
                    ),
                  ]
                else
                  _Intro(entry: entry, i18n: i18n),
                if (entry.priceHint.isNotEmpty) ...[
                  const SizedBox(height: 16),
                  Row(
                    children: [
                      Icon(Icons.toll_outlined, size: 14, color: t.n600),
                      const SizedBox(width: 6),
                      Text(
                        entry.priceHint,
                        style: TextStyle(fontSize: FontSizes.sm, color: t.n600),
                      ),
                    ],
                  ),
                ],
              ],
            ),
          ),
          SafeArea(
            top: false,
            child: Padding(
              padding: const EdgeInsets.fromLTRB(16, 10, 16, 12),
              child: entry.available
                  ? Column(
                      crossAxisAlignment: CrossAxisAlignment.stretch,
                      children: [
                        FilledButton(
                          key: const Key('light-gallery-start'),
                          onPressed:
                              _starting || (gallery && sample == null)
                              ? null
                              : () => _start(
                                  entry,
                                  subtype: subtype,
                                  sample: sample,
                                ),
                          style: FilledButton.styleFrom(
                            backgroundColor: t.a700,
                            foregroundColor: t.bg,
                            disabledBackgroundColor: t.n300,
                            disabledForegroundColor: t.n600,
                            minimumSize: const Size.fromHeight(54),
                            shape: RoundedRectangleBorder(
                              borderRadius: BorderRadius.circular(Radii.full),
                            ),
                          ),
                          child: Text(
                            i18n.t(
                              _starting
                                  ? 'light:gallery.starting'
                                  : gallery
                                  ? 'light:gallery.pick'
                                  : 'light:gallery.start',
                            ),
                            style: const TextStyle(
                              fontSize: FontSizes.xl,
                              fontWeight: FontWeight.w700,
                            ),
                          ),
                        ),
                        if (gallery)
                          TextButton(
                            key: const Key('light-gallery-skip'),
                            onPressed: _starting ? null : () => _start(entry),
                            style: TextButton.styleFrom(
                              foregroundColor: t.n700,
                              minimumSize: const Size.fromHeight(44),
                            ),
                            child: Text(
                              i18n.t('light:gallery.skip'),
                              style: const TextStyle(fontSize: FontSizes.base),
                            ),
                          ),
                      ],
                    )
                  : _Blocked(entry: entry, view: view!, i18n: i18n),
            ),
          ),
        ],
      ),
    );
  }
}

class _SampleCard extends StatelessWidget {
  const _SampleCard({
    super.key,
    required this.sample,
    required this.shade,
    required this.selected,
    required this.needsLabel,
    required this.onTap,
  });

  final LightSample sample;
  final int shade;
  final bool selected;
  final String needsLabel;
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
              // The sample still goes here once `media_url` is filled; until
              // then a tinted portrait tile keeps the card's shape.
              ClipRRect(
                borderRadius: BorderRadius.circular(Radii.md),
                child: SizedBox(
                  width: 72,
                  height: 108,
                  child: sample.mediaUrl != null
                      ? Image.network(
                          sample.mediaUrl!,
                          fit: BoxFit.cover,
                          errorBuilder: (_, _, _) => _tile(t),
                        )
                      : _tile(t),
                ),
              ),
              const SizedBox(width: 14),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Row(
                      children: [
                        Expanded(
                          child: Text(
                            sample.label,
                            style: TextStyle(
                              fontSize: FontSizes.xl,
                              fontWeight: FontWeight.w700,
                              color: t.ink,
                            ),
                          ),
                        ),
                        if (selected)
                          Icon(Icons.check_circle, size: 20, color: t.a800),
                      ],
                    ),
                    const SizedBox(height: 4),
                    Text(
                      sample.description,
                      style: TextStyle(
                        fontSize: FontSizes.md,
                        height: 1.4,
                        color: t.n700,
                      ),
                    ),
                    if (sample.needs.isNotEmpty) ...[
                      const SizedBox(height: 8),
                      Text(
                        needsLabel,
                        style: TextStyle(
                          fontSize: FontSizes.xs,
                          color: t.n600,
                        ),
                      ),
                    ],
                  ],
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _tile(BossipTokens t) => ColoredBox(
    color: [t.n900, t.n800, t.n700][shade % 3],
    child: Icon(Icons.play_arrow_rounded, color: t.n100, size: 26),
  );
}

/// An entry without a gallery: its steps as a preview, so the person knows
/// what the chat will ask before it starts.
class _Intro extends StatelessWidget {
  const _Intro({required this.entry, required this.i18n});

  final LightEntry entry;
  final I18nState i18n;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Container(
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: t.card,
        borderRadius: BorderRadius.circular(Radii.xl),
        border: Border.all(color: t.hair),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Icon(lightEntryIcon(entry.id), size: 22, color: t.ink),
              const SizedBox(width: 8),
              Text(
                i18n.t('light:gallery.stepsTitle'),
                style: TextStyle(
                  fontSize: FontSizes.base,
                  fontWeight: FontWeight.w600,
                  color: t.ink,
                ),
              ),
            ],
          ),
          const SizedBox(height: 12),
          for (final (index, step) in entry.steps.indexed)
            Padding(
              padding: const EdgeInsets.only(bottom: 8),
              child: Row(
                children: [
                  Container(
                    width: 22,
                    height: 22,
                    alignment: Alignment.center,
                    decoration: BoxDecoration(
                      color: t.a200,
                      shape: BoxShape.circle,
                    ),
                    child: Text(
                      '${index + 1}',
                      style: TextStyle(
                        fontSize: FontSizes.xs,
                        fontWeight: FontWeight.w700,
                        color: t.a800,
                      ),
                    ),
                  ),
                  const SizedBox(width: 10),
                  Text(
                    step.label,
                    style: TextStyle(fontSize: FontSizes.base, color: t.ink),
                  ),
                ],
              ),
            ),
          if (entry.steps.isEmpty)
            Text(
              i18n.t('light:gallery.noSamples'),
              style: TextStyle(fontSize: FontSizes.sm, color: t.n600),
            ),
        ],
      ),
    );
  }
}

/// The footer of an entry the workspace cannot use: the reason and the one
/// button that lifts it.
class _Blocked extends StatelessWidget {
  const _Blocked({required this.entry, required this.view, required this.i18n});

  final LightEntry entry;
  final LightEntriesView view;
  final I18nState i18n;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final target = lightUnavailableTarget(
      entry.availability,
      paidPlan: view.paidPlan,
    );
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Text(
          i18n.t('light:home.${lightAvailabilityKey(entry.availability)}'),
          textAlign: TextAlign.center,
          style: TextStyle(fontSize: FontSizes.sm, color: t.n700),
        ),
        if (target != null) ...[
          const SizedBox(height: 10),
          FilledButton(
            key: const Key('light-gallery-unblock'),
            onPressed: () => context.push(target),
            style: FilledButton.styleFrom(
              backgroundColor: t.ink,
              foregroundColor: t.bg,
              minimumSize: const Size.fromHeight(50),
              shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(Radii.full),
              ),
            ),
            child: Text(
              i18n.t(
                target == Paths.desktop
                    ? 'light:home.goDesktop'
                    : target.startsWith('/app/auth-center')
                    ? 'light:home.goBinding'
                    : 'light:home.goPlan',
              ),
              style: const TextStyle(
                fontSize: FontSizes.lg,
                fontWeight: FontWeight.w600,
              ),
            ),
          ),
        ],
      ],
    );
  }
}
