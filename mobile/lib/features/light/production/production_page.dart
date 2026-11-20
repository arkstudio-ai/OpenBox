import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:image_picker/image_picker.dart';

import '../../../shared/api/api_error.dart';
import '../../../shared/appearance/tokens.dart';
import '../../../shared/download/asset_download.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/router/paths.dart';
import '../../../shared/widgets/toast.dart';
import '../api/light_api.dart';
import '../widgets/light_input_bar.dart';
import 'production_api.dart';
import 'production_media.dart';
import 'production_shot_models.dart';
import 'production_shots.dart';
import 'production_stages.dart';
import 'production_widgets.dart';

class ProductionPage extends ConsumerStatefulWidget {
  const ProductionPage({super.key, required this.sessionId, this.initial});

  final String sessionId;
  final LightProduction? initial;

  @override
  ConsumerState<ProductionPage> createState() => _ProductionPageState();
}

class _ProductionPageState extends ConsumerState<ProductionPage> {
  final _shop = TextEditingController();
  final _script = TextEditingController();
  final _productName = TextEditingController();
  final _pending = <String, Object?>{};
  LightProduction? _task;
  Future<bool>? _saving;
  Timer? _autosave;
  Timer? _poll;
  bool _busy = false;
  bool _refreshing = false;
  bool _advancedUpdated = false;
  bool _inAgent = false;
  bool _confirmingAssembly = false;
  double? _uploadProgress;
  Object? _error;
  int _snapshot = 0;
  ({String action, String id, int revision})? _request;

  LightProductionApi get _api => ref.read(lightProductionApiProvider);
  I18nState get _i18n => ref.read(i18nProvider);
  bool get _imagesOnly => _task?.kind == ProductionKind.menu;
  List<ProductionDish> get _dishes =>
      _pending['dishes'] is List<Map<String, String>>
      ? (_pending['dishes']! as List<Map<String, String>>)
            .map(ProductionDish.fromJson)
            .toList()
      : _task?.dishes ?? const [];

  @override
  void initState() {
    super.initState();
    final initial = widget.initial;
    if (initial != null && initial.sessionId == widget.sessionId) {
      _readTask(initial);
    }
    unawaited(_refresh());
    _poll = Timer.periodic(const Duration(seconds: 3), (_) {
      if ((_task?.working == true || _task?.shotWorkspace?.active == true) &&
          !_inAgent &&
          !_busy &&
          _saving == null) {
        unawaited(_refresh());
      }
    });
  }

  void _readTask(LightProduction task) {
    if ((_task?.revision ?? 0) > task.revision) return;
    _task = task;
    if (!_pending.containsKey('shop') && _shop.text.trim() != task.shop) {
      _shop.text = task.shop;
    }
    if (!_pending.containsKey('product_name') &&
        _productName.text.trim() != task.productName) {
      _productName.text = task.productName;
    }
    final field = task.remake ? 'reference_copy' : 'script';
    final copy = task.remake ? task.referenceCopy : task.script;
    if (!_pending.containsKey(field) && _script.text.trim() != copy) {
      _script.text = copy;
    }
  }

  void _receive(LightProduction task) {
    if (mounted) {
      final offerAssembly =
          _task?.shotWorkspace?.active == true &&
          _task?.shotWorkspace?.composing != true &&
          task.shotWorkspace?.canCompose == true;
      setState(() {
        _snapshot++;
        _readTask(task);
        _error = null;
      });
      if (offerAssembly) {
        WidgetsBinding.instance.addPostFrameCallback((_) {
          if (mounted && ModalRoute.of(context)?.isCurrent == true) {
            unawaited(_confirmAssembly());
          }
        });
      }
    }
  }

  Future<void> _shotDetail(ProductionShot shot) async {
    final task = _task!;
    final version = shot.version;
    if (version == null) return;
    final updated = await Navigator.push<LightProduction>(
      context,
      MaterialPageRoute(
        builder: (_) => ProductionShotPage(
          task: task,
          shot: shot,
          version: version,
          editable:
              shot.editable &&
              task.shotWorkspace?.active == false &&
              !task.working,
        ),
      ),
    );
    if (!mounted) return;
    if (updated == null) {
      await _refresh();
      return;
    }
    _receive(updated);
    if (updated.shotWorkspace?.canCompose == true) await _confirmAssembly();
  }

