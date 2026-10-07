import '../models/memory_models.dart';
import '../models/wiki_models.dart';
import '../utils/knowledge_text.dart';

/// Everything the knowledge page shows for one scope and search, derived the
/// way the web's `useKnowledge` derives it. Reads only: building it never
/// calls a model or writes anything.
class KnowledgeModel {
  KnowledgeModel._({
    required this.memoryList,
    required this.moreMemories,
    required this.topics,
    required this.moreTopics,
    required this.files,
    required this.moreFiles,
    required this.cleanupPending,
    required this.topicsOf,
    required this.projects,
    required this.memoriesPending,
    required this.libraryPending,
    required this.documentsPending,
    required this.memoriesLoaded,
    required this.libraryLoaded,
    required this.documentsLoaded,
    required this.error,
  });

  factory KnowledgeModel.build({
    required String query,
    required Paged<MemoryRecord>? memories,
    required Paged<WikiSummary>? library,
    required Paged<KnowledgeDocument>? documents,
    required List<MemoryTopic> groups,
    required List<KnowledgeProject> projects,
    required bool memoriesPending,
    required bool libraryPending,
    required bool documentsPending,
    required Object? error,
  }) {
    final docs = documents?.items ?? const <KnowledgeDocument>[];
    // A document's sections are pages too; they belong under its file, not
    // among topics.
    final documentOf = <String, KnowledgeDocument>{
      for (final doc in docs)
        for (final id in doc.pageIds) id: doc,
    };
    final pages = library?.items ?? const <WikiSummary>[];
    final readAt = library?.fetchedAt;
    final topics = [
      ...pages.where((p) => !documentOf.containsKey(p.id) && p.bodyAvailable),
      ...pages.where(
        (p) =>
            !documentOf.containsKey(p.id) &&
            !p.bodyAvailable &&
            rebuilding(p, readAt),
      ),
    ];
    // A search also finds a file through the text of its pages.
    final textHits = {
      for (final page in pages)
        if (documentOf.containsKey(page.id)) documentOf[page.id]!.id,
    };
    final topicsOf = <String, List<MemoryTopic>>{};
    for (final group in groups) {
      for (final id in group.memoryIds) {
        (topicsOf[id] ??= []).add(group);
      }
    }
    // Earlier results stay up while a search loads; narrow them meanwhile.
    final memoryList = [
      ...?memories?.items.where((m) => matchesQuery(m.summary, query)),
    ]..sort(byRecent);
    return KnowledgeModel._(
      memoryList: memoryList,
      moreMemories: memories?.hasMore ?? false,
      topics: topics,
      moreTopics: library?.hasMore ?? false,
      files: [
        for (final doc in docs)
          if (query.isEmpty ||
              matchesQuery(doc.filename, query) ||
              textHits.contains(doc.id))
            doc,
      ],
      moreFiles: documents?.hasMore ?? false,
      cleanupPending: documents?.cleanupPending ?? 0,
      topicsOf: topicsOf,
      projects: projects,
      memoriesPending: memoriesPending,
      libraryPending: libraryPending,
      documentsPending: documentsPending,
      memoriesLoaded: memories != null,
      libraryLoaded: library != null,
      documentsLoaded: documents != null,
      error: error,
    );
  }

  final List<MemoryRecord> memoryList;
  final bool moreMemories;
  final List<WikiSummary> topics;
  final bool moreTopics;
  final List<KnowledgeDocument> files;
  final bool moreFiles;

  /// Deleted files whose originals are still being removed from storage.
  final int cleanupPending;
  final Map<String, List<MemoryTopic>> topicsOf;
  final List<KnowledgeProject> projects;

  /// Nothing to show yet: neither data nor an earlier result to stand in.
  final bool memoriesPending;
  final bool libraryPending;
  final bool documentsPending;
  final bool memoriesLoaded;
  final bool libraryLoaded;
  final bool documentsLoaded;
  final Object? error;

  bool get loading => memoriesPending || libraryPending || documentsPending;

  bool get empty => memoryList.isEmpty && topics.isEmpty && files.isEmpty;

  String? projectName(String? id) {
    if (id == null) return null;
    for (final project in projects) {
      if (project.id == id) return project.name;
    }
    return null;
  }

  /// The tab counts, with "+" while more are left on the server.
  Map<String, String?> get counts {
    String more(int count, bool hasMore) => '$count${hasMore ? '+' : ''}';
    return {
      'memories': memoriesLoaded ? more(memoryList.length, moreMemories) : null,
      'topics': libraryLoaded ? more(topics.length, moreTopics) : null,
      'files': documentsLoaded ? more(files.length, moreFiles) : null,
    };
  }
}
