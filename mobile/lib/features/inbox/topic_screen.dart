import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:gpt_markdown/gpt_markdown.dart';

import '../../shared/appearance/tokens.dart';
import '../../shared/appearance/type_scale.dart';
import '../../shared/i18n/i18n.dart';
import '../../shared/models/inbox.dart';
import '../../shared/widgets/toast.dart';
import 'api/inbox_api.dart';
import 'state/inbox_navigator.dart';

/// A first-party topic page (web `/topics/:slug`): cover, title, Markdown
/// body rendered natively, optional call to action routed like any inbox link.
class TopicScreen extends ConsumerStatefulWidget {
  const TopicScreen({super.key, required this.slug});

  final String slug;

  @override
  ConsumerState<TopicScreen> createState() => _TopicScreenState();
}

class _TopicScreenState extends ConsumerState<TopicScreen> {
  final _cancel = CancelToken();
  late Future<TopicPage> _topic = _load();

  Future<TopicPage> _load() =>
      ref.read(inboxApiProvider).topic(widget.slug, cancel: _cancel);

  @override
  void dispose() {
    _cancel.cancel();
    super.dispose();
  }

  Future<void> _cta(TopicPage topic) async {
    final navigator = InboxNavigator(ref.read, GoRouter.of(context));
    final result = await navigator.open(
      topic.ctaLink,
      stillCurrent: () => mounted,
    );
    if (result == InboxOpen.unavailable && mounted) {
      ref
          .read(toastProvider.notifier)
          .warning(ref.read(i18nProvider).t('inbox:unavailable'));
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return Scaffold(
      backgroundColor: t.bg,
      appBar: AppBar(
        titleSpacing: 0,
        title: Text(
          i18n.t('inbox:topic.title'),
          style: TextStyle(
            fontSize: FontSizes.lg,
            fontWeight: FontWeight.w500,
            color: t.ink,
          ),
        ),
      ),
      body: FutureBuilder<TopicPage>(
        future: _topic,
        builder: (context, snapshot) {
          if (snapshot.hasError) {
            return Center(
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  Text(
                    i18n.t('inbox:topic.loadFailed'),
                    style: TextStyle(fontSize: FontSizes.sm, color: t.n600),
                  ),
                  TextButton(
                    onPressed: () => setState(() => _topic = _load()),
                    child: Text(i18n.t('common:action.retry')),
                  ),
                ],
              ),
            );
          }
          final topic = snapshot.data;
          if (topic == null) {
            return const Center(
              child: CircularProgressIndicator(strokeWidth: 2),
            );
          }
          return ListView(
            padding: const EdgeInsets.fromLTRB(20, 8, 20, 40),
            children: [
              if (topic.coverUrl != null)
                ClipRRect(
                  borderRadius: BorderRadius.circular(Radii.lg),
                  child: Image.network(
                    topic.coverUrl!,
                    fit: BoxFit.cover,
                    errorBuilder: (_, _, _) => const SizedBox.shrink(),
                  ),
                ),
              if (topic.coverUrl != null) const SizedBox(height: 16),
              Text(
                topic.title,
                style: TextStyle(
                  fontSize: FontSizes.xl,
                  fontWeight: FontWeight.w600,
                  height: 1.3,
                  color: t.ink,
                ),
              ),
              const SizedBox(height: 14),
              GptMarkdown(
                topic.contentMd,
                style: TextStyle(
                  fontSize: FontSizes.lg,
                  height: 1.78,
                  color: t.ink,
                ),
              ),
              if (topic.ctaLabel != null && topic.ctaLink != null) ...[
                const SizedBox(height: 24),
                FilledButton(
                  key: const ValueKey('topic-cta'),
                  onPressed: () => _cta(topic),
                  style: FilledButton.styleFrom(
                    backgroundColor: t.ink,
                    foregroundColor: t.bg,
                    minimumSize: const Size(0, 44),
                    shape: RoundedRectangleBorder(
                      borderRadius: BorderRadius.circular(Radii.full),
                    ),
                  ),
                  child: Text(
                    topic.ctaLabel!,
                    style: const TextStyle(fontSize: FontSizes.base),
                  ),
                ),
              ],
            ],
          );
        },
      ),
    );
  }
}
