import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../../shared/appearance/tokens.dart';
import '../../../../shared/appearance/type_scale.dart';
import '../../../../shared/events/bus.dart';
import '../../../../shared/i18n/i18n.dart';
import '../../../../shared/models/message_part.dart';
import '../../../../shared/utils/project_path.dart';
import '../../utils/diff_preview.dart';
import '../../utils/tool_map.dart';
import '../../utils/tool_parse.dart';
import 'tool_primitives.dart';

/// Structured detail column for one tool call (web
/// `components/tool/ToolOutput.tsx`). Dispatches on the tool's layout and
/// composes request/response blocks from the primitives below, so a shell
/// call reads as a command and its output rather than as a JSON dump.
class ToolOutput extends ConsumerWidget {
  const ToolOutput({super.key, required this.part});

  final MessagePart part;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final tool = part;
    if (tool is SubtaskPart) return _SubtaskOutput(part: tool);
    if (tool is! ToolPart) return const SizedBox.shrink();
    final failed =
        tool.status == ToolStatus.error ||
        (tool.error?.trim().isNotEmpty ?? false);
    return switch (resolveToolLayout(tool.tool)) {
      'search' => _SearchOutput(part: tool, failed: failed),
      'fetch' => _FetchOutput(part: tool, failed: failed),
      'shell' => _ShellOutput(part: tool),
      'file' => _FileOutput(part: tool, failed: failed),
      'find' => _FindOutput(part: tool, failed: failed),
      'skill' => _SkillOutput(part: tool, failed: failed),
      'agent' => _AgentOutput(part: tool, failed: failed),
      'question' => _QuestionAnswered(part: tool),
      _ => _GenericOutput(part: tool, failed: failed),
    };
  }
}

// ---------------------------------------------------------------- layouts

class _SearchOutput extends ConsumerWidget {
  const _SearchOutput({required this.part, required this.failed});

  final ToolPart part;
  final bool failed;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final query = toolInput(part.input, 'query').isNotEmpty
        ? toolInput(part.input, 'query')
        : (part.metadata['query'] is String
              ? (part.metadata['query'] as String).trim()
              : '');
    final action = toolInput(part.input, 'action').isNotEmpty
        ? toolInput(part.input, 'action')
        : toolInput(part.input, 'type');
    final results = parseSearchResults(part.metadata);
    final output = part.output is String ? part.output as String : '';
    final urls = results.isNotEmpty
        ? [for (final r in results) r.url]
        : parseSearchUrls(output);
    final responseLabel = i18n.t(
      failed ? 'chat:toolDetail.error' : 'chat:toolDetail.response',
    );

    return ToolBlocks(
      children: [
        StatusLine(status: part.status),
        if (query.isNotEmpty || (action.isNotEmpty && action != query))
          ToolBlock(
            label: i18n.t('chat:toolDetail.request'),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                if (query.isNotEmpty)
                  Text(
                    '${i18n.t('chat:toolDetail.query')}: $query',
                    style: TextStyle(fontSize: FontSizes.xs, color: t.n700),
                  ),
                if (action.isNotEmpty && action != query)
                  Text(
                    '${i18n.t('chat:toolDetail.action')}: $action',
                    style: TextStyle(fontSize: FontSizes.xs, color: t.n700),
                  ),
              ],
            ),
          ),
        if (urls.isNotEmpty || results.isNotEmpty)
          ToolBlock(
            label: responseLabel,
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                ToolSourceLinks(urls: urls),
                for (final result in results)
                  Padding(
                    padding: const EdgeInsets.only(top: 7),
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        if (result.title.isNotEmpty)
                          Text(
                            result.title,
                            style: TextStyle(
                              fontSize: FontSizes.xs,
                              fontWeight: FontWeight.w500,
                              color: t.n800,
                            ),
                          ),
                        if (result.snippet.isNotEmpty)
                          Text(
                            result.snippet,
                            maxLines: 2,
                            overflow: TextOverflow.ellipsis,
                            style: TextStyle(
                              fontSize: FontSizes.xs2,
                              color: t.n600,
                            ),
                          ),
                      ],
                    ),
                  ),
              ],
            ),
          )
        else if (output.isNotEmpty)
          ToolBlock(
            label: responseLabel,
            child: ToolDetailText(
              failed ? (part.error ?? output) : output,
              failed: failed,
            ),
          ),
      ],
    );
  }
}

