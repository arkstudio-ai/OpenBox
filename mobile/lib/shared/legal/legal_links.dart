import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:url_launcher/url_launcher.dart';

import '../appearance/tokens.dart';
import '../appearance/type_scale.dart';
import '../i18n/i18n.dart';
import '../router/paths.dart';
import '../widgets/labeled_checkbox.dart';

const legalVersion = '2026-09-28';
const legalDocuments = [
  'terms',
  'privacy',
  'ai',
  'disclaimer',
  'contact',
  'collection',
  'third-parties',
  'permissions',
];

String legalDocumentSlug(String id) =>
    const ['collection', 'third-parties', 'permissions'].contains(id)
    ? 'privacy/$id'
    : id;

Future<void> openLegalExternal(
  BuildContext context,
  String value,
  String failure,
) async {
  try {
    if (await launchUrl(
      Uri.parse(value),
      mode: LaunchMode.externalApplication,
    )) {
      return;
    }
  } catch (_) {
    /* The URL remains available to copy in the document. */
  }
  if (context.mounted) {
    ScaffoldMessenger.of(
      context,
    ).showSnackBar(SnackBar(content: Text(failure)));
  }
}

class LegalLinks extends ConsumerWidget {
  const LegalLinks({super.key, this.compact = false});
  final bool compact;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final i18n = ref.watch(i18nProvider);
    return Wrap(
      spacing: 4,
      children: [
        for (final id in legalDocuments.take(compact ? 3 : 5))
          TextButton(
            onPressed: () => context.push(Paths.legalDocument(id)),
            style: TextButton.styleFrom(
              foregroundColor: context.tokens.n700,
              textStyle: const TextStyle(fontSize: FontSizes.xs),
            ),
            child: Text(i18n.t('legal:documents.$id.title')),
          ),
      ],
    );
  }
}

class LegalFooter extends ConsumerWidget {
  const LegalFooter({super.key});
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final i18n = ref.watch(i18nProvider);
    final t = context.tokens;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Divider(color: t.hair),
        const LegalLinks(),
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: 8),
          child: Text(
            i18n.t('legal:operator'),
            style: TextStyle(fontSize: FontSizes.xs, color: t.n700),
          ),
        ),
        TextButton(
          onPressed: () => openLegalExternal(
            context,
            i18n.t('legal:icpUrl'),
            i18n.t('legal:openFailed'),
          ),
          child: Text(
            i18n.t('legal:appIcp'),
            style: TextStyle(fontSize: FontSizes.xs, color: t.n700),
          ),
        ),
      ],
    );
  }
}

class LegalConsent extends ConsumerWidget {
  const LegalConsent({
    super.key,
    required this.accepted,
    required this.onChanged,
  });
  final bool accepted;
  final ValueChanged<bool> onChanged;
  @override
  Widget build(BuildContext context, WidgetRef ref) => Column(
    crossAxisAlignment: CrossAxisAlignment.start,
    children: [
      LabeledCheckbox(
        value: accepted,
        onChanged: onChanged,
        label: ref.watch(i18nProvider).t('legal:consentLabel'),
      ),
      const LegalLinks(compact: true),
    ],
  );
}
