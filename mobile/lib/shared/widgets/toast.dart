import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../appearance/tokens.dart';
import '../appearance/type_scale.dart';
import '../i18n/i18n.dart';

/// Toast store + host, mirroring frontend-v2 `shared/ui/Toast.tsx`.
///
/// Shaped as a card rather than a pill because the messages that matter most
/// are the long ones: a quota refusal runs to a couple of sentences, and a
/// pill with no max width stretched it into a single unreadable line that
/// then vanished before it could be read. Cards wrap, carry an icon so the
/// kind is legible at a glance, stay long enough to finish reading, and can
/// be dismissed early.
///
/// Top, not bottom: the composer and its send button live along the bottom
/// edge, and a toast landing there covered the very control the person had
/// just pressed — including the draft it was telling them had been kept.
enum ToastKind { info, success, warning, error }

class ToastItem {
  const ToastItem({
    required this.id,
    required this.kind,
    required this.text,
    required this.duration,
    this.title,
  });

  final int id;
  final ToastKind kind;
  final String text;

  /// Optional heading above the message, for when the kind alone is not enough.
  final String? title;

  /// Time on screen. [Duration.zero] keeps it until dismissed.
  final Duration duration;
}

/// Reading time, floored and capped.
///
/// A fixed 3.2s suited "已保存" and lost every sentence longer than that.
/// Roughly 14 characters a second is a slow, distracted read — the right pace
/// to assume for something that appeared without being asked for.
Duration _readingTime(String text, String? title) {
  final chars = text.length + (title?.length ?? 0);
  final ms = ((chars / 14) * 1000).round() + 1600;
  return Duration(milliseconds: ms.clamp(3600, 12000));
}

/// Errors are worth keeping on screen; a duplicate of one is not.
class ToastController extends Notifier<List<ToastItem>> {
  int _seq = 0;

  @override
  List<ToastItem> build() => const [];

  int push(
    ToastKind kind,
    String text, {
    String? title,
    Duration? duration,
  }) {
    final existing = state.where((t) => t.text == text && t.kind == kind);
    if (existing.isNotEmpty) return existing.first.id;

    final id = ++_seq;
    final life = duration ?? _readingTime(text, title);
    final item = ToastItem(
      id: id,
      kind: kind,
      text: text,
      title: title,
      duration: life,
    );
    state = [...state, item];
    if (life > Duration.zero) Timer(life, () => remove(id));
    return id;
  }

  int info(String text, {String? title, Duration? duration}) =>
      push(ToastKind.info, text, title: title, duration: duration);

  int success(String text, {String? title, Duration? duration}) =>
      push(ToastKind.success, text, title: title, duration: duration);

  int warning(String text, {String? title, Duration? duration}) =>
      push(ToastKind.warning, text, title: title, duration: duration);

  int error(String text, {String? title, Duration? duration}) =>
      push(ToastKind.error, text, title: title, duration: duration);

  void remove(int id) {
    state = state.where((t) => t.id != id).toList();
  }

  void clear() => state = const [];
}

final toastProvider =
    NotifierProvider<ToastController, List<ToastItem>>(ToastController.new);

/// Overlay host — stack this above the app content.
class ToastHost extends ConsumerWidget {
  const ToastHost({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final items = ref.watch(toastProvider);
    if (items.isEmpty) return const SizedBox.shrink();
    return SafeArea(
      child: Align(
        alignment: Alignment.topCenter,
        child: Padding(
          padding: const EdgeInsets.fromLTRB(12, 12, 12, 0),
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 416),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                for (final item in items)
                  _ToastCard(
                    key: ValueKey(item.id),
                    item: item,
                    onDismiss: () =>
                        ref.read(toastProvider.notifier).remove(item.id),
                  ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class _ToastCard extends ConsumerStatefulWidget {
  const _ToastCard({super.key, required this.item, required this.onDismiss});

  final ToastItem item;
  final VoidCallback onDismiss;

  @override
  ConsumerState<_ToastCard> createState() => _ToastCardState();
}

/// Mirrors the web card's `animate-fade-down`: it enters from above, because
/// that is the edge it is anchored to.
class _ToastCardState extends ConsumerState<_ToastCard>
    with SingleTickerProviderStateMixin {
  late final AnimationController _anim = AnimationController(
    vsync: this,
    duration: const Duration(milliseconds: 340),
  )..forward();

  @override
  void dispose() {
    _anim.dispose();
    super.dispose();
  }

  IconData get _icon => switch (widget.item.kind) {
        ToastKind.info => Icons.info_outline,
        ToastKind.success => Icons.check_circle_outline,
        ToastKind.warning => Icons.warning_amber_outlined,
        ToastKind.error => Icons.error_outline,
      };

  Color _tone(BossipTokens t) => switch (widget.item.kind) {
        ToastKind.info => t.n700,
        ToastKind.success => t.sage,
        ToastKind.warning => t.n800,
        ToastKind.error => t.danger,
      };

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final item = widget.item;
    final curve = CurvedAnimation(parent: _anim, curve: Curves.easeOut);

    return FadeTransition(
      opacity: curve,
      child: SlideTransition(
        position: Tween<Offset>(
          begin: const Offset(0, -0.12),
          end: Offset.zero,
        ).animate(curve),
        child: Container(
          margin: const EdgeInsets.only(bottom: 8),
          decoration: BoxDecoration(
            color: t.card,
            border: Border.all(color: t.hair),
            borderRadius: BorderRadius.circular(Radii.xl2),
            boxShadow: [
              BoxShadow(
                color: Colors.black.withValues(alpha: 0.12),
                blurRadius: 18,
                offset: const Offset(0, 6),
              ),
            ],
          ),
          padding: const EdgeInsets.fromLTRB(14, 12, 8, 12),
          child: Row(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Padding(
                padding: const EdgeInsets.only(top: 1),
                child: Icon(_icon, size: 16, color: _tone(t)),
              ),
              const SizedBox(width: 10),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    if (item.title != null)
                      Padding(
                        padding: const EdgeInsets.only(bottom: 2),
                        child: Text(
                          item.title!,
                          style: TextStyle(
                            fontSize: FontSizes.sm,
                            fontWeight: FontWeight.w500,
                            color: t.ink,
                          ),
                        ),
                      ),
                    Text(
                      item.text,
                      style: TextStyle(
                        fontSize: FontSizes.sm,
                        height: 1.5,
                        color: t.ink,
                      ),
                    ),
                  ],
                ),
              ),
              const SizedBox(width: 4),
              IconButton(
                onPressed: widget.onDismiss,
                icon: Icon(Icons.close, size: 14, color: t.n600),
                tooltip: i18n.t('common:close'),
                visualDensity: VisualDensity.compact,
                constraints: const BoxConstraints.tightFor(width: 26, height: 26),
                padding: EdgeInsets.zero,
              ),
            ],
          ),
        ),
      ),
    );
  }
}