class _FetchOutput extends ConsumerWidget {
  const _FetchOutput({required this.part, required this.failed});

  final ToolPart part;
  final bool failed;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final url = toolInput(part.input, 'url');
    final output = part.output is String ? part.output as String : '';
    final body = failed ? (part.error ?? output) : output;

    return ToolBlocks(
      children: [
        StatusLine(status: part.status),
        if (url.isNotEmpty)
          ToolBlock(
            label: i18n.t('chat:toolDetail.request'),
            child: ToolSourceLinks(urls: [url]),
          ),
        if (body.isNotEmpty)
          ToolBlock(
            label: i18n.t(
              failed ? 'chat:toolDetail.error' : 'chat:toolDetail.response',
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                ToolPre(body, failed: failed),
                if (isTruncated(part.metadata))
                  Padding(
                    padding: const EdgeInsets.only(top: 3),
                    child: Text(
                      i18n.t('chat:toolDetail.truncated'),
                      style: TextStyle(fontSize: FontSizes.xs2, color: t.n600),
                    ),
                  ),
              ],
            ),
          ),
      ],
    );
  }
}

class _ShellOutput extends ConsumerWidget {
  const _ShellOutput({required this.part});

  final ToolPart part;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final command = toolInput(part.input, 'command');
    final exitCode = parseExitCode(part.metadata);
    final failed =
        part.status == ToolStatus.error ||
        (part.error?.trim().isNotEmpty ?? false) ||
        (exitCode != null && exitCode != 0);
    final output = part.output is String ? part.output as String : '';
    final body = output.isNotEmpty ? output : (part.error ?? '');

    return ToolBlocks(
      children: [
        StatusLine(status: part.status),
        if (command.isNotEmpty)
          ToolBlock(
            label: i18n.t('chat:toolDetail.command'),
            child: ToolPre(command),
          ),
        if (body.isNotEmpty)
          ToolBlock(
            label: i18n.t(
              failed ? 'chat:toolDetail.error' : 'chat:toolDetail.output',
            ),
            child: ToolPre(body, failed: failed),
          ),
        if (exitCode != null && exitCode != 0)
          Text(
            '${i18n.t('chat:toolDetail.exitCode')}: $exitCode',
            style: TextStyle(fontSize: FontSizes.xs2, color: t.danger),
          ),
      ],
    );
  }
}

class _FileOutput extends ConsumerWidget {
  const _FileOutput({required this.part, required this.failed});

  final ToolPart part;
  final bool failed;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final tool = part.tool.toLowerCase();
    final rawPath = toolInput(part.input, 'file_path').isNotEmpty
        ? toolInput(part.input, 'file_path')
        : toolInput(part.input, 'path');
    final path = projectScopedDisplayPath(rawPath);
    final edits = parseEdits(part.input);
    final output = part.output is String ? part.output as String : '';
    final patch = toolInput(part.input, 'patch');
    final content = switch (tool) {
      'read' => stripLineNumbers(output),
      'write' =>
        toolInput(part.input, 'content').isNotEmpty
            ? toolInput(part.input, 'content')
            : output,
      _ => projectScopedToolText(patch.isNotEmpty ? patch : output),
    };

    return ToolBlocks(
      children: [
        StatusLine(status: part.status),
        if (path.isNotEmpty)
          Text(
            path,
            style: TextStyle(
              fontSize: FontSizes.xs,
              color: t.n800,
              fontFamily: 'Menlo',
              fontFamilyFallback: const ['monospace'],
            ),
          ),
        if (edits.isNotEmpty)
          ToolBlock(
            label: i18n.t('chat:toolDetail.diff'),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                for (final edit in edits)
                  _EditPreview(
                    preview: editPreview(edit.oldString, edit.newString),
                    path: path,
                  ),
              ],
            ),
          )
        else if (content.isNotEmpty)
          ToolBlock(
            label: i18n.t('chat:toolDetail.content'),
            child: ToolDetailText(content, failed: failed),
          ),
        if (failed && (part.error?.isNotEmpty ?? false))
          ToolBlock(
            label: i18n.t('chat:toolDetail.error'),
            child: ToolPre(part.error!, failed: true),
          ),
      ],
    );
  }
}

