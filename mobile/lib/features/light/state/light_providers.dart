import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/api/auth_store.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/json.dart';
import '../../../shared/models/light.dart';
import '../../../shared/models/light_autopilot.dart';
import '../../../shared/models/session.dart';
import '../../workspace/state/active_workspace_store.dart';
import '../api/light_api.dart';

void _scope(Ref ref) {
  ref.watch(authProvider.select((a) => a.userId));
  ref.watch(
    activeWorkspaceProvider.select((value) => value.valueOrNull?.currentId),
  );
}

/// The home entries with availability for the active workspace. Refetched
/// on a workspace or account change, on pull-to-refresh and when the
/// interface language changes: the titles come from the server in the
/// request's `Accept-Language`, so the cards would otherwise keep the old
/// language until the next refresh.
final lightEntriesProvider = FutureProvider<LightEntriesView>((ref) {
  _scope(ref);
  ref.watch(i18nProvider.select((s) => s.language));
  return ref.watch(lightApiProvider).entries();
});

/// 省心托管 jobs of the active workspace; invalidated after every change.
final lightAutopilotsProvider =
    FutureProvider.autoDispose<List<LightAutopilot>>((ref) {
      _scope(ref);
      return ref.watch(lightApiProvider).autopilots();
    });

/// One entry by id, once the list has answered.
final lightEntryProvider = Provider.family<LightEntry?, String>(
  (ref, id) => ref.watch(lightEntriesProvider).valueOrNull?.byId(id),
);

/// 「最近做的」, polled while the home is on screen.
final lightRecentProvider = FutureProvider.autoDispose<List<LightRecentGroup>>((
  ref,
) {
  _scope(ref);
  final timer = Timer.periodic(
    const Duration(seconds: 30),
    (_) => ref.invalidateSelf(),
  );
  ref.onDispose(timer.cancel);
  return ref.watch(lightApiProvider).recent();
});

/// Presigned URL of a result for its thumbnail; cached well inside the
/// hour the link lives.
final lightAssetUrlProvider = FutureProvider.family<String?, String>((
  ref,
  assetId,
) async {
  final link = ref.keepAlive();
  final expiry = Timer(const Duration(minutes: 40), link.close);
  ref.onDispose(expiry.cancel);
  try {
    return await ref.watch(lightApiProvider).assetUrl(assetId);
  } catch (_) {
    return null;
  }
});

/// What `POST /api/light/start` returned, by session id, so the topic page
/// that opens right after has the entry, steps and opening chips without
/// waiting for the entries list.
final lightStartsProvider = StateProvider<Map<String, LightStartResult>>(
  (_) => const {},
);

/// One row of the 「对话」 tab's list: a conversation, with what the personal
/// assistant knows about it when it follows it.
class LightConversation {
  const LightConversation({
    required this.session,
    this.watched = false,
    this.summary,
  });

  final Session session;
  final bool watched;
  final String? summary;

  bool get isTopic => session.lightEntry != null;
}

/// The 「对话」 list (docs `LIGHT_MODE_IMPL_PLAN.md` §4.2 T6): the workspace's
/// conversations — topic chats and plain ones alike — newest first, with the
/// assistant's own view of them merged in where it answers.
final lightConversationsProvider =
    FutureProvider.autoDispose<List<LightConversation>>((ref) async {
      _scope(ref);
      final api = ref.watch(lightApiProvider);
      final sessions = await api.sessions();
      var followed = const <Map<String, dynamic>>[];
      try {
        followed = await api.assistantSessions();
      } catch (_) {
        // The assistant's read is a decoration on the list, not the list.
      }
      final byId = {for (final item in followed) ?asString(item['id']): item};
      final rows = [
        for (final session in sessions)
          if (session.kind != 'assistant' && !session.isCron)
            LightConversation(
              session: session,
              watched: byId[session.id]?['watched'] == true,
              summary: asString(byId[session.id]?['latest_summary']),
            ),
      ]..sort((a, b) => b.session.updatedAt.compareTo(a.session.updatedAt));
      return rows;
    });
