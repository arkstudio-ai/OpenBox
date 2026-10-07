import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../shared/appearance/tokens.dart';
import '../../shared/i18n/i18n.dart';
import '../../shared/router/paths.dart';
import '../../shared/utils/error_text.dart';
import '../workspace/state/workspace_store.dart';
import 'memory_detail_page.dart';
import 'models/memory_models.dart';
import 'models/wiki_models.dart';
import 'state/document_upload.dart';
import 'state/knowledge_model.dart';
import 'state/knowledge_providers.dart';
import 'utils/knowledge_text.dart';
import 'widgets/knowledge_controls.dart';
import 'widgets/knowledge_menu.dart';
import 'widgets/knowledge_parts.dart';
import 'widgets/knowledge_views.dart';
import 'widgets/memory_dialogs.dart';
import 'widgets/processing_notice.dart';

/// 知识库 (web `KnowledgeHome`), re-flowed for a phone: what the assistant
/// remembers, the topics it organizes and the files it reads — one search,
/// one scope, one place to change any of it. The scope sits under the title,
/// adding and managing in the bar; sheets and dialogs stand in for the web's
/// side panel and modals.
class KnowledgeScreen extends ConsumerStatefulWidget {
  const KnowledgeScreen({
    super.key,
    this.initialView,
    this.initialProject,
    this.initialQuery,
  });

  /// `?view=`, `?project=` and `?q=`, as the web URL carries them.
  final String? initialView;
  final String? initialProject;
  final String? initialQuery;

  @override
  ConsumerState<KnowledgeScreen> createState() => _KnowledgeScreenState();
}

class _KnowledgeScreenState extends ConsumerState<KnowledgeScreen> {
  late String _view = readView(widget.initialView);
  late String _projectId = widget.initialProject ?? '';
  late final _search = TextEditingController(text: widget.initialQuery ?? '');
  late String _query = (widget.initialQuery ?? '').trim();
  Timer? _debounce;
  bool _uploading = false;
  String _uploadError = '';

  // Earlier results stay up while a new search in the same scope loads, so
  // typing never blanks the page. Remembered with the account, workspace and
  // project they were read for: they never stand in for another.
  (KnowledgeScope, String, Paged<MemoryRecord>)? _lastMemories;
  (KnowledgeScope, String, Paged<WikiSummary>)? _lastLibrary;

  KnowledgeQuery get _key => (projectId: _projectId, query: _query);

  @override
  void didUpdateWidget(covariant KnowledgeScreen old) {
    super.didUpdateWidget(old);
    // The same page re-opened with another link: follow the link.
    if (old.initialView != widget.initialView) {
      _view = readView(widget.initialView);
    }
    if (old.initialProject != widget.initialProject) {
      _projectId = widget.initialProject ?? '';
    }
  }

  @override
  void dispose() {
    _debounce?.cancel();
    _search.dispose();
    super.dispose();
  }

  void _onSearch(String value) {
    setState(() {});
    _debounce?.cancel();
    _debounce = Timer(const Duration(milliseconds: 250), () {
      if (mounted) setState(() => _query = value.trim());
    });
  }

  void _setView(String view) => setState(() => _view = view);

  /// Back to where the page was opened from, or to a new chat when it was
  /// opened directly.
  void _back() {
    if (context.canPop()) {
      context.pop();
    } else {
      context.go(Paths.app);
    }
  }

  Future<void> _upload() async {
    final files = await ref.read(documentPickerProvider)();
    if (files.isEmpty || !mounted) return;
    setState(() {
      _uploadError = '';
      _view = 'files';
    });
    await uploadDocuments(
      ref,
      files: files,
      projectId: _projectId,
      onPending: (pending) => setState(() => _uploading = pending),
      onError: (error) => setState(() => _uploadError = error),
      mounted: () => mounted,
    );
  }

  Future<void> _pickScope(List<KnowledgeProject> projects) async {
    final id = await showScopeSheet(
      context,
      ref.read(i18nProvider),
      projects: projects,
      projectId: _projectId,
    );
    if (id != null && mounted) setState(() => _projectId = id);
  }

