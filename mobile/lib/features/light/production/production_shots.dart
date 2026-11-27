import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:image_picker/image_picker.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/i18n/i18n.dart';
import '../api/light_api.dart';
import '../widgets/light_input_bar.dart';
import 'production_api.dart';
import 'production_media.dart';
import 'production_shot_models.dart';
import 'production_stages.dart' show productionErrorText;
import 'production_widgets.dart';

String _s(I18nState i18n, String key, [Map<String, Object?>? vars]) =>
    i18n.t('light:production.shots.$key', vars: vars);

String shotTitle(I18nState i18n, ProductionShot shot) => shot.title.isNotEmpty
    ? shot.title
    : _s(i18n, 'number', {'number': shot.index});

Widget _label(BuildContext context, String text, {double size = 17}) => Text(
  text,
  style: TextStyle(
    fontSize: size,
    fontWeight: FontWeight.w700,
    color: context.tokens.ink,
  ),
);

class _Card extends StatelessWidget {
  const _Card({required this.child, this.padding = const EdgeInsets.all(14)});
  final Widget child;
  final EdgeInsetsGeometry padding;

  @override
  Widget build(BuildContext context) => Container(
    padding: padding,
    decoration: BoxDecoration(
      color: Theme.of(context).cardColor,
      border: Border.all(color: context.tokens.hair),
      borderRadius: BorderRadius.circular(14),
    ),
    child: child,
  );
}

class ProductionShotList extends ConsumerWidget {
  const ProductionShotList({
    super.key,
    required this.task,
    this.onEdit,
    this.onHistory,
    this.onCompose,
  });

  final LightProduction task;
  final ValueChanged<ProductionShot>? onEdit;
  final VoidCallback? onHistory, onCompose;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final i18n = ref.watch(i18nProvider);
    final t = context.tokens;
    final workspace = task.shotWorkspace!;
    final current = workspace.current;
    final output = task.output;
    final progress = task.shotProgress;
    final total = progress?.total ?? workspace.shots.length;
    final done =
        progress?.filmed ??
        workspace.shots.where((s) => s.version != null).length;
    final composePhase = [
      'compose',
      'wan_delivery',
    ].contains(progress?.phase ?? task.phase);
    final editingPhase = (progress?.phase ?? task.phase) == 'wan_edit';
    return ListView(
      key: const Key('production-shot-list'),
      padding: const EdgeInsets.fromLTRB(16, 10, 16, 18),
      children: [
        if (task.working && output == null)
          Align(
            alignment: Alignment.center,
            child: _label(context, workspace.title, size: 25),
          )
        else
          _label(context, workspace.title, size: 27),
        const SizedBox(height: 18),
        if (output case final media?) ...[
          _MediaPreview(
            media: media,
            wideFallback: task.template == 'silk_road',
          ),
          Row(
            children: [
              Expanded(
                child: Text(
                  current != null
                      ? _s(i18n, 'currentNumber', {'number': current.number})
                      : _s(i18n, 'current'),
                  style: TextStyle(color: t.ink),
                ),
              ),
              TextButton(
                key: const Key('production-version-history'),
                onPressed: onHistory,
                child: Text(_s(i18n, 'history')),
              ),
            ],
          ),
        ],
        if (task.working || workspace.composing) ...[
          LinearProgressIndicator(
            key: const Key('production-progress'),
            value:
                composePhase ||
                    workspace.composing ||
                    editingPhase ||
                    total == 0
                ? null
                : (done / total).clamp(0.0, 1.0),
            color: t.a700,
            backgroundColor: t.n300,
            minHeight: 7,
            borderRadius: BorderRadius.circular(8),
          ),
          const SizedBox(height: 12),
          Row(
            children: [
              Expanded(
                child: _label(
                  context,
                  _s(
                    i18n,
                    workspace.composing
                        ? 'composingAgain'
                        : composePhase
                        ? 'composing'
                        : editingPhase
                        ? 'reviewing'
                        : 'generating',
                  ),
                ),
              ),
              if (!workspace.composing && !composePhase)
                Text('$done / $total', style: TextStyle(color: t.ink)),
            ],
          ),
          const SizedBox(height: 18),
        ],
        if (workspace.canCompose)
          Padding(
            padding: const EdgeInsets.only(bottom: 16),
            child: _Card(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  if (workspace.composeState == 'failed')
                    Text(
                      _s(i18n, 'composeFailed'),
                      style: TextStyle(color: t.dangerInk),
                    ),
                  Text(
                    workspace.pending.isEmpty
                        ? _s(i18n, 'titlePending')
                        : _s(i18n, 'pending', {
                            'count': workspace.pending.length,
                          }),
                    style: TextStyle(color: t.ink, fontWeight: FontWeight.w600),
                  ),
                  const SizedBox(height: 8),
                  ProductionPrimary(
                    key: const Key('production-review-compose'),
                    label: _s(i18n, 'recompose'),
                    onPressed: onCompose,
                  ),
                ],
              ),
            ),
          ),
        for (final shot in workspace.shots)
          Padding(
            padding: const EdgeInsets.only(bottom: 12),
            child: _ShotCard(
              key: ValueKey('production-shot-${shot.index}'),
              shot: shot,
              onDetail: shot.version != null && onEdit != null
                  ? () => onEdit!(shot)
                  : null,
              allowEditing: !workspace.active && !task.working,
            ),
          ),
      ],
    );
  }
}