  void _versionHistory() {
    final task = _task!;
    unawaited(
      Navigator.push<void>(
        context,
        MaterialPageRoute(builder: (_) => ProductionVersionsPage(task: task)),
      ),
    );
  }

  Future<void> _confirmAssembly() async {
    final snapshot = _task!;
    final work = snapshot.shotWorkspace;
    if (_confirmingAssembly || _busy || work?.canCompose != true) return;
    _confirmingAssembly = true;
    try {
      final accepted = await confirmProductionAssembly(context, _i18n, work!);
      if (!mounted || accepted != true) return;
      await _perform(() async {
        final versions = work.pending.map((shot) => shot.version!.id).toList();
        _receive(
          await _api.shotAction(
            snapshot,
            'compose',
            productionRequestId(),
            versions: versions,
          ),
        );
      });
    } finally {
      _confirmingAssembly = false;
    }
  }

  Future<void> _refresh({bool alert = false}) async {
    if (_refreshing) return;
    _refreshing = true;
    final snapshot = _snapshot;
    try {
      final task = await _api.get(widget.sessionId);
      if (_saving == null && _snapshot == snapshot) _receive(task);
    } catch (error) {
      if (mounted) {
        if (_task == null) {
          setState(() => _error = error);
        } else if (alert) {
          _notify(error);
        }
      }
    } finally {
      _refreshing = false;
    }
  }

  void _notify(Object error) =>
      ref.read(toastProvider.notifier).error(productionErrorText(_i18n, error));

  void _stage(String key, Object? value) {
    if (!mounted) return;
    setState(() => _pending[key] = value);
    _autosave?.cancel();
    _autosave = Timer(
      const Duration(milliseconds: 480),
      () => unawaited(_flush()),
    );
  }

  Future<bool> _flush() {
    _autosave?.cancel();
    return _saving ??= _saveEdits().whenComplete(() => _saving = null);
  }

  Future<bool> _saveEdits() async {
    while (mounted && _pending.isNotEmpty && _task != null) {
      final changes = Map<String, Object?>.of(_pending);
      try {
        final saved = await _api.edit(_task!, changes);
        if (!mounted) return false;
        for (final change in changes.entries) {
          if (_pending[change.key] == change.value) _pending.remove(change.key);
        }
        _receive(saved);
      } catch (error) {
        if (mounted) {
          if (apiErrorOf(error)?.code == 'LIGHT_PRODUCTION_CHANGED') {
            // Keep the user's unsaved text; obtain the new revision before another explicit save.
            try {
              _receive(await _api.get(widget.sessionId));
            } catch (_) {}
          }
          if (mounted) {
            setState(() => _error = error);
            _notify(error);
          }
        }
        return false;
      }
    }
    return mounted;
  }

