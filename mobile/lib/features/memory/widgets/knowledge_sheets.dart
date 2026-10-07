import 'package:flutter/material.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';

/// One choice in an action sheet.
class SheetAction<T> {
  const SheetAction({
    required this.value,
    required this.label,
    this.icon,
    this.key,
    this.danger = false,
    this.enabled = true,
    this.selected = false,
  });

  final T value;
  final String label;
  final IconData? icon;
  final Key? key;

  /// Destructive: drawn in red. It still asks before doing anything.
  final bool danger;
  final bool enabled;

  /// The current choice, ticked.
  final bool selected;
}

/// A short list of actions from the bottom of the screen, under a quiet
/// [title] or [header] naming what they apply to. Resolves with the chosen
/// value, or null when dismissed.
Future<T?> showActionSheet<T>(
  BuildContext context, {
  String? title,
  Widget? header,
  required List<SheetAction<T>> actions,
}) {
  final t = context.tokens;
  return showModalBottomSheet<T>(
    context: context,
    backgroundColor: t.card,
    showDragHandle: true,
    isScrollControlled: true,
    useSafeArea: true,
    builder: (sheetContext) => ConstrainedBox(
      constraints: BoxConstraints(
        maxHeight: MediaQuery.sizeOf(sheetContext).height * 0.8,
      ),
      child: SafeArea(
        top: false,
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            if (header != null)
              Padding(
                padding: const EdgeInsets.fromLTRB(20, 0, 20, 8),
                child: header,
              )
            else if (title != null)
              Padding(
                padding: const EdgeInsets.fromLTRB(20, 0, 20, 8),
                child: SheetTitle(title),
              ),
            Flexible(
              child: ListView(
                shrinkWrap: true,
                padding: const EdgeInsets.only(bottom: 8),
                children: [
                  for (final action in actions)
                    _ActionTile(
                      action: action,
                      onTap: () => Navigator.of(sheetContext).pop(action.value),
                    ),
                ],
              ),
            ),
          ],
        ),
      ),
    ),
  );
}

/// What a sheet is about, small and muted above its actions.
class SheetTitle extends StatelessWidget {
  const SheetTitle(this.text, {super.key, this.maxLines = 2});

  final String text;
  final int maxLines;

  @override
  Widget build(BuildContext context) => Text(
    text,
    maxLines: maxLines,
    overflow: TextOverflow.ellipsis,
    style: TextStyle(
      fontSize: FontSizes.sm,
      height: 1.5,
      fontWeight: FontWeight.w500,
      color: context.tokens.n600,
    ),
  );
}

class _ActionTile<T> extends StatelessWidget {
  const _ActionTile({required this.action, required this.onTap});

  final SheetAction<T> action;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final color = !action.enabled
        ? t.n500
        : action.danger
        ? t.dangerInk
        : t.ink;
    return ListTile(
      key: action.key,
      enabled: action.enabled,
      selected: action.selected,
      minTileHeight: 50,
      contentPadding: const EdgeInsets.symmetric(horizontal: 20),
      horizontalTitleGap: 14,
      leading: action.icon == null
          ? null
          : Icon(
              action.icon,
              size: 20,
              color: action.danger && action.enabled ? t.dangerInk : t.n700,
            ),
      title: Text(
        action.label,
        maxLines: 2,
        overflow: TextOverflow.ellipsis,
        style: TextStyle(
          fontSize: FontSizes.base,
          fontWeight: action.selected ? FontWeight.w500 : FontWeight.w400,
          color: color,
        ),
      ),
      trailing: action.selected
          ? Icon(Icons.check_rounded, size: 20, color: t.a700)
          : null,
      onTap: action.enabled ? onTap : null,
    );
  }
}