class _MediaPreview extends StatelessWidget {
  const _MediaPreview({required this.media, required this.wideFallback});
  final ProductionMedia media;
  final bool wideFallback;

  @override
  Widget build(BuildContext context) {
    final ratio = media.aspectRatio ?? (wideFallback ? 2.55 : 9 / 16);
    return Center(
      child: ConstrainedBox(
        constraints: BoxConstraints(maxWidth: ratio >= 1 ? 640 : 244),
        child: AspectRatio(
          aspectRatio: ratio,
          child: ProductionResultPreviewButton(
            media: media,
            child: Stack(
              fit: StackFit.expand,
              children: [
                ProductionThumbnail(media: media, showVideoBadge: false),
                const Center(
                  child: Icon(
                    Icons.play_circle_fill,
                    color: Colors.white,
                    size: 46,
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

class _ShotCard extends ConsumerWidget {
  const _ShotCard({
    super.key,
    required this.shot,
    this.onDetail,
    required this.allowEditing,
  });
  final ProductionShot shot;
  final VoidCallback? onDetail;
  final bool allowEditing;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final i18n = ref.watch(i18nProvider);
    final t = context.tokens;
    final version = shot.version;
    final prompt = version?.prompt ?? shot.prompt;
    return _Card(
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            children: [
              Expanded(child: _label(context, shotTitle(i18n, shot))),
              const SizedBox(width: 8),
              Flexible(
                child: Align(
                  alignment: Alignment.centerRight,
                  child: DecoratedBox(
                    decoration: BoxDecoration(
                      color: shot.status == 'ready' && !shot.pending
                          ? t.sage.withValues(alpha: .13)
                          : Colors.transparent,
                      borderRadius: BorderRadius.circular(8),
                    ),
                    child: Padding(
                      padding: const EdgeInsets.symmetric(
                        horizontal: 8,
                        vertical: 3,
                      ),
                      child: Text(
                        _s(i18n, switch (shot.status) {
                          'ready' when shot.pending => 'newVersion',
                          'generating' => 'generatingStatus',
                          _ => shot.status,
                        }),
                        textAlign: TextAlign.end,
                        style: TextStyle(
                          color: shot.status == 'failed' ? t.dangerInk : t.a700,
                          fontSize: 12,
                        ),
                      ),
                    ),
                  ),
                ),
              ),
            ],
          ),
          const SizedBox(height: 10),
          _FramePair(
            first: version?.first ?? shot.first,
            last: version?.last ?? shot.last,
            waiting: shot.status == 'waiting' || shot.status == 'generating',
          ),
          if (version != null) ...[
            const SizedBox(height: 8),
            OutlinedButton.icon(
              key: ValueKey('production-play-shot-${shot.index}'),
              onPressed: () => openProductionMedia(context, version.media),
              icon: const Icon(Icons.play_arrow_rounded, size: 19),
              label: Text(_s(i18n, 'play')),
            ),
          ],
          if (prompt.isNotEmpty) ...[
            const SizedBox(height: 8),
            _PromptPanel(prompt: prompt),
          ],
          if (onDetail != null)
            Align(
              alignment: Alignment.centerRight,
              child: TextButton(
                key: ValueKey('production-edit-shot-${shot.index}'),
                onPressed: onDetail,
                child: Text(
                  _s(i18n, shot.editable && allowEditing ? 'edit' : 'detail'),
                ),
              ),
            ),
        ],
      ),
    );
  }
}

class _PromptPanel extends ConsumerStatefulWidget {
  const _PromptPanel({required this.prompt});
  final String prompt;

  @override
  ConsumerState<_PromptPanel> createState() => _PromptPanelState();
}

class _PromptPanelState extends ConsumerState<_PromptPanel> {
  bool _expanded = false;
  @override
  Widget build(BuildContext context) {
    final i18n = ref.watch(i18nProvider);
    final t = context.tokens;
    const actionMarker = '具体动作：';
    final actionAt = widget.prompt.indexOf(actionMarker);
    final summary = actionAt == -1
        ? widget.prompt
        : widget.prompt.substring(actionAt + actionMarker.length).trimLeft();
    return Material(
      color: t.hairSoft,
      borderRadius: BorderRadius.circular(12),
      child: InkWell(
        borderRadius: BorderRadius.circular(12),
        onTap: () => setState(() => _expanded = !_expanded),
        child: Padding(
          padding: const EdgeInsets.fromLTRB(11, 8, 10, 10),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Expanded(
                    child: Text(
                      _s(i18n, 'prompt'),
                      style: TextStyle(color: t.n800, fontSize: 12),
                    ),
                  ),
                  Text(
                    _s(i18n, _expanded ? 'collapse' : 'expand'),
                    style: TextStyle(color: t.n800, fontSize: 12),
                  ),
                  Icon(
                    _expanded ? Icons.expand_less : Icons.expand_more,
                    color: t.n800,
                    size: 17,
                  ),
                ],
              ),
              const SizedBox(height: 3),
              Text(
                _expanded ? widget.prompt : summary,
                maxLines: _expanded ? null : 1,
                overflow: _expanded
                    ? TextOverflow.visible
                    : TextOverflow.ellipsis,
                style: TextStyle(color: t.ink),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _FramePair extends ConsumerWidget {
  const _FramePair({
    this.first,
    this.last,
    this.waiting = false,
    this.onReplace,
    this.picked = const {},
    this.controls = false,
    this.drafts = const {},
  });
  final ShotFrame? first, last;
  final bool waiting, controls;
  final ValueChanged<bool>? onReplace;
  final Map<bool, _PickedFrame> picked;
  final Map<String, ShotFrameDraft> drafts;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final i18n = ref.watch(i18nProvider);
    final t = context.tokens;

    Widget tile(bool tail, ShotFrame? frame) {
      final pending = picked[tail];
      final state = drafts[shotFrameRole(tail)]?.status;
      final label = _s(
        i18n,
        frame?.source == 'reference' && pending == null
            ? (tail ? 'referenceLast' : 'referenceFirst')
            : (tail ? 'last' : 'first'),
      );
      final placeholder = ColoredBox(
        color: t.hairSoft,
        child: Center(
          child: Padding(
            padding: const EdgeInsets.all(6),
            child: Text(
              _s(
                i18n,
                waiting
                    ? (tail ? 'lastWaiting' : 'firstWaiting')
                    : 'unavailable',
              ),
              textAlign: TextAlign.center,
              style: TextStyle(fontSize: 11, color: t.n800),
            ),
          ),
        ),
      );
      Widget photo = placeholder;
      final url = pending?.media?.thumbnail ?? frame?.url;
      if (pending?.file case final file?) {
        photo = Image.file(
          File(file.path),
          fit: BoxFit.cover,
          errorBuilder: (_, _, _) => placeholder,
        );
      } else if (url != null) {
        photo = Image.network(
          url,
          fit: BoxFit.cover,
          frameBuilder: (_, child, frame, _) =>
              frame == null ? placeholder : child,
          errorBuilder: (_, _, _) => placeholder,
        );
      }
      return Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          if (controls && picked.isNotEmpty)
            SizedBox(
              height: 24,
              child: pending == null
                  ? null
                  : Align(
                      alignment: Alignment.centerRight,
                      child: Text(
                        _s(i18n, 'replaced'),
                        style: TextStyle(color: t.a700, fontSize: 12),
                      ),
                    ),
            ),
          ClipRRect(
            borderRadius: BorderRadius.circular(9),
            child: AspectRatio(
              aspectRatio: controls ? 2.35 : 1.85,
              child: Stack(
                fit: StackFit.expand,
                children: [
                  photo,
                  if (state == 'generating')
                    const ColoredBox(
                      color: Colors.black26,
                      child: Center(
                        child: CircularProgressIndicator(color: Colors.white),
                      ),
                    ),
                  if (!controls && url != null)
                    Positioned(
                      left: 5,
                      bottom: 5,
                      child: DecoratedBox(
                        decoration: BoxDecoration(
                          color: Colors.black54,
                          borderRadius: BorderRadius.circular(4),
                        ),
                        child: Padding(
                          padding: const EdgeInsets.symmetric(
                            horizontal: 5,
                            vertical: 2,
                          ),
                          child: Text(
                            label,
                            style: const TextStyle(
                              color: Colors.white,
                              fontSize: 11,
                            ),
                          ),
                        ),
                      ),
                    ),
                  if (pending != null)
                    DecoratedBox(
                      decoration: BoxDecoration(
                        border: Border.all(color: t.a700, width: 2),
                        borderRadius: BorderRadius.circular(9),
                      ),
                    ),
                ],
              ),
            ),
          ),
          if (controls)
            Row(
              children: [
                Expanded(
                  child: Text(
                    label,
                    style: TextStyle(color: t.ink, fontSize: 13),
                  ),
                ),
                if (onReplace != null)
                  TextButton(
                    key: ValueKey(
                      tail
                          ? 'production-replace-tail'
                          : 'production-replace-head',
                    ),
                    onPressed: state == 'generating'
                        ? null
                        : () => onReplace!(tail),
                    child: Text(
                      _s(
                        i18n,
                        state == 'generating' ? 'frameGenerating' : 'replace',
                      ),
                    ),
                  ),
              ],
            ),
          if (state == 'failed')
            Text(
              _s(i18n, 'frameFailed'),
              style: TextStyle(color: t.dangerInk, fontSize: 12),
            ),
        ],
      );
    }

    return Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Expanded(child: tile(false, first)),
        const SizedBox(width: 8),
        Expanded(child: tile(true, last)),
      ],
    );
  }
}

class _PickedFrame {
  _PickedFrame({this.file, this.media});
  final XFile? file;
  final ProductionMedia? media;
  String? uploaded;
}

enum _FrameSource { prompt, library, album }

class ProductionShotPage extends ConsumerStatefulWidget {
  const ProductionShotPage({
    super.key,
    required this.task,
    required this.shot,
    required this.version,
    this.editable = false,
  });

  final LightProduction task;
  final ProductionShot shot;
  final ShotVersion version;
  final bool editable;

  @override
  ConsumerState<ProductionShotPage> createState() => _ProductionShotPageState();
}

class _ProductionShotPageState extends ConsumerState<ProductionShotPage> {
  late LightProduction _task = widget.task;
  late final _prompt = TextEditingController(text: widget.version.prompt);
  final Map<bool, _PickedFrame> _frames = {};
  Timer? _framePoll;
  bool _refreshing = false;
  bool _expanded = true;
  bool _busy = false;
  String? _request;
  Object? _error;

  ProductionShot get _shot =>
      _task.shotWorkspace?.shots
          .where((s) => s.index == widget.shot.index)
          .firstOrNull ??
      widget.shot;

  bool get _generating =>
      widget.editable &&
      _shot.frameDrafts.values.any((draft) => draft.status == 'generating');

  @override
  void initState() {
    super.initState();
    if (widget.editable) {
      for (final tail in [false, true]) {
        if (_shot.draft(tail)?.media case final media?) {
          _frames[tail] = _PickedFrame(media: media);
        }
      }
    }
    _watchFrames();
  }

  void _watchFrames() {
    if (_generating) {
      _framePoll ??= Timer.periodic(const Duration(seconds: 2), (_) {
        if (!_busy) unawaited(_refreshFrames());
      });
    } else {
      _framePoll?.cancel();
      _framePoll = null;
    }
  }

  void _receive(LightProduction updated) {
    if (!mounted || updated.revision < _task.revision) return;
    setState(() {
      final prior = _shot;
      _task = updated;
      for (final tail in [false, true]) {
        final draft = _shot.draft(tail);
        if (draft?.media case final media?) {
          if (media.id != prior.draft(tail)?.media?.id) {
            _frames[tail] = _PickedFrame(media: media);
            _request = null;
          }
        }
      }
    });
    _watchFrames();
  }

  Future<void> _refreshFrames() async {
    if (_refreshing) return;
    _refreshing = true;
    try {
      final updated = await ref
          .read(lightProductionApiProvider)
          .get(_task.sessionId);
      _receive(updated);
    } catch (_) {
      // Transient refresh failures can recover on the next poll.
    } finally {
      _refreshing = false;
    }
  }

  Future<void> _change(
    Future<LightProduction> Function(LightProductionApi) run,
  ) async {
    if (_busy) return;
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      _receive(await run(ref.read(lightProductionApiProvider)));
    } catch (error) {
      if (!mounted) return;
      setState(() => _error = error);
      await _refreshFrames();
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _editFramePrompt(bool tail) async {
    final i18n = ref.read(i18nProvider);
    final draft = _shot.draft(tail);
    final role = shotFrameRole(tail);
    final choice = await showDialog<({String prompt, bool useCurrent})>(
      context: context,
      builder: (_) => _FramePromptDialog(
        title: _s(i18n, tail ? 'lastPrompt' : 'firstPrompt'),
        prompt: draft?.prompt ?? widget.version.framePrompts[role] ?? '',
        useCurrent: draft?.useCurrent ?? true,
        i18n: i18n,
      ),
    );
    if (!mounted || choice == null) return;
    await _change(
      (api) => api.generateFrame(
        _task,
        requestId: productionRequestId(),
        versionId: widget.version.id,
        role: role,
        prompt: choice.prompt,
        useCurrent: choice.useCurrent,
      ),
    );
  }

  Future<void> _choose(bool tail) async {
    final i18n = ref.read(i18nProvider);
    final source = await showModalBottomSheet<_FrameSource>(
      context: context,
      builder: (context) => SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            ListTile(
              key: const Key('production-edit-frame-prompt'),
              leading: const Icon(Icons.auto_awesome_outlined),
              title: Text(_s(i18n, 'editFramePrompt')),
              onTap: () => Navigator.pop(context, _FrameSource.prompt),
            ),
            ListTile(
              leading: const Icon(Icons.collections_outlined),
              title: Text(_s(i18n, 'library')),
              onTap: () => Navigator.pop(context, _FrameSource.library),
            ),
            ListTile(
              leading: const Icon(Icons.photo_outlined),
              title: Text(_s(i18n, 'album')),
              onTap: () => Navigator.pop(context, _FrameSource.album),
            ),
          ],
        ),
      ),
    );
    if (!mounted || source == null) return;
    if (source == _FrameSource.prompt) {
      await _editFramePrompt(tail);
      return;
    }
    try {
      _PickedFrame? picked;
      if (source == _FrameSource.library) {
        final choices = await showModalBottomSheet<List<ProductionMedia>>(
          context: context,
          isScrollControlled: true,
          builder: (_) =>
              const ProductionLibrary(imagesOnly: true, selected: []),
        );
        if (choices?.firstOrNull case final asset?) {
          picked = _PickedFrame(media: asset);
        }
      } else {
        final images = await ref
            .read(lightPhotoPickerProvider)
            .pick(camera: false, limit: 1);
        if (images.firstOrNull case final file?) {
          picked = _PickedFrame(file: file);
        }
      }
      if (mounted && picked != null) {
        setState(() {
          _frames[tail] = picked!;
          _request = null;
          _error = null;
        });
      }
    } catch (error) {
      if (mounted) setState(() => _error = error);
    }
  }

  Future<String?> _upload(_PickedFrame? frame) async {
    if (frame == null) return null;
    if (frame.media case final media?) return media.id;
    if (frame.uploaded case final id?) return id;
    final file = frame.file!;
    final name = file.name.toLowerCase();
    final mime = name.endsWith('.png')
        ? 'image/png'
        : name.endsWith('.webp')
        ? 'image/webp'
        : name.endsWith('.jpeg') || name.endsWith('.jpg')
        ? 'image/jpeg'
        : file.mimeType;
    if (!const {'image/jpeg', 'image/png', 'image/webp'}.contains(mime)) {
      throw FormatException(_s(ref.read(i18nProvider), 'supportedImages'));
    }
    final length = await file.length();
    return frame.uploaded = await ref
        .read(lightApiProvider)
        .uploadStream(
          name: file.name,
          mime: mime!,
          length: length,
          stream: file.openRead,
          sessionId: _task.sessionId,
        );
  }

  Future<void> _submit() async {
    if (_busy || _generating) return;
    FocusManager.instance.primaryFocus?.unfocus();
    setState(() {
      _busy = true;
      _error = null;
    });
    final api = ref.read(lightProductionApiProvider);
    try {
      final first = await _upload(_frames[false]);
      if (!mounted) return;
      final last = await _upload(_frames[true]);
      if (!mounted) return;
      final result = await api.shotAction(
        _task,
        'retake',
        _request ??= productionRequestId(),
        versionId: widget.version.id,
        prompt: _prompt.text.trim(),
        firstFrame: first,
        lastFrame: last,
      );
      if (mounted) Navigator.pop(context, result);
    } catch (error) {
      if (!mounted) return;
      setState(() => _error = error);
      try {
        _receive(await api.get(_task.sessionId));
      } catch (_) {}
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  void dispose() {
    _framePoll?.cancel();
    _prompt.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final changed =
        _frames.isNotEmpty ||
        _prompt.text.trim() != widget.version.prompt.trim();
    final historical = !widget.editable;
    return PopScope(
      canPop: !_busy,
      child: Scaffold(
        backgroundColor: t.bg,
        appBar: AppBar(
          title: Text(_s(i18n, 'detail')),
          centerTitle: false,
          actions: [
            if (widget.shot.versions.isNotEmpty && widget.editable)
              TextButton(
                onPressed: _busy
                    ? null
                    : () => Navigator.push<void>(
                        context,
                        MaterialPageRoute(
                          builder: (_) => ProductionVersionsPage(
                            task: _task,
                            only: widget.shot,
                          ),
                        ),
                      ),
                child: Text(_s(i18n, 'shotHistory')),
              ),
          ],
        ),
        body: SafeArea(
          top: false,
          child: Column(
            children: [
              Expanded(
                child: ListView(
                  padding: const EdgeInsets.fromLTRB(16, 12, 16, 24),
                  keyboardDismissBehavior:
                      ScrollViewKeyboardDismissBehavior.onDrag,
                  children: [
                    _label(context, shotTitle(i18n, widget.shot), size: 26),
                    const SizedBox(height: 16),
                    Material(
                      color: t.hairSoft,
                      borderRadius: BorderRadius.circular(12),
                      child: ListTile(
                        leading: const Icon(Icons.play_circle_outline),
                        title: Text(
                          _s(i18n, historical ? 'play' : 'playPrevious'),
                        ),
                        trailing: Text('v${widget.version.number}'),
                        onTap: () =>
                            openProductionMedia(context, widget.version.media),
                      ),
                    ),
                    const SizedBox(height: 19),
                    _label(context, _s(i18n, 'frames')),
                    const SizedBox(height: 9),
                    _FramePair(
                      first: widget.version.first,
                      last: widget.version.last,
                      controls: true,
                      picked: _frames,
                      drafts: historical ? const {} : _shot.frameDrafts,
                      onReplace:
                          historical || !widget.shot.frameEditable || _busy
                          ? null
                          : _choose,
                    ),
                    const SizedBox(height: 15),
                    for (final tail in [false, true])
                      if (widget.version.framePrompts[shotFrameRole(tail)]
                          case final prompt?)
                        ExpansionTile(
                          tilePadding: EdgeInsets.zero,
                          title: Text(
                            _s(i18n, tail ? 'lastPrompt' : 'firstPrompt'),
                          ),
                          children: [
                            Align(
                              alignment: Alignment.centerLeft,
                              child: Text(prompt),
                            ),
                          ],
                        ),
                    Row(
                      children: [
                        Expanded(
                          child: _label(context, _s(i18n, 'clipPrompt')),
                        ),
                        TextButton.icon(
                          key: const Key('production-shot-prompt-toggle'),
                          onPressed: () =>
                              setState(() => _expanded = !_expanded),
                          label: Text(
                            _s(i18n, _expanded ? 'collapse' : 'expand'),
                          ),
                          icon: Icon(
                            _expanded ? Icons.expand_less : Icons.expand_more,
                          ),
                          iconAlignment: IconAlignment.end,
                        ),
                      ],
                    ),
                    if (_expanded)
                      TextField(
                        key: const Key('production-shot-prompt-field'),
                        controller: _prompt,
                        onChanged: (_) => setState(() => _request = null),
                        readOnly: historical || _busy,
                        minLines: 3,
                        maxLines: 8,
                        maxLength: historical ? null : 20000,
                        decoration: InputDecoration(
                          counterText: '',
                          border: OutlineInputBorder(
                            borderRadius: BorderRadius.circular(12),
                          ),
                        ),
                      )
                    else
                      Text(
                        _prompt.text,
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                      ),
                    if (_shot.titleHidden case final bool hidden
                        when !historical)
                      SwitchListTile(
                        key: const Key('production-hide-shot-title'),
                        contentPadding: EdgeInsets.zero,
                        title: Text(_s(i18n, 'hideTitle')),
                        value: hidden,
                        onChanged: _busy
                            ? null
                            : (value) => _change(
                                (api) => api.shotAction(
                                  _task,
                                  'titles',
                                  productionRequestId(),
                                  versionId: widget.version.id,
                                  hideTitle: value,
                                ),
                              ),
                      ),
                    if (_error case final error?)
                      Padding(
                        padding: const EdgeInsets.only(top: 9),
                        child: Text(
                          error is FormatException
                              ? error.message
                              : productionErrorText(i18n, error),
                          style: TextStyle(color: t.dangerInk),
                        ),
                      ),
                  ],
                ),
              ),
              if (!historical)
                Padding(
                  padding: const EdgeInsets.fromLTRB(16, 6, 16, 8),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      if (_frames.isNotEmpty)
                        Padding(
                          padding: const EdgeInsets.only(bottom: 9),
                          child: Text(
                            _s(
                              i18n,
                              _frames.length == 2
                                  ? 'bothChanged'
                                  : _frames.containsKey(true)
                                  ? 'lastChanged'
                                  : 'firstChanged',
                            ),
                            textAlign: TextAlign.center,
                            style: TextStyle(color: t.n800, fontSize: 13),
                          ),
                        ),
                      ProductionPrimary(
                        key: const Key('production-retake-shot'),
                        label: _s(i18n, _busy ? 'submitting' : 'retake'),
                        onPressed:
                            changed &&
                                _prompt.text.trim().isNotEmpty &&
                                !_busy &&
                                !_generating
                            ? _submit
                            : null,
                      ),
                    ],
                  ),
                ),
            ],
          ),
        ),
      ),
    );
  }
}

class _FramePromptDialog extends StatefulWidget {
  const _FramePromptDialog({
    required this.title,
    required this.prompt,
    required this.useCurrent,
    required this.i18n,
  });

  final String title, prompt;
  final bool useCurrent;
  final I18nState i18n;

  @override
  State<_FramePromptDialog> createState() => _FramePromptDialogState();
}

class _FramePromptDialogState extends State<_FramePromptDialog> {
  late final _text = TextEditingController(text: widget.prompt);
  late bool _useCurrent = widget.useCurrent;

  @override
  void dispose() {
    _text.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final i18n = widget.i18n;
    return AlertDialog(
      title: Text(widget.title),
      scrollable: true,
      content: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          TextField(
            key: const Key('production-frame-prompt'),
            controller: _text,
            minLines: 4,
            maxLines: 7,
            maxLength: 2400,
            onChanged: (_) => setState(() {}),
            decoration: InputDecoration(
              border: const OutlineInputBorder(),
              hintText: _s(i18n, 'framePromptHint'),
              counterText: '',
            ),
          ),
          CheckboxListTile(
            key: const Key('production-use-current-frame'),
            contentPadding: EdgeInsets.zero,
            title: Text(_s(i18n, 'currentFrameReference')),
            subtitle: Text(_s(i18n, 'materialFrameReference')),
            value: _useCurrent,
            onChanged: (value) => setState(() => _useCurrent = value == true),
          ),
        ],
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.pop(context),
          child: Text(MaterialLocalizations.of(context).cancelButtonLabel),
        ),
        FilledButton(
          key: const Key('production-generate-frame'),
          onPressed: _text.text.trim().isEmpty
              ? null
              : () => Navigator.pop(context, (
                  prompt: _text.text.trim(),
                  useCurrent: _useCurrent,
                )),
          child: Text(_s(i18n, 'generateFrame')),
        ),
      ],
    );
  }
}

