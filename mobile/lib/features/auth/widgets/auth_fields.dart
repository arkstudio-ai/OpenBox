import 'package:flutter/material.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';

/// Labeled input matching the web auth field styling
/// (`features/auth/components/AuthFields.tsx`): h-10.5 rounded-lg hairline
/// border on card, accent border on focus.
class AuthTextField extends StatelessWidget {
  const AuthTextField({
    super.key,
    required this.label,
    required this.controller,
    this.placeholder,
    this.obscure = false,
    this.suffix,
    this.keyboardType,
    this.autofillHints,
    this.textInputAction,
    this.onSubmitted,
  });

  final String label;
  final TextEditingController controller;
  final String? placeholder;
  final bool obscure;
  final Widget? suffix;
  final TextInputType? keyboardType;
  final Iterable<String>? autofillHints;
  final TextInputAction? textInputAction;
  final ValueChanged<String>? onSubmitted;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          label,
          style: TextStyle(
            fontSize: FontSizes.sm,
            color: t.n700,
            fontWeight: FontWeight.w500,
          ),
        ),
        const SizedBox(height: 6),
        TextField(
          controller: controller,
          obscureText: obscure,
          keyboardType: keyboardType,
          autofillHints: autofillHints,
          textInputAction: textInputAction,
          onSubmitted: onSubmitted,
          style: TextStyle(fontSize: FontSizes.base, color: t.ink),
          decoration: InputDecoration(
            hintText: placeholder,
            hintStyle: TextStyle(color: t.n500, fontSize: FontSizes.base),
            suffixIcon: suffix,
            isDense: true,
            filled: true,
            fillColor: t.card,
            contentPadding:
                const EdgeInsets.symmetric(horizontal: 12, vertical: 13),
            enabledBorder: OutlineInputBorder(
              borderRadius: BorderRadius.circular(Radii.lg),
              borderSide: BorderSide(color: t.hair),
            ),
            focusedBorder: OutlineInputBorder(
              borderRadius: BorderRadius.circular(Radii.lg),
              borderSide: BorderSide(color: t.accent),
            ),
          ),
        ),
      ],
    );
  }
}

/// Password field with show/hide toggle (web `PasswordField`).
class PasswordField extends StatefulWidget {
  const PasswordField({
    super.key,
    required this.label,
    required this.controller,
    required this.showLabel,
    required this.hideLabel,
    this.placeholder,
    this.onSubmitted,
  });

  final String label;
  final TextEditingController controller;
  final String showLabel;
  final String hideLabel;
  final String? placeholder;
  final ValueChanged<String>? onSubmitted;

  @override
  State<PasswordField> createState() => _PasswordFieldState();
}

class _PasswordFieldState extends State<PasswordField> {
  bool _visible = false;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return AuthTextField(
      label: widget.label,
      controller: widget.controller,
      placeholder: widget.placeholder,
      obscure: !_visible,
      autofillHints: const [AutofillHints.password],
      textInputAction: TextInputAction.done,
      onSubmitted: widget.onSubmitted,
      suffix: TextButton(
        onPressed: () => setState(() => _visible = !_visible),
        child: Text(
          _visible ? widget.hideLabel : widget.showLabel,
          style: TextStyle(fontSize: FontSizes.xs, color: t.n600),
        ),
      ),
    );
  }
}