/// Changed lines only, with untouched runs collapsed. Tapping opens the same
/// review surface the turn's change card does — anything else reads as a dead
/// tap, since the rows look identical.
class _EditPreview extends ConsumerWidget {
  const _EditPreview({required this.preview, required this.path});

  final DiffPreview preview;
  final String path;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    if (preview.isEmpty) return const SizedBox.shrink();
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final body = Container(
      margin: const EdgeInsets.only(bottom: 6),
      clipBehavior: Clip.antiAlias,
      decoration: BoxDecoration(
        border: Border.all(color: t.hair),
        borderRadius: BorderRadius.circular(Radii.sm),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          for (final row in preview.rows)
            switch (row) {
              GapRow(:final count) => Container(
                width: double.infinity,
                color: t.n200.withValues(alpha: 0.35),
                padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
                child: Text(
                  i18n.t('chat:diff.unmodified', count: count),
                  style: TextStyle(fontSize: FontSizes.xs2, color: t.n600),
                ),
              ),
              ChangeRow(:final added, :final text) => Container(
                width: double.infinity,
                color: added ? t.diffAdd : t.diffDel,
                padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 1),
                child: Text(
                  '${added ? '+' : '−'} $text',
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(
                    fontSize: FontSizes.xs2,
                    height: 1.7,
                    color: added ? t.s700 : t.dangerInk,
                    fontFamily: 'Menlo',
                    fontFamilyFallback: const ['monospace'],
                  ),
                ),
              ),
            },
          if (preview.hiddenChanges > 0)
            Container(
              width: double.infinity,
              color: t.n200.withValues(alpha: 0.35),
              padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
              child: Text(
                i18n.t('chat:diff.more', count: preview.hiddenChanges),
                style: TextStyle(fontSize: FontSizes.xs2, color: t.n600),
              ),
            ),
        ],
      ),
    );
    if (path.isEmpty) return body;
    return GestureDetector(
      onTap: () => ref.read(appEventBusProvider).emit('workbench.open', {
        'kind': 'review',
        'file': path,
      }),
      child: body,
    );
  }
}

class _FindOutput extends ConsumerWidget {
  const _FindOutput({required this.part, required this.failed});

  final ToolPart part;
  final bool failed;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final pattern = toolInput(part.input, 'pattern').isNotEmpty
        ? toolInput(part.input, 'pattern')
        : toolInput(part.input, 'query');
    final output = part.output is String ? part.output as String : '';
    final body = projectScopedDisplayText(
      failed ? (part.error ?? output) : output,
    );

    return ToolBlocks(
      children: [
        StatusLine(status: part.status),
        if (pattern.isNotEmpty)
          Text(
            pattern,
            style: TextStyle(
              fontSize: FontSizes.xs,
              color: t.n800,
              fontFamily: 'Menlo',
              fontFamilyFallback: const ['monospace'],
            ),
          ),
        if (body.isNotEmpty)
          ToolBlock(
            label: i18n.t(
              failed ? 'chat:toolDetail.error' : 'chat:toolDetail.matches',
            ),
            child: ToolDetailText(body, failed: failed),
          ),
      ],
    );
  }
}

class _AgentOutput extends ConsumerWidget {
  const _AgentOutput({required this.part, required this.failed});

  final ToolPart part;
  final bool failed;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final description = [
      toolInput(part.input, 'description'),
      toolInput(part.input, 'skill'),
      toolInput(part.input, 'prompt'),
    ].firstWhere((value) => value.isNotEmpty, orElse: () => '');
    final output = part.output is String ? part.output as String : '';
    final body = failed ? (part.error ?? output) : output;

    return ToolBlocks(
      children: [
        StatusLine(status: part.status),
        if (description.isNotEmpty)
          Text(
            description,
            style: TextStyle(fontSize: FontSizes.xs, color: t.n700),
          ),
        if (body.isNotEmpty)
          ToolBlock(
            label: i18n.t(
              failed ? 'chat:toolDetail.error' : 'chat:toolDetail.result',
            ),
            child: ToolDetailText(body, failed: failed),
          ),
      ],
    );
  }
}