  Future<void> _add({
    required bool canUpload,
    required List<KnowledgeProject> projects,
  }) async {
    final choice = await showAddSheet(
      context,
      ref.read(i18nProvider),
      canUpload: canUpload,
      uploading: _uploading,
    );
    if (!mounted) return;
    switch (choice) {
      case AddChoice.memory:
        await showMemoryEditor(
          context,
          projectId: _projectId,
          projects: projects,
        );
      case AddChoice.upload:
        await _upload();
      case null:
        break;
    }
  }

  void _openMemory(MemoryRecord memory, KnowledgeModel model) {
    Navigator.of(context).push(
      MaterialPageRoute<void>(
        fullscreenDialog: true,
        builder: (_) => MemoryDetailPage(
          memory: memory,
          scopeName: _scopeName(memory, model),
          projectId: _projectId,
          topics: model.topicsOf[memory.id] ?? const [],
          projects: model.projects,
        ),
      ),
    );
  }

  String _scopeName(MemoryRecord memory, KnowledgeModel model) =>
      memory.projectId != null
      ? model.projectName(memory.projectId) ?? ''
      : ref.read(i18nProvider).t('knowledge:personal');

  /// A new chat, filed under the scope in view (web `paths.newChat`).
  void _startChat() {
    if (_projectId.isNotEmpty) {
      ref.read(selectedProjectProvider.notifier).state = _projectId;
    }
    context.go(Paths.app);
  }