Future<bool?> confirmProductionAssembly(
  BuildContext context,
  I18nState i18n,
  ProductionShotWorkspace workspace,
) => showModalBottomSheet<bool>(
  context: context,
  isScrollControlled: true,
  useSafeArea: true,
  showDragHandle: true,
  builder: (context) => SafeArea(
    top: false,
    child: SingleChildScrollView(
      padding: const EdgeInsets.fromLTRB(16, 0, 16, 12),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        mainAxisSize: MainAxisSize.min,
        children: [
          _label(context, _s(i18n, 'confirmTitle'), size: 24),
          const SizedBox(height: 12),
          for (final shot in workspace.pending)
            Padding(
              padding: const EdgeInsets.only(bottom: 8),
              child: _Card(
                child: Row(
                  children: [
                    SizedBox(
                      width: 94,
                      height: 44,
                      child: ProductionThumbnail(media: shot.version!.media),
                    ),
                    const SizedBox(width: 12),
                    Expanded(child: Text(shotTitle(i18n, shot))),
                    Text(
                      'v${shot.versions.where((v) => v.id == workspace.current?.segments[shot.index]).firstOrNull?.number ?? 1} → v${shot.version!.number}',
                    ),
                  ],
                ),
              ),
            ),
          for (final shot in workspace.titleEdits)
            Padding(
              padding: const EdgeInsets.only(bottom: 8),
              child: _Card(
                child: Row(
                  children: [
                    Expanded(child: Text(shotTitle(i18n, shot))),
                    Text(
                      _s(
                        i18n,
                        shot.titleHidden == true ? 'hideTitle' : 'restoreTitle',
                      ),
                    ),
                  ],
                ),
              ),
            ),
          const SizedBox(height: 4),
          Text(
            _s(i18n, 'confirmCopy'),
            style: TextStyle(color: context.tokens.n800),
          ),
          const SizedBox(height: 17),
          ProductionPrimary(
            key: const Key('production-confirm-compose'),
            label: _s(i18n, 'recompose'),
            onPressed: () => Navigator.pop(context, true),
          ),
          const SizedBox(height: 8),
          OutlinedButton(
            key: const Key('production-defer-compose'),
            onPressed: () => Navigator.pop(context, false),
            child: Text(_s(i18n, 'later')),
          ),
        ],
      ),
    ),
  ),
);

