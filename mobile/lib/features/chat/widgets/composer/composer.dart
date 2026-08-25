import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../../shared/api/containers_api.dart';
import '../../../../shared/appearance/tokens.dart';
import '../../../../shared/appearance/type_scale.dart';
import '../../../../shared/i18n/i18n.dart';
import '../../../../shared/models/session.dart';
import '../../api/mention_api.dart';
import '../../state/chat_session_controller.dart';
import '../../state/config_providers.dart';
import '../../utils/mention.dart';
import 'context_ring.dart';
import 'mention_menu.dart';
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

  // Mention menu state (web `useMentionMenu`).
  MentionTrigger? _trigger;
  String? _dismissedKey;
  Timer? _fileDebounce;
  String _fileQuery = '';
  List<String> _fileResults = const [];
  bool _fileLoading = false;

  bool get _canSend => _controller.text.trim().isNotEmpty && !_sending;

  @override
  void initState() {
    super.initState();
    _controller.addListener(_onComposerChanged);
  }

  @override
  void dispose() {
    _fileDebounce?.cancel();
    _controller.dispose();
    _focusNode.dispose();
    super.dispose();
  }

  void _onComposerChanged() {
    final caret = _controller.selection.baseOffset;
    final trigger =
        caret < 0 ? null : resolveTrigger(_controller.text, caret);
    if (trigger?.key != _trigger?.key) {
      setState(() => _trigger = trigger);
      _kickFileSearch(trigger);
    }
  }

  /// Debounced sandbox file search for `@` queries (web: 160ms).
  void _kickFileSearch(MentionTrigger? trigger) {
    _fileDebounce?.cancel();
    final containerId = ref.read(runningContainerProvider).valueOrNull?.id;
    final query = trigger?.query.trim() ?? '';
    if (trigger?.kind != MentionKind.at ||
        containerId == null ||
        query.isEmpty) {
      return;
    }
    setState(() => _fileLoading = true);
    _fileDebounce = Timer(const Duration(milliseconds: 160), () async {
      try {
        final files = await ref
            .read(containersApiProvider)
            .searchFiles(containerId, query);
        if (mounted && _trigger?.query.trim() == query) {
          setState(() {
            _fileQuery = query;
            _fileResults = files;
            _fileLoading = false;
          });
        }
      } catch (_) {
        if (mounted) setState(() => _fileLoading = false);
      }
    });
  }

  bool _matches(String query, String name, String? description) {
    final q = query.trim().toLowerCase();
    if (q.isEmpty) return true;
    return '$name ${description ?? ''}'.toLowerCase().contains(q);
  }

  List<MentionSectionData> _buildMentionSections(String? containerId) {
    final trigger = _trigger;
    if (trigger == null) return const [];
    final query = trigger.query;
    final skills = ref.watch(mentionSkillsProvider);
    final skillItems = [
      for (final s in skills.valueOrNull ?? const <MentionEntry>[])
        if (_matches(query, s.name, s.description))
          MentionItem(
            kind: 'skill',
            label: s.name,
            description: s.description,
            insert: '@skill:${s.name}',
          ),
    ];
    if (trigger.kind == MentionKind.at) {
      return [
        MentionSectionData(
          kind: 'files',
          needSandbox: containerId == null,
          loading: _fileLoading,
          items: [
            if (containerId != null && _fileQuery == query.trim())
              for (final path in _fileResults)
                MentionItem(kind: 'file', label: path, insert: '@$path'),
          ],
        ),
        MentionSectionData(
            kind: 'skills', loading: skills.isLoading, items: skillItems),
      ];
    }
    final commands = ref.watch(mentionCommandsProvider);
    return [
      MentionSectionData(
        kind: 'commands',
        loading: commands.isLoading,
        items: [
          for (final c in commands.valueOrNull ?? const <MentionEntry>[])
            if (_matches(query, c.name, c.description))
              MentionItem(
                kind: 'command',
                label: c.name,
                description: c.description,
                insert: '/${c.name} ',
              ),
        ],
      ),
      MentionSectionData(
          kind: 'skills', loading: skills.isLoading, items: skillItems),
    ];
  }

  void _selectMention(MentionItem item) {
    final trigger = _trigger;
    if (trigger == null) return;
    final next = replaceTrigger(_controller.text, trigger, item.insert);
    _controller.value = TextEditingValue(
      text: next.text,
      selection: TextSelection.collapsed(offset: next.caret),
    );
    setState(() {
      _dismissedKey = null;
      _trigger = null;
    });
    _focusNode.requestFocus();
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

    final containerId = ref.watch(runningContainerProvider).valueOrNull?.id;
    final mentionOpen = _trigger != null && _trigger!.key != _dismissedKey;

    return Container(
      margin: const EdgeInsets.fromLTRB(12, 4, 12, 8),
      clipBehavior: Clip.antiAlias,
      decoration: BoxDecoration(
        color: t.card,
        borderRadius: BorderRadius.circular(Radii.xl3),
        border: Border.all(color: _focusNode.hasFocus ? t.n400 : t.hair),
      ),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          if (mentionOpen)
            MentionMenu(
              sections: _buildMentionSections(containerId),
              onSelect: _selectMention,
            ),
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
