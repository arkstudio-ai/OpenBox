import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/skill.dart';

/// The shell every skill-centre sheet uses. The web centre puts these in
/// centred modals; a phone gets a bottom sheet that grows with its content
/// and keeps the confirm button above the keyboard.
class SkillSheet extends ConsumerWidget {
  const SkillSheet({
    super.key,
    required this.title,
    required this.confirmLabel,
    required this.onConfirm,
    required this.child,
    this.subtitle,
    this.header,
    this.error,
    this.busy = false,
    this.canConfirm = true,
    this.cancelLabel,
  });

  final String title;
  final String? subtitle;

  /// Optional row above the title — the install sheet shows the entry's icon.
  final Widget? header;
  final String confirmLabel;

  /// Null disables the confirm button without dimming the whole sheet.
  final VoidCallback? onConfirm;
  final Widget child;
  final String? error;
  final bool busy;
  final bool canConfirm;
  final String? cancelLabel;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final media = MediaQuery.of(context);
    return Padding(
      padding: EdgeInsets.only(bottom: media.viewInsets.bottom),
      child: SafeArea(
        top: false,
        child: ConstrainedBox(
          constraints: BoxConstraints(maxHeight: media.size.height * 0.88),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Padding(
                padding: const EdgeInsets.fromLTRB(18, 16, 18, 8),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    if (header != null) ...[
                      header!,
                      const SizedBox(height: 10),
                    ],
                    Text(
                      title,
                      style: TextStyle(
                        fontSize: FontSizes.lg,
                        fontWeight: FontWeight.w500,
                        color: t.ink,
                      ),
                    ),
                    if (subtitle != null)
                      Padding(
                        padding: const EdgeInsets.only(top: 4),
                        child: Text(
                          subtitle!,
                          style: TextStyle(
                            fontSize: FontSizes.xs,
                            height: 1.6,
                            color: t.n600,
                          ),
                        ),
                      ),
                  ],
                ),
              ),
              Flexible(
                child: SingleChildScrollView(
                  padding: const EdgeInsets.fromLTRB(18, 0, 18, 8),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      child,
                      if (error != null)
                        Container(
                          margin: const EdgeInsets.only(top: 12),
                          padding: const EdgeInsets.symmetric(
                              horizontal: 11, vertical: 8),
                          decoration: BoxDecoration(
                            color: t.dangerSoft,
                            borderRadius: BorderRadius.circular(Radii.md),
                          ),
                          child: Text(
                            error!,
                            style: TextStyle(
                              fontSize: FontSizes.xs,
                              height: 1.6,
                              color: t.danger,
                            ),
                          ),
                        ),
                    ],
                  ),
                ),
              ),
              Padding(
                padding: const EdgeInsets.fromLTRB(18, 8, 18, 14),
                child: Row(
                  mainAxisAlignment: MainAxisAlignment.end,
                  children: [
                    TextButton(
                      onPressed:
                          busy ? null : () => Navigator.of(context).pop(),
                      child: Text(
                        cancelLabel ?? i18n.t('skills:common.cancel'),
                        style:
                            TextStyle(fontSize: FontSizes.sm, color: t.n700),
                      ),
                    ),
                    const SizedBox(width: 8),
                    FilledButton(
                      onPressed: busy || !canConfirm ? null : onConfirm,
                      style: FilledButton.styleFrom(
                        backgroundColor: t.ink,
                        foregroundColor: t.bg,
                        disabledBackgroundColor: t.ink.withValues(alpha: 0.4),
                        disabledForegroundColor: t.bg,
                        shape: RoundedRectangleBorder(
                          borderRadius: BorderRadius.circular(Radii.full),
                        ),
                      ),
                      child: Text(
                        confirmLabel,
                        style: const TextStyle(fontSize: FontSizes.sm),
                      ),
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
}

/// Labelled text input, matching the web centre's `FIELD` class.
class SheetField extends StatelessWidget {
  const SheetField({
    super.key,
    required this.label,
    required this.controller,
    this.placeholder,
    this.lines = 1,
    this.mono = false,
    this.obscure = false,
    this.onChanged,
  });

  final String label;
  final TextEditingController controller;
  final String? placeholder;
  final int lines;
  final bool mono;
  final bool obscure;
  final ValueChanged<String>? onChanged;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Padding(
      padding: const EdgeInsets.only(top: 12),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(label, style: TextStyle(fontSize: FontSizes.xs, color: t.n600)),
          const SizedBox(height: 4),
          TextField(
            controller: controller,
            maxLines: obscure ? 1 : lines,
            minLines: obscure ? 1 : lines,
            obscureText: obscure,
            autocorrect: !mono,
            enableSuggestions: !mono,
            onChanged: onChanged,
            style: TextStyle(
              fontSize: mono ? FontSizes.xs : FontSizes.sm,
              height: mono ? 1.6 : null,
              color: t.ink,
              fontFamily: mono ? 'Menlo' : null,
              fontFamilyFallback: mono ? const ['monospace'] : null,
            ),
            decoration: InputDecoration(
              isDense: true,
              filled: true,
              fillColor: t.bg,
              hintText: placeholder,
              hintStyle: TextStyle(
                fontSize: mono ? FontSizes.xs : FontSizes.sm,
                color: t.n600,
              ),
              contentPadding:
                  const EdgeInsets.symmetric(horizontal: 10, vertical: 9),
              border: OutlineInputBorder(
                borderRadius: BorderRadius.circular(Radii.md),
                borderSide: BorderSide(color: t.hair),
              ),
              enabledBorder: OutlineInputBorder(
                borderRadius: BorderRadius.circular(Radii.md),
                borderSide: BorderSide(color: t.hair),
              ),
              focusedBorder: OutlineInputBorder(
                borderRadius: BorderRadius.circular(Radii.md),
                borderSide: BorderSide(color: t.accent),
              ),
            ),
          ),
        ],
      ),
    );
  }
}

/// Credentials a server declares. A server installed without its key connects
/// and then fails on every call, so they are collected before the install
/// rather than after the first failure.
class EnvFields extends ConsumerWidget {
  const EnvFields({
    super.key,
    required this.servers,
    required this.controllers,
  });

  final List<CatalogEntry> servers;

  /// `serverId` → `envKey` → controller, owned by the caller so the values
  /// survive rebuilds.
  final Map<String, Map<String, TextEditingController>> controllers;

  static bool missingRequired(
    List<CatalogEntry> servers,
    Map<String, Map<String, TextEditingController>> controllers,
  ) =>
      servers.any((server) => server.requiredEnv.any(
            (field) =>
                (controllers[server.id]?[field.key]?.text ?? '').trim().isEmpty,
          ));

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        for (final server in servers) ...[
          Padding(
            padding: const EdgeInsets.only(top: 14),
            child: Text(
              i18n.t('skills:install.credentialsFor',
                  vars: {'name': server.title}),
              style: TextStyle(
                fontSize: FontSizes.xs,
                fontWeight: FontWeight.w500,
                color: t.ink,
              ),
            ),
          ),
          for (final field in server.requiredEnv)
            SheetField(
              label: field.label,
              placeholder: field.key,
              obscure: field.secret,
              controller: controllers
                  .putIfAbsent(server.id, () => {})
                  .putIfAbsent(field.key, TextEditingController.new),
            ),
        ],
      ],
    );
  }
}
