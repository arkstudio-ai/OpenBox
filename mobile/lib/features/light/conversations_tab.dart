import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../shared/appearance/tokens.dart';
import '../../shared/appearance/type_scale.dart';
import '../../shared/i18n/i18n.dart';
import '../../shared/router/paths.dart';
import '../../shared/utils/error_text.dart';
import '../../shared/utils/format.dart';
import '../../shared/widgets/section_tabs.dart';
import 'state/light_providers.dart';
import 'widgets/entry_card.dart';

/// The 「对话」 tab (docs `LIGHT_MODE_PLAN.md` §4.7 #3, impl §4.2 T6): the
/// personal assistant itself, and beside it one list of every conversation
/// — the topic chats the home started and the plain ones — newest first.
/// The assistant pane is built by the app layer (it needs the scope, the
/// voice button and the task sheet) and handed in.
class LightConversationsTab extends ConsumerStatefulWidget {
  const LightConversationsTab({
    super.key,
    required this.title,
    required this.assistant,
    this.actions = const [],
  });

  final String title;
  final Widget assistant;
  final List<Widget> actions;

  @override
  ConsumerState<LightConversationsTab> createState() =>
      _LightConversationsTabState();
}

class _LightConversationsTabState extends ConsumerState<LightConversationsTab> {
  String _segment = 'assistant';

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return Scaffold(
      backgroundColor: t.bg,
      appBar: AppBar(
        automaticallyImplyLeading: false,
        titleSpacing: 16,
        title: Text(
          widget.title,
          maxLines: 1,
          overflow: TextOverflow.ellipsis,
          style: TextStyle(
            fontSize: FontSizes.lg,
            fontWeight: FontWeight.w500,
            color: t.ink,
          ),
        ),
        actions: [...widget.actions, const SizedBox(width: 4)],
      ),
      body: Column(
        children: [
          SectionTabs(
            labels: {
              'assistant': i18n.t('light:conversations.assistant'),
              'all': i18n.t('light:conversations.all'),
            },
            value: _segment,
            onChanged: (value) => setState(() => _segment = value),
          ),
          Expanded(
            child: IndexedStack(
              index: _segment == 'assistant' ? 0 : 1,
              children: [
                widget.assistant,
                const LightConversationList(),
              ],
            ),
          ),
        ],
      ),
    );
  }
}

class LightConversationList extends ConsumerWidget {
  const LightConversationList({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final rows = ref.watch(lightConversationsProvider);
    return RefreshIndicator(
      onRefresh: () async {
        ref.invalidate(lightConversationsProvider);
        try {
          await ref.read(lightConversationsProvider.future);
        } catch (_) {
          // The list shows its own error state.
        }
      },
      child: rows.when(
        loading: () =>
            const Center(child: CircularProgressIndicator(strokeWidth: 2)),
        error: (error, _) => _Message(
          text: errorText(i18n, error),
          action: i18n.t('common:action.retry'),
          onAction: () => ref.invalidate(lightConversationsProvider),
        ),
        data: (items) => items.isEmpty
            ? _Message(
                key: const Key('light-conversations-empty'),
                text: i18n.t('light:conversations.empty'),
              )
            : ListView.separated(
                key: const Key('light-conversations'),
                physics: const AlwaysScrollableScrollPhysics(),
                padding: const EdgeInsets.only(bottom: 24),
                itemCount: items.length,
                separatorBuilder: (_, _) =>
                    Divider(height: 1, color: t.hairSoft),
                itemBuilder: (context, index) {
                  final row = items[index];
                  final session = row.session;
                  final entry = ref.watch(
                    lightEntryProvider(session.lightEntry ?? ''),
                  );
                  final title = session.title.trim().isNotEmpty
                      ? session.title
                      : entry?.title ?? i18n.t('light:conversations.untitled');
                  final meta = [
                    if (row.isTopic)
                      entry?.title ?? i18n.t('light:conversations.topic'),
                    if (row.watched) i18n.t('light:conversations.watched'),
                    formatSince(session.updatedAt, i18n.language),
                  ].join(' · ');
                  return ListTile(
                    key: Key('light-conversation-${session.id}'),
                    leading: Container(
                      width: 38,
                      height: 38,
                      decoration: BoxDecoration(
                        color: row.isTopic ? t.a200 : t.hairSoft,
                        shape: BoxShape.circle,
                      ),
                      child: Icon(
                        row.isTopic
                            ? lightEntryIcon(session.lightEntry!)
                            : Icons.chat_bubble_outline,
                        size: 18,
                        color: row.isTopic ? t.a800 : t.n700,
                      ),
                    ),
                    title: Text(
                      title,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(
                        fontSize: FontSizes.base,
                        fontWeight: FontWeight.w600,
                        color: t.ink,
                      ),
                    ),
                    subtitle: Text(
                      row.summary?.trim().isNotEmpty == true
                          ? '${row.summary!.trim()}\n$meta'
                          : meta,
                      maxLines: 2,
                      overflow: TextOverflow.ellipsis,
                      style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
                    ),
                    trailing: session.isLive
                        ? SizedBox(
                            width: 14,
                            height: 14,
                            child: CircularProgressIndicator(
                              strokeWidth: 1.8,
                              color: t.a700,
                            ),
                          )
                        : Icon(Icons.chevron_right, size: 18, color: t.n500),
                    onTap: () => context.push(
                      row.isTopic
                          ? Paths.lightChat(session.id)
                          : Paths.chat(session.id),
                    ),
                  );
                },
              ),
      ),
    );
  }
}

class _Message extends StatelessWidget {
  const _Message({super.key, required this.text, this.action, this.onAction});

  final String text;
  final String? action;
  final VoidCallback? onAction;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return ListView(
      physics: const AlwaysScrollableScrollPhysics(),
      children: [
        const SizedBox(height: 96),
        Center(
          child: Text(
            text,
            textAlign: TextAlign.center,
            style: TextStyle(fontSize: FontSizes.sm, color: t.n600),
          ),
        ),
        if (action != null)
          Center(
            child: TextButton(onPressed: onAction, child: Text(action!)),
          ),
      ],
    );
  }
}
