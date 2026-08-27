import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/skill.dart';
import '../utils/parse_mcp_config.dart';
import 'sheet_scaffold.dart';

/// The MCP half of the add sheet.
///
/// Split out of [UploadSheet] so each stays readable, and because these two
/// entry routes — fill in the pieces, or paste the snippet a README gave you
/// — are the only part of adding a server that has any real shape to it.
class McpFormState {
  McpFormState()
      : name = TextEditingController(),
        // npx is what almost every published server's README starts with.
        command = TextEditingController(text: 'npx'),
        args = TextEditingController(),
        env = TextEditingController(),
        url = TextEditingController(),
        headers = TextEditingController(),
        json = TextEditingController();

  final TextEditingController name;
  final TextEditingController command;
  final TextEditingController args;
  final TextEditingController env;
  final TextEditingController url;
  final TextEditingController headers;
  final TextEditingController json;

  String tab = 'form'; // form | json
  String transport = 'stdio'; // stdio | remote
  String? jsonError;

  bool get isStdio => transport == 'stdio';

  bool get canSubmit => tab == 'json'
      ? json.text.trim().isNotEmpty
      : name.text.trim().isNotEmpty &&
          (isStdio ? command.text.trim().isNotEmpty : url.text.trim().isNotEmpty);

  /// The single server the form describes, or null when it is incomplete.
  McpConfig? buildConfig() {
    if (isStdio) {
      final command = this.command.text.trim();
      if (command.isEmpty) return null;
      return McpConfig(
        type: 'stdio',
        command: command,
        // Whitespace-split: MCP commands are flat argv lists in practice.
        args: args.text.trim().isEmpty
            ? const []
            : args.text.trim().split(RegExp(r'\s+')),
        env: parsePairs(env.text),
      );
    }
    final url = this.url.text.trim();
    if (url.isEmpty) return null;
    return McpConfig(
      type: 'remote',
      url: url,
      headers: parsePairs(headers.text),
    );
  }

  void dispose() {
    for (final controller in [name, command, args, env, url, headers, json]) {
      controller.dispose();
    }
  }
}

class McpConfigForm extends ConsumerWidget {
  const McpConfigForm({
    super.key,
    required this.state,
    required this.onChanged,
  });

  final McpFormState state;

  /// The sheet owns the state object, so a field change is reported rather
  /// than mutated behind its back.
  final VoidCallback onChanged;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Row(
          children: [
            for (final tab in const ['form', 'json'])
              Padding(
                padding: const EdgeInsets.only(right: 4),
                child: _Chip(
                  label: i18n.t('skills:upload.mcpTab.$tab'),
                  selected: state.tab == tab,
                  onTap: () {
                    state.tab = tab;
                    onChanged();
                  },
                ),
              ),
          ],
        ),
        if (state.tab == 'form') ...[
          SheetField(
            label: i18n.t('skills:upload.mcpName'),
            placeholder: i18n.t('skills:upload.namePlaceholder'),
            controller: state.name,
            onChanged: (_) => onChanged(),
          ),
          Padding(
            padding: const EdgeInsets.only(top: 12),
            child: Row(
              children: [
                for (final transport in const ['stdio', 'remote'])
                  Padding(
                    padding: const EdgeInsets.only(right: 4),
                    child: _Chip(
                      label: i18n.t('skills:upload.transport.$transport'),
                      selected: state.transport == transport,
                      solid: true,
                      onTap: () {
                        state.transport = transport;
                        onChanged();
                      },
                    ),
                  ),
              ],
            ),
          ),
          if (state.isStdio) ...[
            SheetField(
              label: i18n.t('skills:upload.command'),
              controller: state.command,
              onChanged: (_) => onChanged(),
            ),
            SheetField(
              label: i18n.t('skills:upload.args'),
              placeholder: i18n.t('skills:upload.argsPlaceholder'),
              controller: state.args,
            ),
            SheetField(
              label: i18n.t('skills:upload.env'),
              placeholder: i18n.t('skills:upload.envPlaceholder'),
              controller: state.env,
              lines: 3,
              mono: true,
            ),
          ] else ...[
            SheetField(
              label: i18n.t('skills:upload.mcpUrl'),
              placeholder: i18n.t('skills:upload.urlPlaceholder'),
              controller: state.url,
              onChanged: (_) => onChanged(),
            ),
            SheetField(
              label: i18n.t('skills:upload.headers'),
              placeholder: i18n.t('skills:upload.headersPlaceholder'),
              controller: state.headers,
              lines: 3,
              mono: true,
            ),
            Padding(
              padding: const EdgeInsets.only(top: 6),
              child: Text(
                i18n.t('skills:upload.remoteHint'),
                style: TextStyle(
                    fontSize: FontSizes.xs, height: 1.6, color: t.n600),
              ),
            ),
          ],
        ] else ...[
          SheetField(
            label: i18n.t('skills:upload.jsonLabel'),
            placeholder: i18n.t('skills:upload.jsonPlaceholder'),
            controller: state.json,
            lines: 8,
            mono: true,
            onChanged: (_) {
              state.jsonError = null;
              onChanged();
            },
          ),
          Padding(
            padding: const EdgeInsets.only(top: 6),
            child: Text(
              i18n.t('skills:upload.jsonHint'),
              style:
                  TextStyle(fontSize: FontSizes.xs, height: 1.6, color: t.n600),
            ),
          ),
          if (state.jsonError != null)
            Padding(
              padding: const EdgeInsets.only(top: 6),
              child: Text(
                state.jsonError!,
                style: TextStyle(fontSize: FontSizes.xs, color: t.danger),
              ),
            ),
        ],
      ],
    );
  }
}

class _Chip extends StatelessWidget {
  const _Chip({
    required this.label,
    required this.selected,
    required this.onTap,
    this.solid = false,
  });

  final String label;
  final bool selected;
  final VoidCallback onTap;

  /// The transport picker uses the filled treatment; the tab row is quieter.
  final bool solid;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final background = selected
        ? (solid ? t.ink : t.hairSoft)
        : Colors.transparent;
    final foreground = selected ? (solid ? t.bg : t.ink) : t.n600;
    return GestureDetector(
      onTap: onTap,
      child: Container(
        padding: const EdgeInsets.symmetric(horizontal: 11, vertical: 5),
        decoration: BoxDecoration(
          color: background,
          borderRadius: BorderRadius.circular(Radii.full),
        ),
        child: Text(
          label,
          style: TextStyle(fontSize: FontSizes.xs, color: foreground),
        ),
      ),
    );
  }
}
