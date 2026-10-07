import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../shared/appearance/tokens.dart';
import '../../shared/appearance/type_scale.dart';
import '../../shared/i18n/i18n.dart';
import '../../shared/legal/legal_links.dart';
import '../../shared/router/paths.dart';

/// Public, bundled document reader. No authentication or content API required.
class LegalPage extends ConsumerWidget {
  const LegalPage({super.key, this.document});
  final String? document;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final i18n = ref.watch(i18nProvider);
    final t = context.tokens;
    final id = legalDocuments.contains(document) ? document : null;
    final title = id == null
        ? i18n.t('legal:title')
        : i18n.t('legal:documents.$id.title');
    final prefix = i18n.language.startsWith('zh') ? '' : 'en/';
    final url =
        '${i18n.t('legal:publicOrigin')}/legal/$prefix${id == null ? '' : '${legalDocumentSlug(id)}/'}';
    return Scaffold(
      backgroundColor: t.bg,
      appBar: AppBar(
        title: Text(title, style: const TextStyle(fontSize: FontSizes.lg)),
        leading: IconButton(
          tooltip: i18n.t('legal:back'),
          icon: const Icon(Icons.arrow_back),
          onPressed: () =>
              context.canPop() ? context.pop() : context.go(Paths.landing),
        ),
        actions: [
          IconButton(
            tooltip: i18n.t('legal:openBrowser'),
            icon: const Icon(Icons.open_in_new, size: 20),
            onPressed: () =>
                openLegalExternal(context, url, i18n.t('legal:openFailed')),
          ),
        ],
      ),
      body: SelectionArea(
        child: ListView(
          key: PageStorageKey('legal-${i18n.language}-${id ?? 'index'}'),
          padding: const EdgeInsets.fromLTRB(22, 20, 22, 40),
          children: [
            Text(
              i18n.t('legal:publicAccess'),
              style: TextStyle(fontSize: FontSizes.xs, color: t.a700),
            ),
            const SizedBox(height: 16),
            Text(
              title,
              style: TextStyle(
                fontSize: FontSizes.xl2,
                fontWeight: FontWeight.w600,
                color: t.ink,
              ),
            ),
            const SizedBox(height: 12),
            Text(
              id == null
                  ? i18n.t('legal:intro')
                  : i18n.t('legal:documents.$id.summary'),
              style: TextStyle(
                fontSize: FontSizes.sm,
                color: t.n700,
                height: 1.7,
              ),
            ),
            const SizedBox(height: 12),
            Text(
              '${i18n.t('legal:updated')} ${i18n.t('legal:updatedAt')} · ${i18n.t('legal:versionLabel')} ${i18n.t('legal:version')}',
              style: TextStyle(fontSize: FontSizes.xs, color: t.n700),
            ),
            const SizedBox(height: 24),
            if (id == null)
              ...legalDocuments.map(
                (key) => Padding(
                  padding: const EdgeInsets.only(bottom: 10),
                  child: ListTile(
                    shape: RoundedRectangleBorder(
                      borderRadius: BorderRadius.circular(Radii.xl),
                      side: BorderSide(color: t.hair),
                    ),
                    tileColor: t.card,
                    title: Text(i18n.t('legal:documents.$key.title')),
                    subtitle: Text(
                      i18n.t('legal:documents.$key.summary'),
                      style: TextStyle(fontSize: FontSizes.xs, color: t.n700),
                    ),
                    trailing: const Icon(Icons.chevron_right, size: 18),
                    onTap: () => context.push(Paths.legalDocument(key)),
                  ),
                ),
              )
            else
              ...i18n
                  .tList('legal:documents.$id.sections')
                  .whereType<Map<String, dynamic>>()
                  .map(
                    (section) => Padding(
                      padding: const EdgeInsets.only(bottom: 28),
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            section['title'] as String,
                            style: TextStyle(
                              fontSize: FontSizes.lg,
                              fontWeight: FontWeight.w600,
                              color: t.ink,
                            ),
                          ),
                          const SizedBox(height: 10),
                          for (final paragraph
                              in (section['paragraphs'] as List).cast<String>())
                            Padding(
                              padding: const EdgeInsets.only(bottom: 8),
                              child: Text(
                                paragraph,
                                style: TextStyle(
                                  fontSize: FontSizes.base,
                                  height: 1.8,
                                  color: t.ink,
                                ),
                              ),
                            ),
                        ],
                      ),
                    ),
                  ),
            if (id == 'contact') ...[
              SelectableText(i18n.t('legal:contactEmail')),
              const SizedBox(height: 12),
              for (final key in [
                'contactAction',
                'privacyAction',
                'reportAction',
              ])
                OutlinedButton(
                  onPressed: () => openLegalExternal(
                    context,
                    'mailto:${i18n.t('legal:contactEmail')}?subject=${Uri.encodeComponent('BossIP ${i18n.t('legal:$key')}')}',
                    i18n.t('legal:openFailed'),
                  ),
                  child: Text(i18n.t('legal:$key')),
                ),
              Text(
                i18n.t('legal:emailHint'),
                style: TextStyle(fontSize: FontSizes.xs, color: t.n700),
              ),
            ],
            if (id == 'third-parties')
              ...i18n
                  .tList('legal:providerLinks')
                  .whereType<Map<String, dynamic>>()
                  .map(
                    (link) => TextButton(
                      onPressed: () => openLegalExternal(
                        context,
                        link['url'] as String,
                        i18n.t('legal:openFailed'),
                      ),
                      child: Text(link['label'] as String),
                    ),
                  ),
            if (id != null)
              TextButton(
                onPressed: () => context.push(Paths.legal),
                child: Text(i18n.t('legal:directory')),
              ),
            const SizedBox(height: 16),
            const LegalFooter(),
          ],
        ),
      ),
    );
  }
}