class ProductionVersionsPage extends ConsumerWidget {
  const ProductionVersionsPage({super.key, required this.task, this.only});
  final LightProduction task;
  final ProductionShot? only;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final i18n = ref.watch(i18nProvider);
    final editor = task.shotWorkspace!;
    final localization = MaterialLocalizations.of(context);
    String stamp(DateTime? date) => date == null
        ? ''
        : '${localization.formatCompactDate(date.toLocal())} ${localization.formatTimeOfDay(TimeOfDay.fromDateTime(date.toLocal()))}';
    String duration(double seconds) {
      final whole = seconds.round();
      return '${(whole ~/ 60).toString().padLeft(2, '0')}:${(whole % 60).toString().padLeft(2, '0')}';
    }

    final shots = only == null
        ? [
            for (final shot in editor.shots)
              if (shot.versions.length > 1) shot,
            for (final shot in editor.shots)
              if (shot.versions.length == 1) shot,
          ]
        : [only!];
    return Scaffold(
      backgroundColor: context.tokens.bg,
      appBar: AppBar(
        title: Text(_s(i18n, only == null ? 'history' : 'shotHistory')),
        centerTitle: false,
      ),
      body: SafeArea(
        top: false,
        child: ListView(
          padding: const EdgeInsets.fromLTRB(16, 13, 16, 24),
          children: [
            _label(
              context,
              only == null ? editor.title : shotTitle(i18n, only!),
              size: 26,
            ),
            if (only == null) ...[
              const SizedBox(height: 20),
              _label(context, _s(i18n, 'films')),
              const SizedBox(height: 10),
              for (final film in editor.films)
                _VersionRow(
                  media: film.media,
                  title: _s(i18n, 'filmNumber', {'number': film.number}),
                  subtitle: [
                    stamp(film.createdAt),
                    if (film.duration > 0) duration(film.duration),
                  ].where((text) => text.isNotEmpty).join(' · '),
                  status: film.current ? _s(i18n, 'current') : null,
                  detail: [
                    for (final shot in editor.shots)
                      if (shot.versions.length > 1)
                        for (final version in shot.versions.where(
                          (v) => v.id == film.segments[shot.index],
                        ))
                          '${shotTitle(i18n, shot)} · v${version.number}',
                  ].join(' / '),
                  icon: Icons.play_circle_outline,
                  onPressed: () => openProductionMedia(context, film.media),
                ),
            ],
            const SizedBox(height: 20),
            _label(context, _s(i18n, 'shotVersions')),
            for (final shot in shots.where(
              (item) => item.versions.isNotEmpty,
            )) ...[
              if (only == null)
                Padding(
                  padding: const EdgeInsets.symmetric(vertical: 10),
                  child: _label(context, shotTitle(i18n, shot), size: 14),
                ),
              for (final version in shot.versions.reversed)
                _VersionRow(
                  media: version.media,
                  title: _s(i18n, 'shotNumber', {'number': version.number}),
                  subtitle: stamp(version.createdAt),
                  icon: Icons.chevron_right,
                  onPressed: () => Navigator.push<void>(
                    context,
                    MaterialPageRoute(
                      builder: (_) => ProductionShotPage(
                        task: task,
                        shot: shot,
                        version: version,
                      ),
                    ),
                  ),
                ),
            ],
          ],
        ),
      ),
    );
  }
}

