import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:url_launcher/url_launcher.dart';

import '../../../../shared/appearance/tokens.dart';
import '../../../../shared/appearance/type_scale.dart';
import '../../../../shared/i18n/i18n.dart';
import '../../../../shared/models/message_part.dart';
import '../../utils/tool_parse.dart';

/// Shared building blocks for a tool's detail column, ported from
/// frontend-v2 `components/tool/ToolPrimitives.tsx` and translated onto
/// bossip tokens.
// ------------------------------------------------------------- primitives

/// Faint section caption above a request/response block.
class ToolMiniLabel extends StatelessWidget {
  const ToolMiniLabel(this.text, {super.key});

  final String text;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Padding(
      padding: const EdgeInsets.only(bottom: 3),
      child: Text(
        text,
        style: TextStyle(
          fontSize: FontSizes.xs2,
          fontWeight: FontWeight.w500,
          height: 1.4,
          color: t.n600.withValues(alpha: 0.7),
        ),
      ),
    );
  }
}

/// Monospace code/output box; red-toned on failure, nothing when empty.
class ToolPre extends StatelessWidget {
  const ToolPre(this.text, {super.key, this.failed = false});

  final String text;
  final bool failed;

  @override
  Widget build(BuildContext context) {
    if (text.trim().isEmpty) return const SizedBox.shrink();
    final t = context.tokens;
    return Container(
      width: double.infinity,
      constraints: const BoxConstraints(maxHeight: 220),
      padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 7),
      decoration: BoxDecoration(
        color: failed ? t.dangerSoft : t.n200.withValues(alpha: 0.25),
        border: Border.all(
          color: failed ? t.danger.withValues(alpha: 0.25) : t.hair,
        ),
        borderRadius: BorderRadius.circular(Radii.sm),
      ),
      child: SingleChildScrollView(
        child: Text(
          text,
          style: TextStyle(
            fontSize: FontSizes.xs2,
            height: 1.7,
            color: failed ? t.danger : t.n700,
            fontFamily: 'Menlo',
            fontFamilyFallback: const ['monospace'],
          ),
        ),
      ),
    );
  }
}

/// Row of domain pills linking to sources, deduped and capped at eight.
class ToolSourceLinks extends ConsumerWidget {
  const ToolSourceLinks({super.key, required this.urls});

  final List<String> urls;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final unique = dedupeUrls(urls);
    if (unique.isEmpty) return const SizedBox.shrink();
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return Wrap(
      spacing: 5,
      runSpacing: 5,
      children: [
        for (final (index, url) in unique.indexed)
          GestureDetector(
            onTap: () => launchUrl(
              Uri.parse(url),
              mode: LaunchMode.externalApplication,
            ),
            child: Container(
              constraints: const BoxConstraints(maxWidth: 220),
              padding:
                  const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
              decoration: BoxDecoration(
                color: t.bg.withValues(alpha: 0.55),
                border: Border.all(color: t.hair),
                borderRadius: BorderRadius.circular(Radii.full),
              ),
              child: Text(
                safeHostname(url).isEmpty
                    ? i18n.t('chat:toolDetail.sourceFallback',
                        vars: {'index': index + 1})
                    : safeHostname(url),
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: TextStyle(
                  fontSize: FontSizes.xs2,
                  fontWeight: FontWeight.w500,
                  color: t.n700,
                ),
              ),
            ),
          ),
      ],
    );
  }
}

/// Long text clamped to eight lines, with an expand toggle.
class ToolDetailText extends ConsumerStatefulWidget {
  const ToolDetailText(this.text, {super.key, this.failed = false});

  final String text;
  final bool failed;

  @override
  ConsumerState<ToolDetailText> createState() => _ToolDetailTextState();
}

class _ToolDetailTextState extends ConsumerState<ToolDetailText> {
  static const _collapsedLines = 8;

  bool _open = false;

  @override
  Widget build(BuildContext context) {
    if (widget.text.trim().isEmpty) return const SizedBox.shrink();
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final clamp = widget.text.split('\n').length > _collapsedLines ||
        widget.text.length > 520;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          widget.text,
          maxLines: _open || !clamp ? null : _collapsedLines,
          overflow: _open || !clamp ? null : TextOverflow.ellipsis,
          style: TextStyle(
            fontSize: FontSizes.xs2,
            height: 1.7,
            color: widget.failed ? t.danger : t.n700,
            fontFamily: 'Menlo',
            fontFamilyFallback: const ['monospace'],
          ),
        ),
        if (clamp)
          GestureDetector(
            onTap: () => setState(() => _open = !_open),
            child: Padding(
              padding: const EdgeInsets.only(top: 2),
              child: Text(
                i18n.t(_open
                    ? 'chat:toolDetail.collapse'
                    : 'chat:toolDetail.expand'),
                style: TextStyle(fontSize: FontSizes.xs, color: t.a700),
              ),
            ),
          ),
      ],
    );
  }
}

/// The status line every layout opens with.
class StatusLine extends ConsumerWidget {
  const StatusLine({super.key, required this.status});

  final ToolStatus status;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final text = switch (status) {
      ToolStatus.running || ToolStatus.pending =>
        i18n.t('chat:toolStatus.running'),
      ToolStatus.error => i18n.t('chat:toolStatus.failed'),
      ToolStatus.completed => i18n.t('chat:toolStatus.completed'),
    };
    return Text(
      text,
      style: TextStyle(fontSize: FontSizes.xs2, color: t.n600),
    );
  }
}

/// Vertical rhythm shared by every layout.
class ToolBlocks extends StatelessWidget {
  const ToolBlocks({super.key, required this.children});

  final List<Widget> children;

  @override
  Widget build(BuildContext context) => Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          for (final (index, child) in children.indexed) ...[
            if (index > 0) const SizedBox(height: 7),
            child,
          ],
        ],
      );
}

class ToolBlock extends StatelessWidget {
  const ToolBlock({super.key, required this.label, required this.child});

  final String label;
  final Widget child;

  @override
  Widget build(BuildContext context) => Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [ToolMiniLabel(label), child],
      );
}
