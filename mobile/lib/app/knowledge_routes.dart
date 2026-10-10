import 'package:go_router/go_router.dart';

import '../features/memory/knowledge_screen.dart';
import '../features/memory/wiki/topic_page.dart';
import '../shared/router/paths.dart';

/// 知识库 routes, the web's paths: `/app/wiki` is the page (`?view=`,
/// `?project=`, `?q=`), `/app/wiki/:pageId` one topic or document page, and
/// `/app/memory` — kept for old links — lands on its memories.
final List<RouteBase> knowledgeRoutes = [
  GoRoute(
    path: Paths.memory,
    redirect: (context, state) => Paths.wiki(
      projectId: state.uri.queryParameters['project'],
      view: 'memories',
    ),
  ),
  GoRoute(
    path: '/app/wiki',
    builder: (context, state) => KnowledgeScreen(
      initialView: state.uri.queryParameters['view'],
      initialProject: state.uri.queryParameters['project'],
      initialQuery: state.uri.queryParameters['q'],
    ),
  ),
  GoRoute(
    path: '/app/wiki/:pageId',
    builder: (context, state) => TopicPage(
      pageId: state.pathParameters['pageId'] ?? '',
      projectId: state.uri.queryParameters['project'] ?? '',
    ),
  ),
];
