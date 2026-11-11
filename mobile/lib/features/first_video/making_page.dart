import 'dart:async';
import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../shared/appearance/tokens.dart';
import '../../shared/appearance/type_scale.dart';
import '../../shared/events/app_lifecycle.dart';
import '../../shared/i18n/i18n.dart';
import '../../shared/router/paths.dart';
import 'api/first_video_api.dart';
import 'state/first_video_flow.dart';
import 'state/first_video_store.dart';
import 'template_page.dart';
import 'widgets/capability_cards.dart';
import 'widgets/flow_chrome.dart';

/// ④ 制作中: polls the job every few seconds, paints progress against the
/// server's estimate, and shows what the app can do next while it waits.
/// Leaving is fine — the finished video arrives as a push.
class FirstVideoMakingPage extends ConsumerStatefulWidget {
  const FirstVideoMakingPage({super.key});

  static const pollInterval = Duration(seconds: 3);

  @override
  ConsumerState<FirstVideoMakingPage> createState() =>
      _FirstVideoMakingPageState();
}

class _FirstVideoMakingPageState extends ConsumerState<FirstVideoMakingPage> {
  late final DateTime _started;
  Timer? _poll;
  Timer? _clock;
  bool _polling = false;
  bool _left = false;

  @override
  void initState() {
    super.initState();
    _started = DateTime.now();
    _poll = Timer.periodic(FirstVideoMakingPage.pollInterval, (_) {
      // Off screen there is nobody to paint for; the push brings them back.
      if (ref.read(appVisibleProvider)) unawaited(_refresh());
    });
    _clock = Timer.periodic(const Duration(seconds: 1), (_) {
      if (mounted) setState(() {});
    });
    WidgetsBinding.instance.addPostFrameCallback((_) => unawaited(_refresh()));
  }

  @override
  void dispose() {
    _poll?.cancel();
    _clock?.cancel();
    super.dispose();
  }

  Future<void> _refresh() async {
    if (_polling || _left || !mounted) return;
    _polling = true;
    try {
      final job = await ref.read(firstVideoFlowProvider).refresh();
      if (!mounted || _left) return;
      if (job.ready || job.failed) {
        _left = true;
        context.go(Paths.firstVideoResult);
      } else if (!job.started) {
        // Nothing in flight for this workspace (a stale link): start over.
        _left = true;
        context.go(Paths.firstVideoShop);
      }
    } catch (_) {
      // A missed poll is not news; the next one asks again.
    } finally {
      _polling = false;
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final job = ref.watch(firstVideoJobProvider);
    final saved = ref.watch(firstVideoProvider).prefs;
    final draft = ref.watch(firstVideoDraftProvider);
    final catalog = ref.watch(firstVideoCatalogProvider).valueOrNull;

    final eta = (job?.etaSeconds ?? 0) > 0
        ? job!.etaSeconds
        : (catalog?.etaSeconds ?? 0);
    final elapsed = DateTime.now().difference(_started).inSeconds;
    final remaining = eta > 0 ? eta - elapsed : -1;
    final fraction = firstVideoProgressFraction(elapsed, eta);

    final templateId = draft.template.isNotEmpty
        ? draft.template
        : (saved?.template ?? '');
    final templateLabel = firstVideoTemplateOptions(i18n, catalog)
        .where((option) => option.id == templateId)
        .map((option) => option.label)
        .firstOrNull;
    final shopName = draft.shopName.trim().isNotEmpty
        ? draft.shopName.trim()
        : (saved?.shopName ?? '');
    final subtitle = [
      if (shopName.isNotEmpty) shopName,
      ?templateLabel,
      i18n.t('first_video:making.length'),
    ].join(' · ');

    final cards = [
      for (final (index, item)
          in i18n
              .tList('first_video:making.cards')
              .whereType<Map<String, dynamic>>()
              .indexed)
        CapabilityCard(
          title: (item['title'] ?? '').toString(),
          body: (item['body'] ?? '').toString(),
          icon: const [
            Icons.upload_outlined,
            Icons.schedule_outlined,
            Icons.movie_filter_outlined,
          ][index % 3],
        ),
    ];

    return FirstVideoScaffold(
      scrollable: false,
      footer: FirstVideoSecondaryButton(
        key: const Key('fv-leave'),
        label: i18n.t('first_video:making.leave'),
        height: 54,
        onTap: () => context.go(Paths.app),
      ),
      body: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          FirstVideoHeading(
            title: i18n.t('first_video:making.title'),
            lede: subtitle,
          ),
          const SizedBox(height: 24),
          Container(
            key: const Key('fv-progress'),
            padding: const EdgeInsets.all(18),
            decoration: BoxDecoration(
              color: t.surface,
              borderRadius: BorderRadius.circular(Radii.xl),
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    Expanded(
                      child: Text(
                        i18n.t('first_video:making.stage'),
                        style: TextStyle(
                          fontSize: FontSizes.base,
                          fontWeight: FontWeight.w600,
                          color: t.ink,
                        ),
                      ),
                    ),
                    Text(
                      _remainingText(i18n, remaining),
                      style: TextStyle(fontSize: FontSizes.md, color: t.n700),
                    ),
                  ],
                ),
                const SizedBox(height: 10),
                ClipRRect(
                  borderRadius: BorderRadius.circular(Radii.full),
                  child: LinearProgressIndicator(
                    value: fraction,
                    minHeight: 8,
                    backgroundColor: t.n300,
                    color: t.a700,
                  ),
                ),
                const SizedBox(height: 10),
                Text(
                  i18n.t('first_video:making.background'),
                  style: TextStyle(fontSize: FontSizes.sm, color: t.n600),
                ),
              ],
            ),
          ),
          const SizedBox(height: 24),
          Text(
            i18n.t('first_video:making.meanwhile'),
            style: TextStyle(
              fontSize: FontSizes.md,
              fontWeight: FontWeight.w600,
              color: t.n700,
            ),
          ),
          const SizedBox(height: 10),
          if (cards.isNotEmpty) CapabilityCards(cards: cards),
          const Spacer(),
        ],
      ),
    );
  }

  String _remainingText(I18nState i18n, int remaining) {
    if (remaining < 0) return i18n.t('first_video:making.remainingUnknown');
    if (remaining <= 5) return i18n.t('first_video:making.remainingSoon');
    final minutes = remaining ~/ 60;
    final seconds = remaining % 60;
    final time = minutes > 0
        ? i18n.t(
            'first_video:making.minutes',
            vars: {'m': minutes, 's': seconds},
          )
        : i18n.t('first_video:making.seconds', vars: {'s': seconds});
    return i18n.t('first_video:making.remaining', vars: {'time': time});
  }
}

/// How far along the bar sits for a given elapsed/estimate: it never reaches
/// the end on its own (the server says when it is done), and without an
/// estimate it creeps so the screen still visibly moves.
double firstVideoProgressFraction(int elapsedSeconds, int etaSeconds) =>
    etaSeconds > 0
    ? math.min(0.96, elapsedSeconds / etaSeconds)
    : math.min(0.9, 0.08 + elapsedSeconds / 240);
