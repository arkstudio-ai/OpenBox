import 'package:flutter/material.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';

class AdminTheme extends StatelessWidget {
  const AdminTheme({super.key, required this.child});
  final Widget child;
  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final border = OutlineInputBorder(
      borderRadius: BorderRadius.circular(Radii.md),
      borderSide: BorderSide(color: t.hair),
    );
    return Theme(
      data: Theme.of(context).copyWith(
        inputDecorationTheme: InputDecorationTheme(
          filled: true,
          fillColor: t.card,
          isDense: true,
          contentPadding: const EdgeInsets.symmetric(
            horizontal: 12,
            vertical: 14,
          ),
          border: border,
          enabledBorder: border,
          focusedBorder: border.copyWith(borderSide: BorderSide(color: t.n400)),
          labelStyle: TextStyle(color: t.n600, fontSize: FontSizes.md),
          hintStyle: TextStyle(color: t.n600, fontSize: FontSizes.sm),
          alignLabelWithHint: true,
        ),
        filledButtonTheme: FilledButtonThemeData(
          style: FilledButton.styleFrom(
            minimumSize: const Size(44, 44),
            backgroundColor: t.ink,
            foregroundColor: t.bg,
          ),
        ),
        outlinedButtonTheme: OutlinedButtonThemeData(
          style: OutlinedButton.styleFrom(
            minimumSize: const Size(44, 44),
            side: BorderSide(color: t.hair),
          ),
        ),
        textButtonTheme: TextButtonThemeData(
          style: TextButton.styleFrom(minimumSize: const Size(44, 44)),
        ),
        expansionTileTheme: ExpansionTileThemeData(
          shape: const Border(),
          collapsedShape: const Border(),
          iconColor: t.n600,
          collapsedIconColor: t.n600,
          childrenPadding: EdgeInsets.zero,
        ),
      ),
      child: child,
    );
  }
}

/// A list row, distinct from the grouped surfaces used for forms and details.
class AdminRecordTile extends StatelessWidget {
  const AdminRecordTile({
    super.key,
    required this.title,
    this.subtitle,
    this.leading,
    this.trailing,
    this.child,
    this.onTap,
    this.fullText = false,
  });
  final String title;
  final String? subtitle;
  final Widget? leading, trailing, child;
  final VoidCallback? onTap;
  final bool fullText;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Material(
      color: t.bg,
      child: InkWell(
        onTap: onTap,
        child: Container(
          padding: const EdgeInsets.symmetric(vertical: 14, horizontal: 2),
          decoration: BoxDecoration(
            border: Border(bottom: BorderSide(color: t.hair)),
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Row(
                children: [
                  if (leading != null) ...[leading!, const SizedBox(width: 12)],
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          title,
                          maxLines: fullText ? null : 2,
                          overflow: fullText ? null : TextOverflow.ellipsis,
                          style: TextStyle(
                            color: t.ink,
                            fontSize: FontSizes.lg,
                            fontWeight: FontWeight.w500,
                          ),
                        ),
                        if (subtitle?.isNotEmpty ?? false)
                          Padding(
                            padding: const EdgeInsets.only(top: 3),
                            child: Text(
                              subtitle!,
                              maxLines: fullText ? null : 2,
                              overflow: fullText ? null : TextOverflow.ellipsis,
                              style: TextStyle(
                                color: t.n600,
                                fontSize: FontSizes.sm,
                              ),
                            ),
                          ),
                      ],
                    ),
                  ),
                  if (trailing != null)
                    trailing!
                  else if (onTap != null)
                    Padding(
                      padding: const EdgeInsets.only(left: 8),
                      child: Icon(Icons.chevron_right, size: 20, color: t.n600),
                    ),
                ],
              ),
              if (child != null) ...[const SizedBox(height: 10), child!],
            ],
          ),
        ),
      ),
    );
  }
}

class AdminSectionHeading extends StatelessWidget {
  const AdminSectionHeading(this.title, {super.key, this.subtitle});
  final String title;
  final String? subtitle;
  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.fromLTRB(0, 12, 0, 10),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          title,
          style: TextStyle(
            color: context.tokens.ink,
            fontSize: FontSizes.lg,
            fontWeight: FontWeight.w500,
          ),
        ),
        if (subtitle != null)
          Padding(
            padding: const EdgeInsets.only(top: 4),
            child: Text(
              subtitle!,
              style: TextStyle(
                color: context.tokens.n600,
                fontSize: FontSizes.sm,
              ),
            ),
          ),
      ],
    ),
  );
}

/// Primary actions stay reachable while content scrolls, including with a keyboard.
class AdminActionBar extends StatelessWidget {
  const AdminActionBar({super.key, required this.primary, this.secondary});
  final Widget primary;
  final Widget? secondary;
  @override
  Widget build(BuildContext context) => DecoratedBox(
    decoration: BoxDecoration(
      color: context.tokens.card,
      border: Border(top: BorderSide(color: context.tokens.hair)),
    ),
    child: SafeArea(
      top: false,
      minimum: const EdgeInsets.fromLTRB(16, 10, 16, 12),
      child: LayoutBuilder(
        builder: (context, constraints) {
          final stack =
              constraints.maxWidth < 300 ||
              MediaQuery.textScalerOf(context).scale(14) > 20;
          return Flex(
            direction: stack ? Axis.vertical : Axis.horizontal,
            crossAxisAlignment: stack
                ? CrossAxisAlignment.stretch
                : CrossAxisAlignment.center,
            mainAxisSize: MainAxisSize.min,
            children: [
              if (secondary != null) ...[
                if (stack) secondary! else Expanded(child: secondary!),
                SizedBox(width: stack ? 0 : 12, height: stack ? 6 : 0),
              ],
              if (stack) primary else Expanded(child: primary),
            ],
          );
        },
      ),
    ),
  );
}

class AdminSheet extends StatelessWidget {
  const AdminSheet({super.key, required this.content, required this.footer});
  final Widget content, footer;
  @override
  Widget build(BuildContext context) => Padding(
    padding: EdgeInsets.only(bottom: MediaQuery.viewInsetsOf(context).bottom),
    child: Column(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Flexible(
          child: SingleChildScrollView(
            keyboardDismissBehavior: ScrollViewKeyboardDismissBehavior.onDrag,
            padding: const EdgeInsets.fromLTRB(20, 20, 20, 16),
            child: content,
          ),
        ),
        footer,
      ],
    ),
  );
}

/// Tertiary query/view controls use a labelled picker instead of a second tab bar.
class AdminScopePicker extends StatelessWidget {
  const AdminScopePicker({
    super.key,
    required this.label,
    required this.value,
    required this.options,
    required this.onChanged,
    this.trailing,
  });
  final String label, value;
  final Map<String, String> options;
  final ValueChanged<String>? onChanged;
  final Widget? trailing;
  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.fromLTRB(16, 10, 16, 2),
    child: Row(
      children: [
        Expanded(
          child: DropdownButtonFormField<String>(
            key: ValueKey((label, value)),
            initialValue: value,
            isExpanded: true,
            decoration: InputDecoration(labelText: label, isDense: true),
            items: [
              for (final entry in options.entries)
                DropdownMenuItem(
                  value: entry.key,
                  child: Text(
                    entry.value,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(fontSize: FontSizes.md),
                  ),
                ),
            ],
            onChanged: onChanged == null ? null : (value) => onChanged!(value!),
          ),
        ),
        if (trailing != null) ...[const SizedBox(width: 8), trailing!],
      ],
    ),
  );
}
