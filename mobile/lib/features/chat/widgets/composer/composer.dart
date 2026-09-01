import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../../shared/api/containers_api.dart';
import '../../../../shared/appearance/tokens.dart';
import '../../../../shared/appearance/type_scale.dart';
import '../../../../shared/i18n/i18n.dart';
import '../../../../shared/models/resource.dart';
import '../../../../shared/models/session.dart';
import '../../../../shared/utils/format.dart';
import '../../api/mention_api.dart';
import '../../state/chat_session_controller.dart';
import '../../state/config_providers.dart';
import '../../utils/mention.dart';
import 'context_ring.dart';
import 'mention_menu.dart';
import 'picker_sheets.dart';
import 'resource_slot.dart';

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
    this.projectId,
    this.onStop,
    this.autofocus = false,
    this.resources,
  });

  /// Session id, or `draft` on the empty screen.
  final String sessionKey;

  final Session? session;
  final String? projectId;
  final bool busy;

  /// [attachments] are OSS asset ids the backend pulls into the sandbox
  /// before the run starts.
  final Future<void> Function(String text, List<String> attachments) onSend;
  final VoidCallback? onStop;
  final bool autofocus;

  /// Resource centre, injected by the app layer (see [ComposerResourceSlot]).
  /// Without it the "@" menu falls back to sandbox files and skills only.
  final ComposerResourceSlot? resources;

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

  /// Resources pinned to the next message. Already in OSS, so nothing
  /// transfers here — the message only has to name them.
  final _attachments = <Resource>[];
  bool _uploading = false;

  bool get _canSend =>
      (_controller.text.trim().isNotEmpty || _attachments.isNotEmpty) &&
      !_sending &&
      !_uploading;

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
    final trigger = caret < 0 ? null : resolveTrigger(_controller.text, caret);
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
            .searchFiles(
              containerId,
              query,
              sessionId: widget.sessionKey == 'draft'
                  ? null
                  : widget.sessionKey,
              projectId: widget.projectId ?? widget.session?.projectId,
            );
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
          kind: 'skills',
          loading: skills.isLoading,
          items: skillItems,
        ),
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
        kind: 'skills',
        loading: skills.isLoading,
        items: skillItems,
      ),
    ];
  }

  /// Picking a resource attaches it: drop the trigger span entirely (the
  /// normal replace always leaves a trailing space, which would strand one
  /// mid-sentence) and pin the file.
  void _attachResource(Resource resource) {
    final trigger = _trigger;
    final text = _controller.text;
    if (trigger != null) {
      final next =
          text.substring(0, trigger.start) + text.substring(trigger.end);
      _controller.value = TextEditingValue(
        text: next,
        selection: TextSelection.collapsed(offset: trigger.start),
      );
    }
    setState(() {
      if (!_attachments.any((r) => r.id == resource.id)) {
        _attachments.add(resource);
      }
      _dismissedKey = null;
      _trigger = null;
    });
    _focusNode.requestFocus();
  }

  /// "+ → resource centre" types the "@" the menu keys off, so browsing and
  /// typing share one code path (web `insertMentionTrigger`).
  void _openResourceMenu() {
    final text = _controller.text;
    final caret = _controller.selection.baseOffset;
    final at = caret < 0 ? text.length : caret.clamp(0, text.length);
    final needsSpace = at > 0 && !RegExp(r'\s').hasMatch(text[at - 1]);
    final insert = needsSpace ? ' @' : '@';
    _controller.value = TextEditingValue(
      text: text.substring(0, at) + insert + text.substring(at),
      selection: TextSelection.collapsed(offset: at + insert.length),
    );
    _focusNode.requestFocus();
  }

  Future<void> _uploadAndAttach() async {
    final slot = widget.resources;
    if (slot == null) return;
    setState(() => _uploading = true);
    try {
      final landed = await slot.pickAndUpload(
        context,
        projectId: widget.session?.projectId,
      );
      if (!mounted) return;
      setState(() => _attachments.addAll(landed));
    } finally {
      if (mounted) setState(() => _uploading = false);
    }
  }

  Future<void> _showToolsMenu() async {
    final i18n = ref.read(i18nProvider);
    final t = context.tokens;
    final choice = await showModalBottomSheet<String>(
      context: context,
      builder: (sheetContext) => SafeArea(
        child: ListView(
          shrinkWrap: true,
          padding: const EdgeInsets.symmetric(vertical: 8),
          children: [
            ListTile(
              dense: true,
              leading: Icon(Icons.layers_outlined, size: 18, color: t.n600),
              title: Text(
                i18n.t('chat:composer.resourceCenter'),
                style: TextStyle(fontSize: FontSizes.base, color: t.ink),
              ),
              onTap: () => Navigator.pop(sheetContext, 'resources'),
            ),
            ListTile(
              dense: true,
              leading: Icon(Icons.upload_outlined, size: 18, color: t.n600),
              title: Text(
                i18n.t('chat:composer.uploadFile'),
                style: TextStyle(fontSize: FontSizes.base, color: t.ink),
              ),
              onTap: () => Navigator.pop(sheetContext, 'upload'),
            ),
          ],
        ),
      ),
    );
    if (!mounted) return;
    if (choice == 'resources') _openResourceMenu();
    if (choice == 'upload') await _uploadAndAttach();
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
    if ((text.isEmpty && _attachments.isEmpty) || _sending) return;
    setState(() => _sending = true);
    try {
      await widget.onSend(text, [for (final r in _attachments) r.id]);
      // Only now: a rejected send — a quota, a dropped connection — must not
      // eat what the person typed, with an empty box reading as success.
      _controller.clear();
      if (mounted) setState(_attachments.clear);
    } catch (_) {
      // The send path already reported it; the draft above is the remedy.
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

    final activeModelId =
        pickedModel ??
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
              // The resource centre leads the menu: referencing a file
              // someone already has beats searching the sandbox for one, and
              // it is the only source that survives a recycled container.
              leading: widget.resources == null
                  ? null
                  : widget.resources!.mentionSection(
                      context,
                      query: _trigger?.query ?? '',
                      projectId: widget.session?.projectId,
                      onPick: _attachResource,
                    ),
            ),
          if (_attachments.isNotEmpty || _uploading)
            _AttachmentStrip(
              attachments: _attachments,
              uploading: _uploading,
              onRemove: (resource) =>
                  setState(() => _attachments.remove(resource)),
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
                fontSize: FontSizes.lg,
                height: 1.5,
                color: t.ink,
              ),
              decoration: InputDecoration(
                isDense: true,
                border: InputBorder.none,
                hintText: widget.busy
                    ? i18n.t('chat:composer.placeholderRunning')
                    : i18n.t('chat:composer.placeholder'),
                hintStyle: TextStyle(fontSize: FontSizes.base, color: t.n700),
              ),
            ),
          ),
          Padding(
            padding: const EdgeInsets.fromLTRB(10, 4, 8, 8),
            child: Row(
              children: [
                if (widget.resources != null) ...[
                  IconButton(
                    onPressed: _showToolsMenu,
                    icon: Icon(Icons.add, size: 20, color: t.n700),
                    tooltip: i18n.t('chat:composer.tools'),
                    visualDensity: VisualDensity.compact,
                    constraints: const BoxConstraints.tightFor(
                      width: 32,
                      height: 32,
                    ),
                    padding: EdgeInsets.zero,
                  ),
                  const SizedBox(width: 2),
                ],
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
                  label:
                      activeModel?.name ??
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

/// Pinned resources above the field (web `AttachmentRow`). These are already
/// in OSS, so a chip appears the moment one is picked — nothing to transfer.
class _AttachmentStrip extends StatelessWidget {
  const _AttachmentStrip({
    required this.attachments,
    required this.uploading,
    required this.onRemove,
  });

  final List<Resource> attachments;
  final bool uploading;
  final ValueChanged<Resource> onRemove;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return SizedBox(
      height: 56,
      child: ListView(
        scrollDirection: Axis.horizontal,
        padding: const EdgeInsets.fromLTRB(12, 10, 12, 0),
        children: [
          if (uploading)
            Container(
              width: 56,
              margin: const EdgeInsets.only(right: 8),
              decoration: BoxDecoration(
                border: Border.all(color: t.hair),
                borderRadius: BorderRadius.circular(Radii.lg),
              ),
              child: const Center(
                child: SizedBox(
                  width: 16,
                  height: 16,
                  child: CircularProgressIndicator(strokeWidth: 2),
                ),
              ),
            ),
          for (final resource in attachments)
            Container(
              margin: const EdgeInsets.only(right: 8),
              padding: const EdgeInsets.fromLTRB(8, 0, 4, 0),
              constraints: const BoxConstraints(maxWidth: 190),
              decoration: BoxDecoration(
                color: t.n200,
                border: Border.all(color: t.hair),
                borderRadius: BorderRadius.circular(Radii.lg),
              ),
              child: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  if (resource.kind == 'image' && resource.url.isNotEmpty)
                    ClipRRect(
                      borderRadius: BorderRadius.circular(Radii.sm),
                      child: Image.network(
                        resource.url,
                        width: 26,
                        height: 26,
                        fit: BoxFit.cover,
                        errorBuilder: (_, _, _) =>
                            Icon(Icons.image_outlined, size: 15, color: t.n600),
                      ),
                    )
                  else
                    Icon(
                      Icons.insert_drive_file_outlined,
                      size: 15,
                      color: t.n600,
                    ),
                  const SizedBox(width: 7),
                  Flexible(
                    child: Column(
                      mainAxisAlignment: MainAxisAlignment.center,
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          resource.name,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: TextStyle(
                            fontSize: FontSizes.xs,
                            fontWeight: FontWeight.w500,
                            color: t.ink,
                          ),
                        ),
                        Text(
                          formatBytes(resource.size),
                          style: TextStyle(
                            fontSize: FontSizes.xs2,
                            color: t.n600,
                          ),
                        ),
                      ],
                    ),
                  ),
                  IconButton(
                    onPressed: () => onRemove(resource),
                    icon: Icon(Icons.close, size: 13, color: t.n600),
                    visualDensity: VisualDensity.compact,
                    constraints: const BoxConstraints.tightFor(
                      width: 26,
                      height: 26,
                    ),
                    padding: EdgeInsets.zero,
                  ),
                ],
              ),
            ),
        ],
      ),
    );
  }
}
