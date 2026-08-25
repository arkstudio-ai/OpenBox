import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../../shared/appearance/tokens.dart';
import '../../../../shared/appearance/type_scale.dart';
import '../../../../shared/i18n/i18n.dart';
import '../../../../shared/models/session.dart';
import '../../state/chat_session_controller.dart';
import '../../state/config_providers.dart';
import 'context_ring.dart';
import 'picker_sheets.dart';

/// The chat input (web `Composer.tsx`), mobile-optimized: rounded-3xl card
/// shell, chromeless auto-growing field, mode/model pickers, context ring,
/// morphing send/stop button.
class Composer extends ConsumerStatefulWidget {
  const Composer({
    super.key,
    required this.sessionKey,
    required this.busy,
    required this.onSend,
    this.session,
    this.onStop,
    this.autofocus = false,
  });

  /// Session id, or `draft` on the empty screen.
  final String sessionKey;

  final Session? session;
  final bool busy;
  final Future<void> Function(String text) onSend;
  final VoidCallback? onStop;
  final bool autofocus;

  @override
  ConsumerState<Composer> createState() => _ComposerState();
}

class _ComposerState extends ConsumerState<Composer> {
  final _controller = TextEditingController();
  final _focusNode = FocusNode();
  bool _sending = false;

  bool get _canSend => _controller.text.trim().isNotEmpty && !_sending;

  @override
  void dispose() {
    _controller.dispose();
    _focusNode.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    final text = _controller.text.trim();
    if (text.isEmpty || _sending) return;
    setState(() => _sending = true);
    try {
      await widget.onSend(text);
      _controller.clear();
    } finally {
      if (mounted) setState(() => _sending = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final config = ref.watch(appConfigProvider).valueOrNull;
    final pickedModel = ref.watch(pickedModelProvider(widget.sessionKey));
    final pickedAgent = ref.watch(pickedAgentProvider(widget.sessionKey));

    final activeModelId = pickedModel ??
        ((widget.session?.model.isNotEmpty ?? false)
            ? widget.session!.model
            : config?.defaultModel ?? '');
    final activeModel = config?.byId(activeModelId);
    final activeAgent =
        pickedAgent ?? widget.session?.agent ?? config?.defaultAgent ?? 'build';

    return Container(
      margin: const EdgeInsets.fromLTRB(12, 4, 12, 8),
      decoration: BoxDecoration(
        color: t.card,
        borderRadius: BorderRadius.circular(Radii.xl3),
        border: Border.all(color: _focusNode.hasFocus ? t.n400 : t.hair),
      ),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(18, 10, 18, 0),
            child: TextField(
              controller: _controller,
              focusNode: _focusNode,
              autofocus: widget.autofocus,
              minLines: 1,
              maxLines: 6,
              textInputAction: TextInputAction.newline,
              onChanged: (_) => setState(() {}),
              style: TextStyle(
                  fontSize: FontSizes.lg, height: 1.5, color: t.ink),
              decoration: InputDecoration(
                isDense: true,
                border: InputBorder.none,
                hintText: widget.busy
                    ? i18n.t('chat:composer.placeholderRunning')
                    : i18n.t('chat:composer.placeholder'),
                hintStyle: TextStyle(
                  fontSize: FontSizes.base,
                  color: t.n700,
                ),
              ),
            ),
          ),
          Padding(
            padding: const EdgeInsets.fromLTRB(10, 4, 8, 8),
            child: Row(
              children: [
                _pill(
                  t,
                  label: _agentDisplay(i18n, activeAgent),
                  icon: Icons.tune,
                  onTap: () => showModePicker(
                    context,
                    ref,
                    sessionKey: widget.sessionKey,
                    currentAgent: activeAgent,
                  ),
                ),
                const SizedBox(width: 6),
                _pill(
                  t,
                  label: activeModel?.name ??
                      (activeModelId.isEmpty ? '…' : activeModelId),
                  icon: Icons.workspaces_outline,
                  onTap: () => showModelPicker(
                    context,
                    ref,
                    sessionKey: widget.sessionKey,
                    currentModel: widget.session?.model,
                  ),
                ),
                const SizedBox(width: 8),
                if (widget.session?.tokenUsage != null && activeModel != null)
                  ContextRing(
                    used: widget.session!.tokenUsage!.context,
                    limit: activeModel.contextLimit ?? 0,
                  ),
                const Spacer(),
                _SendButton(
                  busy: widget.busy,
                  canSend: _canSend,
                  onSend: _submit,
                  onStop: widget.onStop,
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }

  String _agentDisplay(I18nState i18n, String agent) {
    final key = 'chat:mode.$agent';
    final label = i18n.t(key);
    return label == key ? agent : label;
  }

  Widget _pill(
    BossipTokens t, {
    required String label,
    required IconData icon,
    required VoidCallback onTap,
  }) {
    return Material(
      color: Colors.transparent,
      child: InkWell(
        borderRadius: BorderRadius.circular(Radii.full),
        onTap: onTap,
        child: Container(
          padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
          decoration: BoxDecoration(
            border: Border.all(color: t.hair),
            borderRadius: BorderRadius.circular(Radii.full),
          ),
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              Icon(icon, size: 13, color: t.n600),
              const SizedBox(width: 5),
              ConstrainedBox(
                constraints: const BoxConstraints(maxWidth: 110),
                child: Text(
                  label,
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(fontSize: FontSizes.xs, color: t.n700),
                ),
              ),
              const SizedBox(width: 2),
              Icon(Icons.expand_more, size: 13, color: t.n500),
            ],
          ),
        ),
      ),
    );
  }
}

/// Morphing round send/stop button (web `SendButton`): ArrowUp ↔ Square.
class _SendButton extends StatelessWidget {
  const _SendButton({
    required this.busy,
    required this.canSend,
    required this.onSend,
    this.onStop,
  });

  final bool busy;
  final bool canSend;
  final VoidCallback onSend;
  final VoidCallback? onStop;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final showStop = busy && onStop != null;
    final enabled = showStop || canSend;
    return Material(
      color: t.ink.withValues(alpha: enabled ? 1 : 0.4),
      shape: const CircleBorder(),
      child: InkWell(
        customBorder: const CircleBorder(),
        onTap: enabled ? (showStop ? onStop : onSend) : null,
        child: SizedBox(
          width: 40,
          height: 40,
          child: Icon(
            showStop ? Icons.stop_rounded : Icons.arrow_upward,
            size: 19,
            color: t.bg,
          ),
        ),
      ),
    );
  }
}
