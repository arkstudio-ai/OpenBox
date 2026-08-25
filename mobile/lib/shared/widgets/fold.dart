import 'package:flutter/material.dart';

/// Port of the web `.fold` accordion (grid-template-rows 0fr→1fr,
/// 220ms ease-out): animated reveal that clips its child while closed.
class Fold extends StatelessWidget {
  const Fold({super.key, required this.open, required this.child});

  final bool open;
  final Widget child;

  @override
  Widget build(BuildContext context) {
    return AnimatedSize(
      duration: const Duration(milliseconds: 220),
      curve: Curves.easeOut,
      alignment: Alignment.topCenter,
      child: open
          ? child
          : const SizedBox(width: double.infinity, height: 0),
    );
  }
}
