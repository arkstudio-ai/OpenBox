import 'package:flutter/material.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';

/// The six screens share one frame: a 44pt top row (back or close, the
/// four-step bar, skip), a heading, the body, and a pinned footer. No app
/// bar — the flow sits outside the workspace shell on purpose.
class FirstVideoScaffold extends StatelessWidget {
  const FirstVideoScaffold({
    super.key,
    required this.body,
    this.step,
    this.leading,
    this.trailing,
    this.footer,
    this.scrollable = true,
  });

  /// 1-based position on the four-step bar; null hides the bar.
  final int? step;
  final Widget? leading;
  final Widget? trailing;
  final Widget body;
  final Widget? footer;

  /// Form screens scroll (the keyboard needs room); the making and result
  /// screens are laid out to fit.
  final bool scrollable;

  static const steps = 4;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final content = Padding(
      padding: const EdgeInsets.symmetric(horizontal: 24),
      child: body,
    );
    return Scaffold(
      backgroundColor: t.bg,
      resizeToAvoidBottomInset: true,
      body: SafeArea(
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            SizedBox(
              height: 52,
              child: Padding(
                padding: const EdgeInsets.symmetric(horizontal: 12),
                child: Row(
                  children: [
                    SizedBox(width: 44, child: leading),
                    Expanded(
                      child: step == null
                          ? const SizedBox.shrink()
                          : _StepBar(step: step!, tokens: t),
                    ),
                    SizedBox(
                      width: 72,
                      child: Align(
                        alignment: Alignment.centerRight,
                        child: trailing,
                      ),
                    ),
                  ],
                ),
              ),
            ),
            Expanded(
              child: scrollable
                  ? SingleChildScrollView(
                      keyboardDismissBehavior:
                          ScrollViewKeyboardDismissBehavior.onDrag,
                      child: content,
                    )
                  : content,
            ),
            if (footer != null)
              Padding(
                padding: const EdgeInsets.fromLTRB(24, 12, 24, 16),
                child: footer,
              ),
          ],
        ),
      ),
    );
  }
}

class _StepBar extends StatelessWidget {
  const _StepBar({required this.step, required this.tokens});

  final int step;
  final BossipTokens tokens;

  @override
  Widget build(BuildContext context) => Row(
    mainAxisAlignment: MainAxisAlignment.center,
    children: [
      for (var i = 1; i <= FirstVideoScaffold.steps; i++)
        Container(
          margin: const EdgeInsets.symmetric(horizontal: 3),
          width: 22,
          height: 4,
          decoration: BoxDecoration(
            color: i <= step ? tokens.a700 : tokens.n300,
            borderRadius: BorderRadius.circular(Radii.full),
          ),
        ),
    ],
  );
}

/// Title + one-line lede at the top of a screen.
class FirstVideoHeading extends StatelessWidget {
  const FirstVideoHeading({super.key, required this.title, this.lede});

  final String title;
  final String? lede;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          title,
          style: TextStyle(
            fontSize: FontSizes.xl3,
            fontWeight: FontWeight.w800,
            height: 1.25,
            color: t.ink,
          ),
        ),
        if (lede != null) ...[
          const SizedBox(height: 6),
          Text(
            lede!,
            style: TextStyle(
              fontSize: FontSizes.base,
              height: 1.45,
              color: t.n700,
            ),
          ),
        ],
      ],
    );
  }
}

class FirstVideoPrimaryButton extends StatelessWidget {
  const FirstVideoPrimaryButton({
    super.key,
    required this.label,
    required this.onTap,
    this.icon,
  });

  final String label;
  final VoidCallback? onTap;
  final IconData? icon;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return FilledButton(
      onPressed: onTap,
      style: FilledButton.styleFrom(
        backgroundColor: t.a700,
        foregroundColor: t.bg,
        disabledBackgroundColor: t.n300,
        disabledForegroundColor: t.n600,
        minimumSize: const Size.fromHeight(54),
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(Radii.full),
        ),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          if (icon != null) ...[Icon(icon, size: 18), const SizedBox(width: 8)],
          Text(
            label,
            style: const TextStyle(
              fontSize: FontSizes.xl,
              fontWeight: FontWeight.w700,
            ),
          ),
        ],
      ),
    );
  }
}

class FirstVideoSecondaryButton extends StatelessWidget {
  const FirstVideoSecondaryButton({
    super.key,
    required this.label,
    required this.onTap,
    this.height = 48,
  });

  final String label;
  final VoidCallback? onTap;
  final double height;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return OutlinedButton(
      onPressed: onTap,
      style: OutlinedButton.styleFrom(
        foregroundColor: t.ink,
        side: BorderSide(color: t.hair),
        minimumSize: Size.fromHeight(height),
        padding: const EdgeInsets.symmetric(horizontal: 12),
        shape: RoundedRectangleBorder(
          borderRadius: BorderRadius.circular(Radii.full),
        ),
      ),
      child: Text(
        label,
        textAlign: TextAlign.center,
        style: const TextStyle(
          fontSize: FontSizes.lg,
          fontWeight: FontWeight.w600,
        ),
      ),
    );
  }
}

/// The quiet third action ("去首页看看还能做什么").
class FirstVideoLinkButton extends StatelessWidget {
  const FirstVideoLinkButton({
    super.key,
    required this.label,
    required this.onTap,
  });

  final String label;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return TextButton(
      onPressed: onTap,
      style: TextButton.styleFrom(
        foregroundColor: t.n700,
        minimumSize: const Size.fromHeight(44),
      ),
      child: Text(label, style: const TextStyle(fontSize: FontSizes.base)),
    );
  }
}

/// Top-row text action ("跳过").
class FirstVideoTopAction extends StatelessWidget {
  const FirstVideoTopAction({
    super.key,
    required this.label,
    required this.onTap,
  });

  final String label;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return TextButton(
      onPressed: onTap,
      style: TextButton.styleFrom(
        foregroundColor: t.n700,
        minimumSize: const Size(44, 44),
        padding: const EdgeInsets.symmetric(horizontal: 8),
      ),
      child: Text(label, style: const TextStyle(fontSize: FontSizes.base)),
    );
  }
}

/// Top-row icon action (back, close).
class FirstVideoTopIcon extends StatelessWidget {
  const FirstVideoTopIcon({
    super.key,
    required this.icon,
    required this.tooltip,
    required this.onTap,
  });

  final IconData icon;
  final String tooltip;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) => IconButton(
    icon: Icon(icon, size: 22, color: context.tokens.ink),
    tooltip: tooltip,
    onPressed: onTap,
  );
}
