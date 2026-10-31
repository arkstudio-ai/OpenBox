import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../shared/appearance/tokens.dart';
import '../../shared/appearance/type_scale.dart';
import '../../shared/download/asset_download.dart';
import '../../shared/i18n/i18n.dart';
import '../../shared/models/first_video.dart';
import '../../shared/router/paths.dart';
import '../../shared/widgets/toast.dart';
import 'state/first_video_flow.dart';
import 'widgets/flow_chrome.dart';
import 'widgets/video_box.dart';

/// Whether the account may publish from here (a paid plan — publishing needs
/// the cloud desktop). Wired by the app layer from the billing feature;
/// absent or failing, the page sends people to the plans.
typedef FirstVideoPublishGate = Future<bool> Function();

/// ⑤ 成片 / ⑥ 失败: one route for both, since a push for either lands here
/// and the page asks the server which it is. In flight → the making page;
/// nothing started → the first screen.
class FirstVideoResultPage extends ConsumerStatefulWidget {
  const FirstVideoResultPage({super.key, this.canPublish});

  final FirstVideoPublishGate? canPublish;

  @override
  ConsumerState<FirstVideoResultPage> createState() =>
      _FirstVideoResultPageState();
}

class _FirstVideoResultPageState extends ConsumerState<FirstVideoResultPage> {
  FirstVideoJob? _job;
  bool _loadFailed = false;
  bool _saving = false;
  bool _publishing = false;

  @override
  void initState() {
    super.initState();
    final known = ref.read(firstVideoJobProvider);
    if (known != null && (known.ready || known.failed)) _job = known;
    WidgetsBinding.instance.addPostFrameCallback((_) => unawaited(_load()));
  }

  Future<void> _load() async {
    try {
      final job = await ref.read(firstVideoFlowProvider).refresh();
      if (!mounted) return;
      if (job.inFlight) {
        context.go(Paths.firstVideoMaking);
      } else if (!job.started) {
        context.go(Paths.firstVideoShop);
      } else {
        setState(() {
          _job = job;
          _loadFailed = false;
        });
      }
    } catch (_) {
      if (mounted && _job == null) setState(() => _loadFailed = true);
    }
  }

  Future<void> _save(String assetId) async {
    if (_saving) return;
    setState(() => _saving = true);
    try {
      await saveAssetToDevice(
        ref.read,
        assetId: assetId,
        mimeType: 'video/mp4',
        target: AssetSaveTarget.album,
      );
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  Future<void> _publish(FirstVideoJob job) async {
    if (_publishing) return;
    setState(() => _publishing = true);
    var allowed = false;
    try {
      allowed = await widget.canPublish?.call() ?? false;
    } catch (_) {
      allowed = false;
    }
    if (!mounted) return;
    setState(() => _publishing = false);
    final i18n = ref.read(i18nProvider);
    final toast = ref.read(toastProvider.notifier);
    final sessionId = job.sessionId;
    if (!allowed || sessionId == null || sessionId.isEmpty) {
      toast.info(i18n.t('first_video:result.publishLocked'));
      context.go(Paths.billing());
      return;
    }
    toast.info(i18n.t('first_video:result.publishHint'));
    context.go(Paths.chat(sessionId));
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final job = _job;
    if (job == null) {
      return FirstVideoScaffold(
        scrollable: false,
        body: Center(
          child: _loadFailed
              ? FirstVideoSecondaryButton(
                  key: const Key('fv-retry'),
                  label: i18n.t('first_video:result.retry'),
                  onTap: _load,
                )
              : const CircularProgressIndicator(strokeWidth: 2),
        ),
      );
    }
    if (job.failed || job.assetId == null) {
      return _FailedView(job: job);
    }
    final assetId = job.assetId!;
    return FirstVideoScaffold(
      scrollable: false,
      footer: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          FirstVideoPrimaryButton(
            key: const Key('fv-publish'),
            label: i18n.t('first_video:result.publish'),
            icon: Icons.upload_outlined,
            onTap: _publishing ? null : () => _publish(job),
          ),
          const SizedBox(height: 10),
          Row(
            children: [
              Expanded(
                child: FirstVideoSecondaryButton(
                  key: const Key('fv-save'),
                  label: i18n.t('first_video:result.save'),
                  onTap: _saving ? null : () => _save(assetId),
                ),
              ),
              const SizedBox(width: 10),
              Expanded(
                child: FirstVideoSecondaryButton(
                  key: const Key('fv-again'),
                  label: i18n.t('first_video:result.again'),
                  onTap: job.sessionId == null
                      ? null
                      : () => context.go(Paths.chat(job.sessionId!)),
                ),
              ),
            ],
          ),
          FirstVideoLinkButton(
            key: const Key('fv-home'),
            label: i18n.t('first_video:result.home'),
            onTap: () => context.go(Paths.app),
          ),
        ],
      ),
      body: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            crossAxisAlignment: CrossAxisAlignment.end,
            children: [
              Expanded(
                child: Text(
                  i18n.t('first_video:result.title'),
                  style: TextStyle(
                    fontSize: FontSizes.xl2,
                    fontWeight: FontWeight.w800,
                    color: t.ink,
                  ),
                ),
              ),
              Text(
                i18n.t('first_video:free'),
                key: const Key('fv-free-note'),
                style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
              ),
            ],
          ),
          const SizedBox(height: 14),
          Expanded(
            child: Center(
              child: FirstVideoPlayer(key: ValueKey(assetId), assetId: assetId),
            ),
          ),
          const SizedBox(height: 4),
        ],
      ),
    );
  }
}

class _FailedView extends ConsumerWidget {
  const _FailedView({required this.job});

  final FirstVideoJob job;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final sessionId = job.sessionId;
    return FirstVideoScaffold(
      scrollable: false,
      footer: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          FirstVideoPrimaryButton(
            key: const Key('fv-failed-home'),
            label: i18n.t('first_video:failed.home'),
            onTap: () => context.go(Paths.app),
          ),
          const SizedBox(height: 10),
          FirstVideoSecondaryButton(
            key: const Key('fv-failed-chat'),
            label: i18n.t('first_video:failed.chat'),
            onTap: () => context.go(
              sessionId == null || sessionId.isEmpty
                  ? Paths.app
                  : Paths.chat(sessionId),
            ),
          ),
        ],
      ),
      body: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const SizedBox(height: 24),
          Container(
            width: 56,
            height: 56,
            decoration: BoxDecoration(color: t.surface, shape: BoxShape.circle),
            child: Icon(Icons.sentiment_dissatisfied_outlined, color: t.n700),
          ),
          const SizedBox(height: 20),
          FirstVideoHeading(
            title: i18n.t('first_video:failed.title'),
            lede: i18n.t('first_video:failed.lede'),
          ),
        ],
      ),
    );
  }
}
