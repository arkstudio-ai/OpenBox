import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:url_launcher/url_launcher.dart';

import '../../../../shared/appearance/tokens.dart';
import '../../../../shared/appearance/type_scale.dart';
import '../../../../shared/i18n/i18n.dart';
import '../../../../shared/models/message_part.dart';
import '../../../../shared/models/video_identity.dart';
import '../../api/video_identity_api.dart';
import '../../state/chat_session_controller.dart';
import '../../utils/video_identity_card.dart';

/// Real-person authorisation, shown in the turn that asked for it
/// (web `VideoIdentityCards`).
///
/// A video with a real presenter cannot be generated until that person has
/// completed a liveness authorisation on their own device. Nothing tells this
/// client when that lands, so the card carries the link and a "check again"
/// button, and once the check comes back active it sends the conversation on
/// by itself.
class VideoIdentityCards extends StatelessWidget {
  const VideoIdentityCards({
    super.key,
    required this.parts,
    required this.sessionId,
  });

  final List<MessagePart> parts;
  final String sessionId;

  @override
  Widget build(BuildContext context) {
    final tools = parts
        .whereType<ToolPart>()
        .where((p) => p.tool == 'video_identity')
        .toList();
    if (tools.isEmpty) return const SizedBox.shrink();
    final cards = cardsFromTools(tools);
    if (cards.isEmpty) return const SizedBox.shrink();
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        for (final card in cards)
          _IdentityCard(
            key: ValueKey(
              '${card.identity.identityId}:'
              '${card.identity.updatedAt ?? card.identity.status}',
            ),
            initial: card.identity,
            material: card.material,
            sessionId: sessionId,
          ),
      ],
    );
  }
}

class _IdentityCard extends ConsumerStatefulWidget {
  const _IdentityCard({
    super.key,
    required this.initial,
    required this.material,
    required this.sessionId,
  });

  final VideoIdentity initial;
  final VideoMaterialAsset? material;
  final String sessionId;

  @override
  ConsumerState<_IdentityCard> createState() => _IdentityCardState();
}

class _IdentityCardState extends ConsumerState<_IdentityCard> {
  late VideoIdentity _identity = widget.initial;
  bool _busy = false;
  String? _error;

  /// Only ever https. A link that arrives as anything else is dropped rather
  /// than launched.
  String? get _authorizationUrl {
    final raw = _identity.authorizationUrl;
    if (raw == null || raw.isEmpty) return null;
    final uri = Uri.tryParse(raw);
    return uri != null && uri.scheme == 'https' ? raw : null;
  }

  String _expiryLabel(I18nState i18n) {
    final raw = _identity.expiresAt;
    if (raw == null) return '';
    final at = DateTime.tryParse(raw)?.toLocal();
    if (at == null) return '';
    final hh = at.hour.toString().padLeft(2, '0');
    final mm = at.minute.toString().padLeft(2, '0');
    return i18n.t('chat:videoIdentity.expires', vars: {'time': '$hh:$mm'});
  }