  /// The value to show for a list, standing in an earlier search's result
  /// from the same scope while this one loads.
  T? _withPlaceholder<T>(
    KnowledgeScope scope,
    AsyncValue<T> state,
    (KnowledgeScope, String, T)? last,
    void Function((KnowledgeScope, String, T)) remember,
  ) {
    final value = state.current;
    if (value != null) {
      remember((scope, _projectId, value));
      return value;
    }
    return last != null && last.$1 == scope && last.$2 == _projectId
        ? last.$3
        : null;
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final memoriesState = ref.watch(memoryListProvider(_key));
    final libraryState = ref.watch(wikiLibraryProvider(_key));
    final documentsState = ref.watch(knowledgeDocumentsProvider(_projectId));
    final processing = ref.watch(memoryProcessingProvider(_projectId)).current;
    final groups = ref.watch(memoryGroupsProvider(_projectId)).current;
    final projects = ref.watch(knowledgeProjectsProvider).current ?? const [];
    final canUpload = ref.watch(knowledgeUploadsProvider).current ?? false;
    // Read on open, so the manage sheet shows the setting straight away.
    ref.watch(memorySettingsProvider);
    // A finished save adds a memory; show it without waiting for the poll.
    ref.listen(memoryProcessingProvider(_projectId), (previous, next) {
      final before = previous?.current?.pending ?? 0;
      final after = next.current?.pending;
      if (after != null && after < before) ref.invalidate(memoryListProvider);
    });

    final scope = ref.watch(knowledgeScopeProvider);
    final memories = _withPlaceholder(
      scope,
      memoriesState,
      _lastMemories,
      (last) => _lastMemories = last,
    );
    final library = _withPlaceholder(
      scope,
      libraryState,
      _lastLibrary,
      (last) => _lastLibrary = last,
    );
    final documents = documentsState.current;
    Object? errorOf(AsyncValue<Object?> state) =>
        state.hasError ? state.error : null;
    final model = KnowledgeModel.build(
      query: _query,
      memories: memories,
      library: library,
      documents: documents,
      groups: groups ?? const [],
      projects: projects,
      memoriesPending: memories == null && !memoriesState.hasError,
      libraryPending: library == null && !libraryState.hasError,
      documentsPending: documents == null && !documentsState.hasError,
      error:
          errorOf(memoriesState) ??
          errorOf(libraryState) ??
          errorOf(documentsState),
    );
    final actions = KnowledgeActions(
      setView: _setView,
      scopeName: (memory) => _scopeName(memory, model),
      openMemory: (memory) => _openMemory(memory, model),
      editMemory: (memory) => showMemoryEditor(
        context,
        memory: memory,
        projectId: _projectId,
        projects: projects,
      ),
      forgetMemory: (memory) => showForgetDialog(context, memory),
      addMemory: () =>
          showMemoryEditor(context, projectId: _projectId, projects: projects),
      openPage: (id) => context.push(Paths.wikiPage(id, projectId: _projectId)),
      upload: _upload,
      startChat: _startChat,
      loadMoreMemories: () =>
          ref.read(memoryListProvider(_key).notifier).loadMore(),
      loadMoreTopics: () =>
          ref.read(wikiLibraryProvider(_key).notifier).loadMore(),
      loadMoreFiles: () =>
          ref.read(knowledgeDocumentsProvider(_projectId).notifier).loadMore(),
    );
    // A project whose name is not known yet shows no name rather than a
    // wrong one.
    final scopeLabel = _projectId.isEmpty
        ? i18n.t('knowledge:allScopes')
        : model.projectName(_projectId) ?? '';

    return Scaffold(
      backgroundColor: t.bg,
      appBar: AppBar(
        titleSpacing: 0,
        leading: BackButton(onPressed: _back),
        title: KnowledgeTitle(
          scope: scopeLabel,
          onPickScope: projects.isEmpty ? null : () => _pickScope(projects),
        ),
        actions: [
          IconButton(
            key: const ValueKey('knowledge-add'),
            tooltip: i18n.t('wiki:consumer.add'),
            icon: Icon(Icons.add, size: 24, color: t.ink),
            onPressed: () => _add(canUpload: canUpload, projects: projects),
          ),
          IconButton(
            key: const ValueKey('knowledge-manage'),
            tooltip: i18n.t('knowledge:manage.title'),
            icon: Icon(Icons.tune, size: 21, color: t.ink),
            onPressed: () => showKnowledgeMenu(
              context,
              ref,
              projectId: _projectId,
              scopeName: _projectId.isEmpty
                  ? null
                  : model.projectName(_projectId),
            ),
          ),
          const SizedBox(width: 6),
        ],
      ),
      body: RefreshIndicator(
        onRefresh: () async {
          refreshKnowledge(ref);
          try {
            await Future.wait<Object?>([
              ref.read(memoryListProvider(_key).future),
              ref.read(wikiLibraryProvider(_key).future),
              ref.read(knowledgeDocumentsProvider(_projectId).future),
            ]);
          } catch (_) {
            // A read that failed says so on the page itself.
          }
        },
        child: ListView(
          key: const ValueKey('knowledge-list'),
          physics: const AlwaysScrollableScrollPhysics(),
          keyboardDismissBehavior: ScrollViewKeyboardDismissBehavior.onDrag,
          padding: EdgeInsets.fromLTRB(
            16,
            4,
            16,
            32 + MediaQuery.paddingOf(context).bottom,
          ),
          children: [
            KnowledgeSearchField(controller: _search, onChanged: _onSearch),
            const SizedBox(height: 6),
            KnowledgeTabs(
              view: _view,
              counts: model.counts,
              onChanged: _setView,
            ),
            if (_view == 'overview' || _view == 'memories')
              ProcessingNotice(
                projectId: _projectId,
                processing: processing,
                onOpenChat: (id) => context.go(Paths.chat(id)),
              ),
            if (model.error != null)
              Padding(
                padding: const EdgeInsets.only(top: 12),
                child: KnowledgeBanner(
                  key: const ValueKey('knowledge-load-error'),
                  tone: BannerTone.danger,
                  leading: Icon(
                    Icons.error_outline,
                    size: 17,
                    color: t.dangerInk,
                  ),
                  text:
                      '${i18n.t('knowledge:loadFailed')} '
                      '${errorText(i18n, model.error!)}',
                  action: i18n.t('knowledge:retry'),
                  onAction: () => refreshKnowledge(ref),
                ),
              ),
            const SizedBox(height: 18),
            KnowledgeBody(
              view: _view,
              model: model,
              query: _query,
              canUpload: canUpload,
              uploading: _uploading,
              uploadError: _uploadError,
              loadingMore: (
                memories: memories?.loadingMore ?? false,
                topics: library?.loadingMore ?? false,
                files: documents?.loadingMore ?? false,
              ),
              actions: actions,
            ),
          ],
        ),
      ),
    );
  }
}
