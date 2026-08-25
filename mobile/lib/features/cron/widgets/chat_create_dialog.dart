import 'package:dio/dio.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../shared/api/providers.dart';
import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/json.dart';
import '../../../shared/router/paths.dart';
import '../api/cron_api.dart';

/// "Create via chat" (web `ChatCreateDialog`): pick a project, then open a
/// fresh conversation in it seeded with the guided-setup prompt — the
/// scheduled-tasks skill takes over on the agent side.
Future<void> showCronChatCreateDialog(BuildContext context) {
  return showDialog<void>(
    context: context,
    builder: (_) => const _ChatCreateBody(),
  );
}

class _ChatCreateBody extends ConsumerStatefulWidget {
  const _ChatCreateBody();

  @override
  ConsumerState<_ChatCreateBody> createState() => _ChatCreateBodyState();
}

class _ChatCreateBodyState extends ConsumerState<_ChatCreateBody> {
  String _projectId = '';
  bool _starting = false;
  bool _failed = false;

  Future<void> _start(String targetProject) async {
    final i18n = ref.read(i18nProvider);
    setState(() {
      _starting = true;
      _failed = false;
    });
    try {
      final dio = ref.read(apiDioProvider);
      final session = await dio.post<Map<String, dynamic>>(
        '/api/agent/session',
        data: {
          'project_id': targetProject,
          'title': i18n.t('cron:chatCreate.sessionTitle'),
        },
      );
      final sessionId = asString(session.data?['id']) ?? '';
      await dio.post<dynamic>(
        '/api/agent/session/$sessionId/prompt_async',
        data: {'text': i18n.t('cron:chatCreate.prompt')},
      );
      if (mounted) {
        Navigator.pop(context);
        context.go(Paths.chat(sessionId));
      }
    } on DioException {
      if (mounted) {
        setState(() {
          _starting = false;
          _failed = true;
        });
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final projects = ref.watch(cronProjectsProvider).valueOrNull ?? const [];
    final targetProject = _projectId.isNotEmpty
        ? _projectId
        : (projects.isNotEmpty ? projects.first.$1 : '');
    final targetName = projects
            .where((p) => p.$1 == targetProject)
            .map((p) => p.$2)
            .firstOrNull ??
        targetProject;

    return AlertDialog(
      title: Text(i18n.t('cron:chatCreate.title'),
          style: const TextStyle(fontSize: FontSizes.lg)),
      content: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(
            i18n.t('cron:chatCreate.body'),
            style: TextStyle(
                fontSize: FontSizes.sm, color: t.n700, height: 1.6),
          ),
          const SizedBox(height: 14),
          Text(i18n.t('cron:form.project'),
              style: TextStyle(fontSize: FontSizes.xs, color: t.n600)),
          const SizedBox(height: 6),
          InkWell(
            borderRadius: BorderRadius.circular(Radii.md),
            onTap: () => showModalBottomSheet<void>(
              context: context,
              builder: (sheetContext) => SafeArea(
                child: ListView(
                  shrinkWrap: true,
                  children: [
                    for (final (id, label) in projects)
                      ListTile(
                        dense: true,
                        title: Text(label,
                            style: TextStyle(
                                fontSize: FontSizes.base, color: t.ink)),
                        trailing: id == targetProject
                            ? Icon(Icons.check, size: 18, color: t.a700)
                            : null,
                        onTap: () {
                          Navigator.pop(sheetContext);
                          setState(() => _projectId = id);
                        },
                      ),
                  ],
                ),
              ),
            ),
            child: Container(
              padding:
                  const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
              decoration: BoxDecoration(
                border: Border.all(color: t.hair),
                borderRadius: BorderRadius.circular(Radii.md),
              ),
              child: Row(
                children: [
                  Expanded(
                    child: Text(targetName,
                        style: TextStyle(
                            fontSize: FontSizes.sm, color: t.ink)),
                  ),
                  Icon(Icons.expand_more, size: 15, color: t.n500),
                ],
              ),
            ),
          ),
          if (_failed)
            Padding(
              padding: const EdgeInsets.only(top: 8),
              child: Text(i18n.t('cron:chatCreate.failed'),
                  style:
                      TextStyle(fontSize: FontSizes.xs, color: t.danger)),
            ),
        ],
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.pop(context),
          child: Text(i18n.t('cron:form.cancel'),
              style: TextStyle(fontSize: FontSizes.sm, color: t.n700)),
        ),
        FilledButton(
          onPressed: targetProject.isEmpty || _starting
              ? null
              : () => _start(targetProject),
          style: FilledButton.styleFrom(
            backgroundColor: t.ink,
            foregroundColor: t.bg,
            disabledBackgroundColor: t.ink.withValues(alpha: 0.5),
            disabledForegroundColor: t.bg,
            shape: RoundedRectangleBorder(
              borderRadius: BorderRadius.circular(Radii.full),
            ),
          ),
          child: Text(
            _starting
                ? i18n.t('cron:chatCreate.starting')
                : i18n.t('cron:chatCreate.start'),
            style: const TextStyle(fontSize: FontSizes.sm),
          ),
        ),
      ],
    );
  }
}
