import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../appearance/tokens.dart';
import '../appearance/type_scale.dart';

/// Toast store + host, mirroring frontend-v2 `shared/ui/Toast.tsx`:
/// bottom-center pills, auto-removed after 3200ms.
enum ToastKind { info, error }

class ToastItem {
  const ToastItem({required this.id, required this.kind, required this.text});

  final int id;
  final ToastKind kind;
  final String text;
}

class ToastController extends Notifier<List<ToastItem>> {
  int _seq = 0;

  @override
  List<ToastItem> build() => const [];

  void push(ToastKind kind, String text) {
    final item = ToastItem(id: _seq++, kind: kind, text: text);
    state = [...state, item];
    Timer(const Duration(milliseconds: 3200), () => remove(item.id));
  }

  void info(String text) => push(ToastKind.info, text);

  void error(String text) => push(ToastKind.error, text);

  void remove(int id) {
    state = state.where((t) => t.id != id).toList();
  }
}

final toastProvider =
    NotifierProvider<ToastController, List<ToastItem>>(ToastController.new);

/// Overlay host — stack this above the app content.
class ToastHost extends ConsumerWidget {
  const ToastHost({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final items = ref.watch(toastProvider);
    final t = context.tokens;
    return IgnorePointer(
      child: Align(
        alignment: Alignment.bottomCenter,
        child: Padding(
          padding: const EdgeInsets.only(bottom: 96),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              for (final item in items)
                Container(
                  margin: const EdgeInsets.only(top: 8),
                  padding:
                      const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
                  decoration: BoxDecoration(
                    color: item.kind == ToastKind.error ? t.danger : t.a800,
                    borderRadius: BorderRadius.circular(Radii.full),
                  ),
                  child: Text(
                    item.text,
                    style: TextStyle(color: t.bg, fontSize: FontSizes.sm),
                  ),
                ),
            ],
          ),
        ),
      ),
    );
  }
}