  Future<void> _perform(Future<void> Function() work) async {
    if (_busy || _task == null) return;
    setState(() => _busy = true);
    try {
      if (await _flush() && mounted) await work();
    } catch (error) {
      if (mounted) {
        _notify(error);
        await _refresh();
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _send(String action) async {
    final task = _task!;
    final previous = _request;
    final same =
        previous?.action == action && previous?.revision == task.revision;
    final id = same ? previous!.id : productionRequestId();
    _request = (action: action, id: id, revision: task.revision);
    final updated = await _api.action(task, action, id);
    _request = null;
    _receive(updated);
  }

  Future<void> _goTo(String page) async =>
      _receive(await _api.edit(_task!, {'page': page}));

  Future<void> _next({bool skip = false}) => _perform(() async {
    final task = _task!;
    FocusManager.instance.primaryFocus?.unfocus();
    switch (task.page) {
      case 'materials':
        await _goTo('style');
      case 'style' when task.kind == ProductionKind.footage:
        await _send('render');
      case 'style':
        _receive(
          await _api.edit(task, {'page': 'review', if (skip) 'template': ''}),
        );
        if (!mounted) return;
        final review = _task!;
        final menuNeedsReading =
            review.kind == ProductionKind.menu && review.materials.isNotEmpty;
        final needsCopy = review.remake
            ? !review.remakePrepared
            : review.kind != ProductionKind.menu && review.script.isEmpty;
        if (menuNeedsReading || (needsCopy && !review.manualPreparation)) {
          await _send('prepare');
        }
      default:
        await _send(
          task.manualPreparation && !task.remakePrepared ? 'prepare' : 'render',
        );
    }
  });

  void _leave() {
    if (context.canPop()) {
      context.pop();
    } else {
      context.go(Paths.home);
    }
  }

  Future<void> _back() async {
    if (_task == null) {
      _leave();
      return;
    }
    await _perform(() async {
      final task = _task!;
      if (task.status != 'draft' || task.page == 'materials') {
        _leave();
      } else {
        await _goTo(task.page == 'review' ? 'style' : 'materials');
      }
    });
  }

  Future<void> _advanced() async {
    if (_busy || _inAgent) return;
    _inAgent = true;
    try {
      if (!await _flush() || !mounted) return;
      final revision = _task!.agentRevision;
      final outputId = _task!.output?.id;
      await context.push(Paths.lightProductionAgent(widget.sessionId));
      if (!mounted) return;
      await _refresh(alert: true);
      if (mounted &&
          (_task!.agentRevision > revision || _task!.output?.id != outputId)) {
        setState(() => _advancedUpdated = true);
      }
    } finally {
      _inAgent = false;
    }
  }

  Future<void> _library() => _perform(() async {
    final selected = await showModalBottomSheet<List<String>>(
      context: context,
      isScrollControlled: true,
      showDragHandle: true,
      builder: (_) =>
          ProductionLibrary(imagesOnly: _imagesOnly, selected: _task!.assets),
    );
    if (selected != null && mounted) {
      _receive(await _api.edit(_task!, {'asset_ids': selected}));
    }
  });

  Future<void> _pick({required bool camera}) => _perform(() async {
    final picker = ref.read(lightPhotoPickerProvider);
    final limit = 10 - _task!.assets.length;
    if (limit <= 0) return;
    final List<XFile> files;
    if (camera && !_imagesOnly) {
      final video = await showModalBottomSheet<bool>(
        context: context,
        showDragHandle: true,
        builder: (context) => SafeArea(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              ListTile(
                leading: const Icon(Icons.photo_camera_outlined),
                title: Text(_i18n.t('light:production.takePhoto')),
                onTap: () => Navigator.pop(context, false),
              ),
              ListTile(
                leading: const Icon(Icons.videocam_outlined),
                title: Text(_i18n.t('light:production.takeVideo')),
                onTap: () => Navigator.pop(context, true),
              ),
            ],
          ),
        ),
      );
      if (video == null || !mounted) return;
      if (video) {
        final clip = await ImagePicker().pickVideo(source: ImageSource.camera);
        files = [?clip];
      } else {
        files = await picker.pick(camera: true, limit: 1);
      }
    } else {
      files = _imagesOnly
          ? await picker.pick(camera: camera, limit: limit)
          : await picker.pickMedia(limit: limit);
    }
    if (files.isNotEmpty && mounted) await _upload(files);
  });

  Future<void> _upload(List<XFile> files) async {
    final ids = [..._task!.assets];
    final api = ref.read(lightApiProvider);
    setState(() => _uploadProgress = 0);
    try {
      for (final (index, file) in files.indexed) {
        final length = await file.length();
        if (length > lightMaxClipBytes) {
          if (mounted) {
            ref
                .read(toastProvider.notifier)
                .error(_i18n.t('light:production.fileTooLarge'));
          }
          continue;
        }
        final name = file.name.toLowerCase();
        final mime =
            file.mimeType ??
            (lightIsVideo(file)
                ? (name.endsWith('.mov') ? 'video/quicktime' : 'video/mp4')
                : name.endsWith('.png')
                ? 'image/png'
                : name.endsWith('.heic')
                ? 'image/heic'
                : 'image/jpeg');
        final id = await api.uploadStream(
          name: file.name,
          mime: mime,
          length: length,
          stream: file.openRead,
          sessionId: widget.sessionId,
          onProgress: (fraction) {
            if (mounted) {
              setState(
                () => _uploadProgress = (index + fraction) / files.length,
              );
            }
          },
        );
        ids.add(id);
      }
    } finally {
      if (mounted) {
        setState(() => _uploadProgress = null);
        if (ids.length != _task!.assets.length) {
          _stage('asset_ids', ids);
          await _flush();
        }
      }
    }
  }

  Future<void> _saveOutput() => _perform(() async {
    final file = _task!.output!;
    await saveAssetToDevice(
      ref.read,
      assetId: file.id,
      name: file.name,
      mimeType: file.mime,
      target: AssetSaveTarget.album,
    );
  });

  String _text(String key, {Map<String, Object?>? vars}) =>
      _i18n.t('light:production.$key', vars: vars);

  @override
  Widget build(BuildContext context) {
    ref.watch(i18nProvider);
    final task = _task;
    final t = context.tokens;
    return PopScope(
      canPop: !_busy && _pending.isEmpty,
      onPopInvokedWithResult: (didPop, _) {
        if (!didPop && !_busy) unawaited(_back());
      },
      child: Scaffold(
        backgroundColor: t.bg,
        appBar: AppBar(
          backgroundColor: t.bg,
          surfaceTintColor: Colors.transparent,
          titleSpacing: 0,
          centerTitle: false,
          leading: BackButton(
            key: const Key('production-back'),
            onPressed: _busy ? null : _back,
          ),
          title: Text(
            task?.kind.label(_i18n) ?? _text('history'),
            style: const TextStyle(fontSize: 18, fontWeight: FontWeight.w700),
          ),
          actions: [
            if (task != null)
              TextButton(
                key: const Key('production-advanced'),
                onPressed: _busy ? null : _advanced,
                child: Text(_text('advanced'), style: TextStyle(color: t.n800)),
              ),
          ],
        ),
        body: SafeArea(
          top: false,
          child: task == null
              ? ProductionLoading(error: _error, onRetry: _refresh)
              : Column(
                  children: [
                    Expanded(
                      child: switch (task.status) {
                        'working' || 'completed'
                            when task.shotWorkspace != null =>
                          ProductionShotList(
                            task: task,
                            onEdit: task.working ? null : _shotDetail,
                            onHistory: _versionHistory,
                            onCompose: _confirmAssembly,
                          ),
                        'working' => ProductionProgress(task: task),
                        'completed' when task.output != null =>
                          ProductionResult(task: task),
                        'review' ||
                        'blocked' ||
                        'failed' => ProductionDecision(task: task),
                        _ => _form(task),
                      },
                    ),
                    if (task.status != 'working')
                      Padding(
                        padding: const EdgeInsets.fromLTRB(16, 8, 16, 8),
                        child: _footer(task),
                      ),
                  ],
                ),
        ),
      ),
    );
  }

  Widget _form(LightProduction task) => ListView(
    key: ValueKey(task.page),
    padding: const EdgeInsets.fromLTRB(16, 10, 16, 20),
    keyboardDismissBehavior: ScrollViewKeyboardDismissBehavior.onDrag,
    children: [
      if (_advancedUpdated) ...[
        Align(
          alignment: Alignment.centerLeft,
          child: Chip(
            avatar: const Icon(Icons.check_circle_outline, size: 16),
            label: Text(_text('updatedFromChat')),
          ),
        ),
        const SizedBox(height: 10),
      ],
      switch (task.page) {
        'style' => Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            ProductionHeading(
              _text('style.${task.kind.id}'),
              description: _text('styleBody.${task.kind.id}'),
            ),
            const SizedBox(height: 24),
            ProductionTemplates(
              task: task,
              selected: _pending['template'] as String? ?? task.template,
              onChoose: (value) {
                if (!_busy) _stage('template', value);
              },
            ),
            if (task.kind == ProductionKind.footage) ...[
              const SizedBox(height: 12),
              _originalAudio(task),
              _creativeEffects(task),
            ],
          ],
        ),
        'review' => _review(task),
        _ => _materials(task),
      },
      if (_error != null) ...[
        const SizedBox(height: 10),
        Text(
          productionErrorText(_i18n, _error!),
          style: TextStyle(color: context.tokens.dangerInk),
        ),
        TextButton(
          onPressed: _busy ? null : _flush,
          child: Text(_text('saveDraft')),
        ),
      ],
    ],
  );

  Widget _materials(LightProduction task) => Column(
    crossAxisAlignment: CrossAxisAlignment.stretch,
    children: [
      ProductionHeading(
        _text('materials.${task.kind.id}'),
        description: _text('materialsBody.${task.kind.id}'),
      ),
      if (task.brief.isNotEmpty) ...[
        const SizedBox(height: 12),
        Text(task.brief),
      ],
      const SizedBox(height: 18),
      TextField(
        key: const Key('production-shop'),
        controller: _shop,
        maxLength: 80,
        enabled: !_busy,
        textInputAction: TextInputAction.done,
        onChanged: (value) => _stage('shop', value),
        decoration: InputDecoration(labelText: _text('shop'), counterText: ''),
      ),
      if (task.kind == ProductionKind.menu) ...[
        const SizedBox(height: 14),
        ProductionDishes(
          dishes: _dishes,
          onChange: (dishes) =>
              _stage('dishes', dishes.map((dish) => dish.toJson()).toList()),
        ),
        const SizedBox(height: 20),
        Text(_text('fromPhotos')),
      ],
      const SizedBox(height: 14),
      if (task.kind == ProductionKind.video && task.materials.isEmpty)
        _videoMaterialSources()
      else
        Row(
          children: [
            _source('library', Icons.photo_library_outlined, _library),
            const SizedBox(width: 8),
            _source(
              task.kind == ProductionKind.menu ? 'takePhoto' : 'camera',
              Icons.photo_camera_outlined,
              () => _pick(camera: true),
            ),
            const SizedBox(width: 8),
            _source(
              'upload',
              Icons.file_upload_outlined,
              () => _pick(camera: false),
            ),
          ],
        ),
      const SizedBox(height: 16),
      if (_uploadProgress case final fraction?) ...[
        LinearProgressIndicator(value: fraction),
        const SizedBox(height: 8),
      ],
      if (task.materials.isNotEmpty)
        ProductionSelectionGrid(
          materials: task.materials,
          onRemove: _busy
              ? null
              : (item) => _perform(
                  () async => _receive(
                    await _api.edit(_task!, {
                      'asset_ids': _task!.assets
                          .where((id) => id != item.id)
                          .toList(),
                    }),
                  ),
                ),
        ),
      if (task.assets.length != task.materials.length)
        TextButton(
          onPressed: _busy
              ? null
              : () => _perform(
                  () async => _receive(
                    await _api.edit(_task!, {
                      'asset_ids': task.materials.map((m) => m.id).toList(),
                    }),
                  ),
                ),
          child: Text(_text('removeMissing')),
        ),
      if (task.materials.isNotEmpty) ...[
        const SizedBox(height: 12),
        Text(_text('selected', vars: {'count': task.materials.length})),
      ],
    ],
  );

  Widget _videoMaterialSources() => Column(
    crossAxisAlignment: CrossAxisAlignment.stretch,
    children: [
      _materialSourceCard(
        'upload',
        Icons.add_photo_alternate_outlined,
        () => _pick(camera: false),
        featured: true,
      ),
      const SizedBox(height: 10),
      Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Expanded(
            child: _materialSourceCard(
              'library',
              Icons.photo_library_outlined,
              _library,
            ),
          ),
          const SizedBox(width: 10),
          Expanded(
            child: _materialSourceCard(
              'camera',
              Icons.photo_camera_outlined,
              () => _pick(camera: true),
            ),
          ),
        ],
      ),
    ],
  );

  Widget _materialSourceCard(
    String method,
    IconData icon,
    VoidCallback tap, {
    bool featured = false,
  }) {
    final t = context.tokens;
    final label = Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          _text(method),
          style: Theme.of(context).textTheme.titleSmall?.copyWith(
            fontWeight: FontWeight.w700,
            color: t.ink,
          ),
        ),
        const SizedBox(height: 4),
        Text(
          _text('materialSource.$method'),
          style: Theme.of(
            context,
          ).textTheme.bodySmall?.copyWith(color: t.n800, height: 1.35),
        ),
      ],
    );
    return OutlinedButton(
      key: Key('production-source-$method'),
      onPressed: _busy ? null : tap,
      style: OutlinedButton.styleFrom(
        alignment: Alignment.centerLeft,
        backgroundColor: featured ? t.surface : t.card,
        minimumSize: Size(0, featured ? 102 : 112),
        padding: const EdgeInsets.all(16),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(16)),
      ),
      child: featured
          ? Row(
              children: [
                DecoratedBox(
                  decoration: BoxDecoration(
                    color: t.card,
                    borderRadius: BorderRadius.circular(12),
                  ),
                  child: Padding(
                    padding: const EdgeInsets.all(10),
                    child: Icon(icon, size: 24, color: t.a700),
                  ),
                ),
                const SizedBox(width: 13),
                Expanded(child: label),
                const SizedBox(width: 6),
                Icon(Icons.add, size: 21, color: t.ink),
              ],
            )
          : Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Icon(icon, size: 21, color: t.a700),
                const SizedBox(height: 9),
                label,
              ],
            ),
    );
  }

  Widget _source(String key, IconData icon, VoidCallback tap) => Expanded(
    child: OutlinedButton(
      key: Key('production-source-$key'),
      onPressed: _busy ? null : tap,
      style: OutlinedButton.styleFrom(
        padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 10),
        backgroundColor: context.tokens.card,
        minimumSize: const Size(44, 48),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(13)),
      ),
      child: Wrap(
        alignment: WrapAlignment.center,
        crossAxisAlignment: WrapCrossAlignment.center,
        spacing: 5,
        children: [
          Icon(icon, size: 17),
          Text(_text(key), textAlign: TextAlign.center),
        ],
      ),
    ),
  );

  Widget _originalAudio(LightProduction task) => SwitchListTile.adaptive(
    contentPadding: EdgeInsets.zero,
    title: Text(_text('keepOriginal')),
    value: _pending['keep_original'] as bool? ?? task.keepOriginal,
    onChanged: _busy ? null : (value) => _stage('keep_original', value),
  );

  Widget _creativeEffects(LightProduction task) => SwitchListTile.adaptive(
    key: const Key('production-creative-effects'),
    contentPadding: EdgeInsets.zero,
    title: Text(_text('creativeEffects')),
    subtitle: Text(_text('creativeEffectsHint')),
    value: _pending['creative_effects'] as bool? ?? task.creativeEffects,
    onChanged: _busy ? null : (value) => _stage('creative_effects', value),
  );

  Widget _review(LightProduction task) {
    final menu = task.kind == ProductionKind.menu;
    final summary = [
      task.shop,
      if (task.materials.isNotEmpty)
        _text('selected', vars: {'count': task.materials.length}),
      if (task.template.isNotEmpty)
        productionTemplateLabel(_i18n, task.template),
      if (task.supportsVideoTiers(task.template) && task.videoTier != null)
        "${_text('videoTier.title')} ${task.videoTier!.label(_i18n)}",
    ].where((value) => value.isNotEmpty).join(' · ');
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        ProductionHeading(_text('review.${task.kind.id}')),
        if (summary.isNotEmpty)
          Padding(
            padding: const EdgeInsets.only(top: 8, bottom: 12),
            child: Align(
              alignment: Alignment.centerLeft,
              child: ProductionTag(text: summary),
            ),
          ),
        if (task.materials.isNotEmpty) ...[
          ProductionMediaStrip(media: task.materials),
          const SizedBox(height: 16),
        ],
        if (task.template == 'silk_road') ...[
          TextField(
            key: const Key('production-product-name'),
            controller: _productName,
            enabled: !_busy,
            maxLength: 36,
            onChanged: (value) => _stage('product_name', value),
            decoration: InputDecoration(
              labelText: _text('productName'),
              hintText: task.suggestedProduct.isEmpty
                  ? _text('productNameHint')
                  : task.suggestedProduct,
              counterText: '',
            ),
          ),
          SwitchListTile.adaptive(
            key: const Key('production-beat-music'),
            contentPadding: EdgeInsets.zero,
            title: Text(_text('beatMusic')),
            value: _pending['beat_music'] as bool? ?? task.beatMusic,
            onChanged: _busy ? null : (value) => _stage('beat_music', value),
          ),
        ],
        if (menu)
          ProductionDishes(
            dishes: _dishes,
            onChange: (dishes) =>
                _stage('dishes', dishes.map((dish) => dish.toJson()).toList()),
          )
        else if (task.remake)
          ProductionRemakeCopy(
            wanStory: task.wanRemake,
            controller: _script,
            enabled: !_busy,
            onChanged: (value) => _stage('reference_copy', value),
          )
        else
          ProductionCopyField(
            controller: _script,
            enabled: !_busy,
            hint: _text('scriptPlaceholder'),
            onChanged: (value) => _stage('script', value),
          ),
        if (task.kind == ProductionKind.footage) ...[
          _originalAudio(task),
          _creativeEffects(task),
        ],
        if (!menu || task.materials.isNotEmpty) ...[
          const SizedBox(height: 12),
          Align(
            alignment: Alignment.center,
            child: OutlinedButton.icon(
              key: const Key('production-optimize'),
              onPressed: _busy
                  ? null
                  : () {
                      FocusManager.instance.primaryFocus?.unfocus();
                      unawaited(_perform(() => _send('prepare')));
                    },
              icon: const Icon(Icons.refresh),
              label: Text(
                _text(
                  menu
                      ? 'recognize'
                      : !task.remakePrepared && _script.text.trim().isEmpty
                      ? 'draftCopy'
                      : 'optimize',
                ),
              ),
            ),
          ),
          if (task.phase == 'prepare_failed')
            Text(_text('prepareFailed'), textAlign: TextAlign.center),
        ],
        if (task.remakePrepared && !_pending.containsKey('reference_copy'))
          ProductionRemakeScript(task.script),
        if (task.notes.isNotEmpty)
          ListTile(
            contentPadding: EdgeInsets.zero,
            onTap: _advanced,
            leading: const Icon(Icons.tune_outlined),
            trailing: const Icon(Icons.chevron_right),
            title: Text(_text('advanced')),
            subtitle: Text(
              task.notes,
              maxLines: 2,
              overflow: TextOverflow.ellipsis,
            ),
          ),
      ],
    );
  }

  Widget _footer(LightProduction task) {
    final ready = !_busy;
    Widget primary(String label, VoidCallback? action) => ProductionPrimary(
      key: const Key('production-primary'),
      label: _text(label),
      onPressed: ready ? action : null,
    );
    Widget secondary(String label, VoidCallback action) => TextButton(
      onPressed: ready && task.shotWorkspace?.active != true ? action : null,
      child: Text(_text(label)),
    );
    return switch (task.status) {
      'completed' => Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          primary(
            task.output?.video == true ? 'saveVideo' : 'saveMenu',
            _saveOutput,
          ),
          Row(
            mainAxisAlignment: MainAxisAlignment.spaceEvenly,
            children: [
              secondary(
                'editResult',
                task.kind == ProductionKind.footage
                    ? _advanced
                    : () => _perform(() => _send('edit')),
              ),
              secondary(
                'another',
                () => context.pushReplacement(
                  Paths.lightProductionStart(task.kind.id),
                ),
              ),
            ],
          ),
        ],
      ),
      'review' when task.proposal != null => Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          primary('apply', () => _perform(() => _send('apply'))),
          secondary('dismiss', () => _perform(() => _send('dismiss'))),
        ],
      ),
      'review' || 'blocked' => primary('advanced', _advanced),
      'failed' => Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          primary('retry', () => _perform(() => _send('render'))),
          secondary('editDraft', () => _perform(() => _send('edit'))),
        ],
      ),
      _ => switch (task.page) {
        'materials' => primary(
          'next',
          _canMoveFromMaterials(task) ? _next : null,
        ),
        'style' when task.kind == ProductionKind.video => Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            if (task.supportsVideoTiers(
              _pending['template'] as String? ?? task.template,
            )) ...[
              ProductionTierPicker(
                selected:
                    ProductionVideoTier.fromId(_pending['video_tier']) ??
                    task.videoTier,
                available: task.availableVideoTiers,
                onChoose: _busy
                    ? null
                    : (tier) => _stage('video_tier', tier.name),
              ),
              const SizedBox(height: 12),
            ],
            primary(
              'next',
              (_pending['template'] as String? ?? task.template).isEmpty
                  ? null
                  : _next,
            ),
            secondary('skip', () => _next(skip: true)),
          ],
        ),
        'style' when task.kind == ProductionKind.footage => primary(
          'generate.footage_edit',
          _canRender(task) ? _next : null,
        ),
        'style' => primary('next', _next),
        _ => primary(
          task.manualPreparation && !task.remakePrepared
              ? 'draftCopy'
              : 'generate.${task.kind.id}',
          _canRender(task) ? _next : null,
        ),
      },
    };
  }

  bool _canRender(LightProduction task) => switch (task.kind) {
    ProductionKind.video => task.remake || _script.text.trim().isNotEmpty,
    ProductionKind.footage => task.materials.isNotEmpty,
    ProductionKind.menu => _dishes.isNotEmpty,
  };

  bool _canMoveFromMaterials(LightProduction task) => switch (task.kind) {
    ProductionKind.menu => _dishes.isNotEmpty || task.materials.isNotEmpty,
    ProductionKind.footage => task.materials.isNotEmpty,
    ProductionKind.video =>
      task.materials.isNotEmpty ||
          _shop.text.trim().isNotEmpty ||
          task.brief.isNotEmpty,
  };

  @override
  void dispose() {
    _autosave?.cancel();
    _poll?.cancel();
    _shop.dispose();
    _script.dispose();
    _productName.dispose();
    super.dispose();
  }
}
