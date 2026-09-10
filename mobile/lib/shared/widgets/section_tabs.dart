import 'package:flutter/material.dart';

import '../appearance/tokens.dart';
import '../appearance/type_scale.dart';

/// Lightweight native section navigation. Labels can scroll at large type sizes;
/// changing a section is explicit and does not create or dispose page contents.
class SectionTabs extends StatelessWidget {
  const SectionTabs({
    super.key,
    required this.labels,
    required this.value,
    required this.onChanged,
    this.padding = const EdgeInsets.symmetric(horizontal: 16),
  });

  final Map<String, String> labels;
  final String value;
  final ValueChanged<String>? onChanged;
  final EdgeInsetsGeometry padding;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return DecoratedBox(
      decoration: BoxDecoration(
        border: Border(bottom: BorderSide(color: t.hair)),
      ),
      child: SingleChildScrollView(
        scrollDirection: Axis.horizontal,
        padding: padding,
        child: Row(
          children: [
            for (final entry in labels.entries)
              Semantics(
                selected: value == entry.key,
                button: true,
                child: Material(
                  type: MaterialType.transparency,
                  child: InkWell(
                    onTap: onChanged == null
                        ? null
                        : () {
                            if (value != entry.key) {
                              FocusManager.instance.primaryFocus?.unfocus();
                              onChanged!(entry.key);
                            }
                          },
                    child: Container(
                      constraints: const BoxConstraints(
                        minHeight: 48,
                        minWidth: 72,
                      ),
                      alignment: Alignment.center,
                      padding: const EdgeInsets.symmetric(
                        horizontal: 12,
                        vertical: 12,
                      ),
                      decoration: BoxDecoration(
                        border: Border(
                          bottom: BorderSide(
                            width: 2,
                            color: value == entry.key
                                ? t.ink
                                : t.hair.withValues(alpha: 0),
                          ),
                        ),
                      ),
                      child: Text(
                        entry.value,
                        style: TextStyle(
                          fontSize: FontSizes.md,
                          fontWeight: value == entry.key
                              ? FontWeight.w600
                              : FontWeight.w400,
                          color: value == entry.key ? t.ink : t.n600,
                        ),
                      ),
                    ),
                  ),
                ),
              ),
          ],
        ),
      ),
    );
  }
}