class _VersionRow extends StatelessWidget {
  const _VersionRow({
    required this.media,
    required this.title,
    required this.subtitle,
    this.detail = '',
    this.status,
    required this.icon,
    required this.onPressed,
  });
  final ProductionMedia media;
  final String title, subtitle, detail;
  final String? status;
  final IconData icon;
  final VoidCallback onPressed;

  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.only(bottom: 8),
    child: _Card(
      padding: EdgeInsets.zero,
      child: InkWell(
        borderRadius: BorderRadius.circular(14),
        onTap: onPressed,
        child: Padding(
          padding: const EdgeInsets.all(10),
          child: Row(
            children: [
              SizedBox(
                width: 104,
                height: 48,
                child: ProductionThumbnail(media: media, showVideoBadge: false),
              ),
              const SizedBox(width: 10),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Wrap(
                      spacing: 8,
                      runSpacing: 4,
                      crossAxisAlignment: WrapCrossAlignment.center,
                      children: [
                        _label(context, title, size: 15),
                        if (status case final text?)
                          DecoratedBox(
                            decoration: BoxDecoration(
                              color: context.tokens.hairSoft,
                              borderRadius: BorderRadius.circular(9),
                            ),
                            child: Padding(
                              padding: const EdgeInsets.symmetric(
                                horizontal: 7,
                                vertical: 3,
                              ),
                              child: Text(
                                text,
                                style: TextStyle(
                                  color: context.tokens.ink,
                                  fontSize: 11,
                                ),
                              ),
                            ),
                          ),
                      ],
                    ),
                    if (subtitle.isNotEmpty)
                      Text(
                        subtitle,
                        style: TextStyle(
                          color: context.tokens.n800,
                          fontSize: 12,
                        ),
                      ),
                    if (detail.isNotEmpty)
                      Text(
                        detail,
                        style: TextStyle(
                          color: context.tokens.n800,
                          fontSize: 12,
                        ),
                      ),
                  ],
                ),
              ),
              const SizedBox(width: 5),
              Icon(icon, size: 21),
            ],
          ),
        ),
      ),
    ),
  );
}
