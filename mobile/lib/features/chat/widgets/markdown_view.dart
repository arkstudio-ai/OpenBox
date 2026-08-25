import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_highlight/flutter_highlight.dart';
import 'package:flutter_highlight/themes/atom-one-dark.dart';
import 'package:flutter_highlight/themes/github.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:gpt_markdown/gpt_markdown.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/widgets/toast.dart';

/// Streaming-tolerant markdown, the mobile analog of web `Markdown.tsx`
/// (streamdown): incomplete-markdown tolerant rendering, themed code blocks
/// with copy, block caret while streaming.
enum MarkdownVariant { normal, thinking, user }

class MarkdownView extends StatelessWidget {
  const MarkdownView(
    this.text, {
    super.key,
    this.variant = MarkdownVariant.normal,
    this.streaming = false,
  });

  final String text;
  final MarkdownVariant variant;
  final bool streaming;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final style = switch (variant) {
      MarkdownVariant.normal =>
        TextStyle(fontSize: FontSizes.lg, height: 1.78, color: t.ink),
      MarkdownVariant.thinking =>
        TextStyle(fontSize: FontSizes.md, height: 1.7, color: t.n600),
      MarkdownVariant.user =>
        TextStyle(fontSize: FontSizes.base, height: 1.65, color: t.ink),
    };
    // Block caret while streaming (web streamdown `caret="block"`).
    final content = streaming && text.isNotEmpty ? '$text ▌' : text;
    return GptMarkdown(
      content,
      style: style,
      codeBuilder: (context, name, code, closed) =>
          CodeBlock(language: name, code: code),
      inlineCodeBuilder: (context, code, inlineStyle, codeStyle) => TextSpan(
        text: code,
        style: inlineStyle.copyWith(
          fontFamily: 'Menlo',
          fontFamilyFallback: const ['Courier', 'monospace'],
          fontSize: (inlineStyle.fontSize ?? FontSizes.base) * 0.86,
          color: t.n800,
          backgroundColor: t.n200,
        ),
      ),
    );
  }
}

/// Code block styled like the web streamdown slot (`index.css:241-267`):
/// radius 14, hairline border, bg = 45% n200 over card, mono 12.48/1.7,
/// header with language + copy.
class CodeBlock extends ConsumerWidget {
  const CodeBlock({super.key, required this.language, required this.code});

  final String language;
  final String code;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final isDark = Theme.of(context).brightness == Brightness.dark;
    final bg = Color.lerp(t.card, t.n200, 0.45)!;
    final i18n = ref.watch(i18nProvider);
    return Container(
      width: double.infinity,
      margin: const EdgeInsets.symmetric(vertical: 14),
      decoration: BoxDecoration(
        color: bg,
        borderRadius: BorderRadius.circular(Radii.lg),
        border: Border.all(color: t.hair),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(14, 8, 6, 0),
            child: Row(
              children: [
                Expanded(
                  child: Text(
                    language.isEmpty ? 'text' : language,
                    style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
                  ),
                ),
                InkWell(
                  borderRadius: BorderRadius.circular(Radii.sm),
                  onTap: () async {
                    await Clipboard.setData(ClipboardData(text: code));
                    ref.read(toastProvider.notifier).info(i18n.t('chat:copied'));
                  },
                  child: Padding(
                    padding: const EdgeInsets.all(6),
                    child: Icon(Icons.copy_outlined, size: 14, color: t.n600),
                  ),
                ),
              ],
            ),
          ),
          SingleChildScrollView(
            scrollDirection: Axis.horizontal,
            padding: const EdgeInsets.fromLTRB(16, 6, 16, 14),
            child: HighlightView(
              code,
              language: language.isEmpty ? 'plaintext' : language,
              theme: {
                ...(isDark ? atomOneDarkTheme : githubTheme),
                'root': TextStyle(
                  backgroundColor: Colors.transparent,
                  color: isDark ? t.n800 : t.n900,
                ),
              },
              textStyle: const TextStyle(
                fontFamily: 'Menlo',
                fontFamilyFallback: ['Courier', 'monospace'],
                fontSize: FontSizes.sm,
                height: 1.7,
              ),
            ),
          ),
        ],
      ),
    );
  }
}
