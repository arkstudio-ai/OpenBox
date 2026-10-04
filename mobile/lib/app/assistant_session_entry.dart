import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../features/chat/api/assistant_api.dart';
import '../shared/i18n/i18n.dart';
import '../shared/models/session.dart';
import '../shared/router/paths.dart';
import '../shared/utils/error_text.dart';

typedef _Entry = ({AssistantScope scope, String sessionId});
final _entryProvider = FutureProvider.autoDispose.family<Session, _Entry>(
  (ref, key) =>
      ref.read(assistantApiProvider(key.scope)).entrySession(key.sessionId),
);

/// A legacy main-session URL must redirect before the ordinary controller or
/// its cached message bodies are ever mounted. This read is metadata only.
class AssistantSessionEntry extends ConsumerWidget {
  const AssistantSessionEntry({
    super.key,
    required this.sessionId,
    required this.builder,
  });
  final String sessionId;
  final WidgetBuilder builder;
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final scope = ref.watch(assistantScopeProvider);
    if (scope == null) return const Center(child: CircularProgressIndicator());
    final key = (scope: scope, sessionId: sessionId);
    return ref
        .watch(_entryProvider(key))
        .when(
          skipLoadingOnRefresh: false,
          loading: () => const Center(child: CircularProgressIndicator()),
          error: (error, _) => Center(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(errorText(ref.watch(i18nProvider), error)),
                TextButton(
                  onPressed: () => ref.invalidate(_entryProvider(key)),
                  child: Text(
                    ref.watch(i18nProvider).t('chat:assistant.reload'),
                  ),
                ),
              ],
            ),
          ),
          data: (session) {
            if (session.kind != 'assistant') return builder(context);
            WidgetsBinding.instance.addPostFrameCallback((_) {
              if (context.mounted && ref.read(assistantScopeProvider) == scope) {
                context.go(Paths.assistant);
              }
            });
            return const Center(child: CircularProgressIndicator());
          },
        );
  }
}