/// A skill load: just which skill, and whether it loaded.
///
/// The output is the skill's full instruction document — thousands of words
/// written for the model, not the reader. Showing it turned every skill call
/// into a wall of manual the user had to scroll past to find the answer.
class _SkillOutput extends ConsumerWidget {
  const _SkillOutput({required this.part, required this.failed});

  final ToolPart part;
  final bool failed;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final name = toolInput(part.input, 'skill').isNotEmpty
        ? toolInput(part.input, 'skill')
        : (part.title ?? '');

    return ToolBlocks(
      children: [
        StatusLine(status: part.status),
        if (name.isNotEmpty)
          Text(
            name,
            style: TextStyle(
              fontSize: FontSizes.xs,
              color: t.n700,
              fontFamily: 'Menlo',
              fontFamilyFallback: const ['monospace'],
            ),
          ),
        if (failed && (part.error?.isNotEmpty ?? false))
          ToolBlock(
            label: i18n.t('chat:toolDetail.error'),
            child: ToolDetailText(part.error!, failed: true),
          ),
      ],
    );
  }
}

class _GenericOutput extends ConsumerWidget {
  const _GenericOutput({required this.part, required this.failed});

  final ToolPart part;
  final bool failed;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final i18n = ref.watch(i18nProvider);
    final input = part.input;
    final args = input is Map<String, dynamic> && input.isNotEmpty
        ? projectScopedDisplayText(
            const JsonEncoder.withIndent('  ').convert(input),
          )
        : '';
    final output = part.output is String
        ? part.output as String
        : (part.output == null ? '' : '${part.output}');
    final body = projectScopedDisplayText(
      failed ? (part.error ?? output) : output,
    );

    return ToolBlocks(
      children: [
        StatusLine(status: part.status),
        if (args.isNotEmpty)
          ToolBlock(
            label: i18n.t('chat:toolDetail.arguments'),
            child: ToolPre(args),
          ),
        if (body.isNotEmpty)
          ToolBlock(
            label: i18n.t(
              failed ? 'chat:toolDetail.error' : 'chat:toolDetail.result',
            ),
            child: ToolDetailText(body, failed: failed),
          ),
      ],
    );
  }
}

class _SubtaskOutput extends ConsumerWidget {
  const _SubtaskOutput({required this.part});

  final SubtaskPart part;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final failed = part.status == 'error';
    return ToolBlocks(
      children: [
        StatusLine(status: failed ? ToolStatus.error : ToolStatus.completed),
        if (part.description.isNotEmpty)
          Text(
            part.description,
            style: TextStyle(fontSize: FontSizes.xs, color: t.n700),
          ),
        if (part.output?.isNotEmpty ?? false)
          ToolBlock(
            label: i18n.t(
              failed ? 'chat:toolDetail.error' : 'chat:toolDetail.result',
            ),
            child: ToolDetailText(part.output!, failed: failed),
          ),
      ],
    );
  }
}

/// What was asked, and what the user chose — shown in the conversation after
/// the dock above the composer has gone (web `QuestionAnswered`).
///
/// The dock is for answering; this is the record. Without it the exchange left
/// only "问了 2 个问题" in the tool chain, so scrolling back told you a
/// decision had been made but not which way.
class _QuestionAnswered extends ConsumerWidget {
  const _QuestionAnswered({required this.part});

  final ToolPart part;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final pairs = questionPairs(part.metadata);
    if (pairs.isEmpty) {
      return _GenericOutput(
        part: part,
        failed: part.status == ToolStatus.error,
      );
    }
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        for (final (question, answer) in pairs)
          Padding(
            padding: const EdgeInsets.only(bottom: 7),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  question,
                  style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
                ),
                Text(
                  answer.isEmpty
                      ? i18n.t('chat:question.unanswered')
                      : answer.join('、'),
                  style: TextStyle(fontSize: FontSizes.sm, color: t.ink),
                ),
              ],
            ),
          ),
      ],
    );
  }
}
