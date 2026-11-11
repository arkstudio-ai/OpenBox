import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/download/asset_download.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/widgets/toast.dart';
import '../../chat/state/chat_session_controller.dart';
import '../../chat/utils/content_view.dart';
import '../state/light_providers.dart';
import '../state/light_start.dart';
import 'autopilot_switch.dart';

/// The result card's own buttons on a Light topic page (docs
/// `LIGHT_MODE_PLAN.md` §4.3 ④ 成片卡): save to the album, publish to Douyin
/// (or, without a desktop, the page that unlocks it), and make another one in
/// the same conversation. Publishing and "another" are ordinary chat turns,
/// so the agent keeps the context of this result.
class LightResultActions extends ConsumerStatefulWidget {
  const LightResultActions({
    super.key,
    required this.sessionId,
    required this.group,
  });

  final String sessionId;
  final ArtifactGroup group;

  @override
  ConsumerState<LightResultActions> createState() => _LightResultActionsState();
}

class _LightResultActionsState extends ConsumerState<LightResultActions> {
  bool _saving = false;
  bool _savingPdf = false;
  bool _sending = false;

  ({String assetId, String mime})? get _media {
    for (final part in widget.group.parts) {
      final id = part.assetId;
      final mime = part.mimeType ?? '';
      if (id != null &&
          id.isNotEmpty &&
          (mime.startsWith('video/') || mime.startsWith('image/'))) {
        return (assetId: id, mime: mime);
      }
    }
    return null;
  }

  /// The printable PDF `image_to_pdf` attached next to a menu or poster
  /// image: same group as the picture, `application/pdf`.
  ({String assetId, String name})? get _pdf {
    for (final part in widget.group.parts) {
      final id = part.assetId;
      if (id != null && id.isNotEmpty && part.mimeType == 'application/pdf') {
        return (assetId: id, name: part.path.split('/').last);
      }
    }
    return null;
  }

  Future<void> _savePdf(String assetId, String name) async {
    if (_savingPdf) return;
    setState(() => _savingPdf = true);
    try {
      await saveAssetToDevice(
        ref.read,
        assetId: assetId,
        name: name,
        mimeType: 'application/pdf',
        target: AssetSaveTarget.files,
      );
    } finally {
      if (mounted) setState(() => _savingPdf = false);
    }
  }

  Future<void> _save(String assetId, String mime) async {
    if (_saving) return;
    setState(() => _saving = true);
    try {
      await saveAssetToDevice(
        ref.read,
        assetId: assetId,
        mimeType: mime,
        target: AssetSaveTarget.album,
      );
    } finally {
      if (mounted) setState(() => _saving = false);
    }
  }

  Future<void> _say(String text) async {
    if (_sending) return;
    setState(() => _sending = true);
    try {
      await ref.read(chatSessionProvider(widget.sessionId).notifier).send(text);
    } finally {
      if (mounted) setState(() => _sending = false);
    }
  }

  Future<void> _publish() async {
    final i18n = ref.read(i18nProvider);
    final view = ref.read(lightEntriesProvider).valueOrNull;
    final entry = view?.byId('douyin_publish');
    if (entry != null && entry.available) {
      await _say(i18n.t('light:result.publishPrompt'));
      return;
    }
    ref.read(toastProvider.notifier).info(i18n.t('light:result.publishLocked'));
    final target = entry == null
        ? null
        : lightUnavailableTarget(
            entry.availability,
            paidPlan: view?.paidPlan ?? false,
          );
    if (target != null && mounted) unawaited(context.push(target));
  }

  @override
  Widget build(BuildContext context) {
    final i18n = ref.watch(i18nProvider);
    final media = _media;
    if (media == null) return const SizedBox.shrink();
    final video = media.mime.startsWith('video/');
    final pdf = _pdf;
    return Padding(
      padding: const EdgeInsets.only(top: 10),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              _ActionChip(
                key: const Key('light-result-save'),
                icon: Icons.download_outlined,
                label: i18n.t('light:result.save'),
                primary: true,
                busy: _saving,
                onTap: () => _save(media.assetId, media.mime),
              ),
              if (pdf != null)
                _ActionChip(
                  key: const Key('light-result-save-pdf'),
                  icon: Icons.picture_as_pdf_outlined,
                  label: i18n.t('light:result.savePdf'),
                  busy: _savingPdf,
                  onTap: () => _savePdf(pdf.assetId, pdf.name),
                ),
              if (video)
                _ActionChip(
                  key: const Key('light-result-publish'),
                  icon: Icons.upload_outlined,
                  label: i18n.t('light:result.publish'),
                  onTap: _sending ? null : _publish,
                ),
              _ActionChip(
                key: const Key('light-result-again'),
                icon: Icons.refresh,
                label: i18n.t('light:result.again'),
                onTap: _sending
                    ? null
                    : () => _say(i18n.t('light:result.againPrompt')),
              ),
            ],
          ),
          LightAutopilotSwitch(
            sessionId: widget.sessionId,
            assetId: media.assetId,
            isVideo: video,
          ),
        ],
      ),
    );
  }
}

class _ActionChip extends StatelessWidget {
  const _ActionChip({
    super.key,
    required this.icon,
    required this.label,
    required this.onTap,
    this.primary = false,
    this.busy = false,
  });

  final IconData icon;
  final String label;
  final VoidCallback? onTap;
  final bool primary;
  final bool busy;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final foreground = primary ? t.bg : t.ink;
    return Material(
      color: primary ? t.a700 : t.card,
      shape: RoundedRectangleBorder(
        side: primary ? BorderSide.none : BorderSide(color: t.hair),
        borderRadius: BorderRadius.circular(Radii.full),
      ),
      child: InkWell(
        borderRadius: BorderRadius.circular(Radii.full),
        onTap: busy ? null : onTap,
        child: ConstrainedBox(
          constraints: const BoxConstraints(minHeight: 40),
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 8),
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                if (busy)
                  SizedBox(
                    width: 16,
                    height: 16,
                    child: CircularProgressIndicator(
                      strokeWidth: 2,
                      color: foreground,
                    ),
                  )
                else
                  Icon(icon, size: 18, color: foreground),
                const SizedBox(width: 6),
                Text(
                  label,
                  style: TextStyle(
                    fontSize: FontSizes.base,
                    fontWeight: FontWeight.w600,
                    color: foreground,
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

/// Builder handed to the chat transcript (`LightChatScope.resultActions`).
Widget lightResultActionsFor(
  String sessionId,
  BuildContext context,
  ArtifactGroup group,
) => LightResultActions(
  key: ValueKey('light-result-${group.id}'),
  sessionId: sessionId,
  group: group,
);