  Future<void> _refresh() async {
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final refreshed =
          await ref.read(videoIdentityApiProvider).refresh(_identity.identityId);
      if (!mounted || refreshed == null) return;
      // Authorised — carry the conversation forward without making the person
      // work out what to type next.
      if (refreshed.isActive) {
        await ref.read(chatSessionProvider(widget.sessionId).notifier).send(
              ref.read(i18nProvider).t('chat:videoIdentity.continueMessage'),
            );
      }
      if (mounted) setState(() => _identity = refreshed);
    } catch (error) {
      if (mounted) {
        setState(() => _error =
            ref.read(i18nProvider).t('chat:videoIdentity.refreshFailed'));
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final active = _identity.isActive;
    final terminal = _identity.isTerminal;
    final pending = _identity.isPending;
    final message = _error ?? _identity.error;

    final (glyphBg, glyphFg, icon) = active
        ? (t.s100, t.s800, Icons.verified_user_outlined)
        : terminal
            ? (t.dangerSoft, t.danger, Icons.warning_amber_outlined)
            : (t.a100, t.a800, Icons.shield_outlined);

    return Container(
      margin: const EdgeInsets.only(top: 8),
      padding: const EdgeInsets.all(13),
      decoration: BoxDecoration(
        color: active
            ? t.s100.withValues(alpha: 0.35)
            : terminal
                ? t.dangerSoft.withValues(alpha: 0.35)
                : t.card,
        border: Border.all(
          color: active
              ? t.s400.withValues(alpha: 0.35)
              : terminal
                  ? t.danger.withValues(alpha: 0.25)
                  : t.hair,
        ),
        borderRadius: BorderRadius.circular(Radii.lg),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Container(
                width: 34,
                height: 34,
                alignment: Alignment.center,
                decoration:
                    BoxDecoration(color: glyphBg, shape: BoxShape.circle),
                child: Icon(icon, size: 18, color: glyphFg),
              ),
              const SizedBox(width: 11),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Wrap(
                      spacing: 7,
                      runSpacing: 4,
                      crossAxisAlignment: WrapCrossAlignment.center,
                      children: [
                        Text(
                          i18n.t('chat:videoIdentity.title'),
                          style: TextStyle(
                            fontSize: FontSizes.sm,
                            fontWeight: FontWeight.w500,
                            color: t.ink,
                          ),
                        ),
                        Container(
                          padding: const EdgeInsets.symmetric(
                              horizontal: 8, vertical: 2),
                          decoration: BoxDecoration(
                            color: t.hairSoft,
                            borderRadius: BorderRadius.circular(Radii.full),
                          ),
                          child: Text(
                            _identity.label,
                            style: TextStyle(
                                fontSize: FontSizes.xs, color: t.n700),
                          ),
                        ),
                      ],
                    ),
                    const SizedBox(height: 5),
                    Text(
                      i18n.t('chat:videoIdentity.status.${_identity.status}'),
                      style: TextStyle(
                        fontSize: FontSizes.sm,
                        height: 1.6,
                        color: t.n700,
                      ),
                    ),
                    if (pending && _identity.expiresAt != null)
                      Padding(
                        padding: const EdgeInsets.only(top: 3),
                        child: Text(
                          _expiryLabel(i18n),
                          style: TextStyle(
                              fontSize: FontSizes.xs, color: t.n600),
                        ),
                      ),
                    if (active)
                      Padding(
                        padding: const EdgeInsets.only(top: 4),
                        child: Text(
                          i18n.t('chat:videoIdentity.readyHint'),
                          style: TextStyle(
                              fontSize: FontSizes.xs, color: t.s800),
                        ),
                      ),
                    if (widget.material?.isActive ?? false)
                      Padding(
                        padding: const EdgeInsets.only(top: 4),
                        child: Text(
                          i18n.t('chat:videoIdentity.assetReady'),
                          style: TextStyle(
                              fontSize: FontSizes.xs, color: t.s800),
                        ),
                      ),
                    if (message != null && message.isNotEmpty)
                      Padding(
                        padding: const EdgeInsets.only(top: 6),
                        child: Text(
                          message,
                          style: TextStyle(
                            fontSize: FontSizes.xs,
                            height: 1.5,
                            color: t.danger,
                          ),
                        ),
                      ),
                  ],
                ),
              ),
            ],
          ),
          if (pending)
            Padding(
              padding: const EdgeInsets.only(top: 11),
              child: Wrap(
                spacing: 8,
                runSpacing: 8,
                children: [
                  if (_authorizationUrl != null)
                    FilledButton.icon(
                      onPressed: () => launchUrl(
                        Uri.parse(_authorizationUrl!),
                        mode: LaunchMode.externalApplication,
                      ),
                      icon: const Icon(Icons.open_in_new, size: 15),
                      label: Text(i18n.t('chat:videoIdentity.open'),
                          style: const TextStyle(fontSize: FontSizes.sm)),
                      style: FilledButton.styleFrom(
                        backgroundColor: t.ink,
                        foregroundColor: t.bg,
                        shape: RoundedRectangleBorder(
                          borderRadius: BorderRadius.circular(Radii.full),
                        ),
                      ),
                    ),
                  OutlinedButton.icon(
                    onPressed: _busy ? null : _refresh,
                    icon: _busy
                        ? SizedBox(
                            width: 14,
                            height: 14,
                            child: CircularProgressIndicator(
                                strokeWidth: 1.8, color: t.n600),
                          )
                        : const Icon(Icons.refresh, size: 15),
                    label: Text(
                      i18n.t(_busy
                          ? 'chat:videoIdentity.checking'
                          : 'chat:videoIdentity.completed'),
                      style: const TextStyle(fontSize: FontSizes.sm),
                    ),
                    style: OutlinedButton.styleFrom(
                      side: BorderSide(color: t.hair),
                      foregroundColor: t.n800,
                      shape: RoundedRectangleBorder(
                        borderRadius: BorderRadius.circular(Radii.full),
                      ),
                    ),
                  ),
                ],
              ),
            ),
        ],
      ),
    );
  }
}
