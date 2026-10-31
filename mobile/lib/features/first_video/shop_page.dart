import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../shared/appearance/tokens.dart';
import '../../shared/appearance/type_scale.dart';
import '../../shared/i18n/i18n.dart';
import '../../shared/models/first_video.dart';
import '../../shared/router/paths.dart';
import 'api/first_video_api.dart';
import 'state/first_video_flow.dart';
import 'state/first_video_store.dart';
import 'widgets/flow_chrome.dart';

/// ① 你的店: industry (four chips), shop name, the one thing to push. Every
/// field can stay empty; the close button leaves the whole flow.
class FirstVideoShopPage extends ConsumerStatefulWidget {
  const FirstVideoShopPage({super.key});

  @override
  ConsumerState<FirstVideoShopPage> createState() => _FirstVideoShopPageState();
}

class _FirstVideoShopPageState extends ConsumerState<FirstVideoShopPage> {
  late final TextEditingController _name;
  late final TextEditingController _headline;

  @override
  void initState() {
    super.initState();
    final draft = ref.read(firstVideoDraftProvider);
    _name = TextEditingController(text: draft.shopName);
    _headline = TextEditingController(text: draft.headline);
  }

  @override
  void dispose() {
    _name.dispose();
    _headline.dispose();
    super.dispose();
  }

  Future<void> _leave() async {
    final i18n = ref.read(i18nProvider);
    final t = context.tokens;
    final leave = await showDialog<bool>(
      context: context,
      builder: (dialog) => AlertDialog(
        backgroundColor: t.card,
        title: Text(
          i18n.t('first_video:leave.title'),
          style: TextStyle(fontSize: FontSizes.xl, color: t.ink),
        ),
        content: Text(
          i18n.t('first_video:leave.body'),
          style: TextStyle(fontSize: FontSizes.base, color: t.n700),
        ),
        actions: [
          TextButton(
            key: const Key('fv-leave-stay'),
            onPressed: () => Navigator.of(dialog).pop(false),
            child: Text(i18n.t('first_video:leave.stay')),
          ),
          TextButton(
            key: const Key('fv-leave-confirm'),
            onPressed: () => Navigator.of(dialog).pop(true),
            child: Text(i18n.t('first_video:leave.confirm')),
          ),
        ],
      ),
    );
    if (leave != true || !mounted) return;
    await ref.read(firstVideoProvider.notifier).markSkipped();
    if (mounted) context.go(Paths.app);
  }

  void _next() => context.push(Paths.firstVideoTemplate);

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final draft = ref.watch(firstVideoDraftProvider);
    final drafts = ref.read(firstVideoDraftProvider.notifier);
    final catalog = ref.watch(firstVideoCatalogProvider).valueOrNull;
    String industryLabel(String id) {
      for (final item in catalog?.industries ?? const <FirstVideoOption>[]) {
        if (item.id == id) return item.label;
      }
      return i18n.t('first_video:shop.industries.$id');
    }

    return FirstVideoScaffold(
      step: 1,
      leading: FirstVideoTopIcon(
        key: const Key('fv-close'),
        icon: Icons.close,
        tooltip: i18n.t('first_video:leave.confirm'),
        onTap: _leave,
      ),
      trailing: FirstVideoTopAction(
        key: const Key('fv-skip'),
        label: i18n.t('first_video:skip'),
        onTap: _next,
      ),
      footer: FirstVideoPrimaryButton(
        key: const Key('fv-next'),
        label: i18n.t('first_video:next'),
        onTap: _next,
      ),
      body: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          FirstVideoHeading(
            title: i18n.t('first_video:shop.title'),
            lede: i18n.t('first_video:shop.lede'),
          ),
          const SizedBox(height: 24),
          _Label(i18n.t('first_video:shop.industry')),
          const SizedBox(height: 10),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              for (final id in firstVideoIndustries)
                _IndustryChip(
                  key: Key('fv-industry-$id'),
                  label: industryLabel(id),
                  selected: draft.industry == id,
                  onTap: () =>
                      drafts.setIndustry(draft.industry == id ? null : id),
                ),
            ],
          ),
          const SizedBox(height: 24),
          _Label(i18n.t('first_video:shop.name')),
          const SizedBox(height: 10),
          _Field(
            key: const Key('fv-shop-name'),
            controller: _name,
            hint: i18n.t('first_video:shop.namePlaceholder'),
            onChanged: drafts.setShopName,
            action: TextInputAction.next,
          ),
          const SizedBox(height: 24),
          _Label(i18n.t('first_video:shop.headline')),
          const SizedBox(height: 10),
          _Field(
            key: const Key('fv-headline'),
            controller: _headline,
            hint: i18n.t('first_video:shop.headlinePlaceholder'),
            onChanged: drafts.setHeadline,
            action: TextInputAction.done,
            onSubmitted: (_) => _next(),
          ),
          const SizedBox(height: 8),
          Text(
            i18n.t('first_video:shop.headlineHint'),
            style: TextStyle(fontSize: FontSizes.sm, color: t.n600),
          ),
          const SizedBox(height: 24),
        ],
      ),
    );
  }
}

class _Label extends StatelessWidget {
  const _Label(this.text);

  final String text;

  @override
  Widget build(BuildContext context) => Text(
    text,
    style: TextStyle(
      fontSize: FontSizes.md,
      fontWeight: FontWeight.w600,
      color: context.tokens.n700,
    ),
  );
}

class _IndustryChip extends StatelessWidget {
  const _IndustryChip({
    super.key,
    required this.label,
    required this.selected,
    required this.onTap,
  });

  final String label;
  final bool selected;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return Material(
      color: selected ? t.a100 : t.card,
      shape: RoundedRectangleBorder(
        side: BorderSide(color: selected ? t.a700 : t.hair),
        borderRadius: BorderRadius.circular(Radii.full),
      ),
      child: InkWell(
        borderRadius: BorderRadius.circular(Radii.full),
        onTap: onTap,
        child: Padding(
          padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
          child: Text(
            label,
            style: TextStyle(
              fontSize: FontSizes.base,
              fontWeight: selected ? FontWeight.w600 : FontWeight.w400,
              color: selected ? t.a800 : t.ink,
            ),
          ),
        ),
      ),
    );
  }
}

class _Field extends StatelessWidget {
  const _Field({
    super.key,
    required this.controller,
    required this.hint,
    required this.onChanged,
    required this.action,
    this.onSubmitted,
  });

  final TextEditingController controller;
  final String hint;
  final ValueChanged<String> onChanged;
  final TextInputAction action;
  final ValueChanged<String>? onSubmitted;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return TextField(
      controller: controller,
      onChanged: onChanged,
      onSubmitted: onSubmitted,
      textInputAction: action,
      maxLength: 40,
      style: TextStyle(fontSize: FontSizes.xl, color: t.ink),
      decoration: InputDecoration(
        hintText: hint,
        hintStyle: TextStyle(fontSize: FontSizes.xl, color: t.n500),
        counterText: '',
        filled: true,
        fillColor: t.surface,
        contentPadding: const EdgeInsets.symmetric(
          horizontal: 16,
          vertical: 15,
        ),
        enabledBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(Radii.lg),
          borderSide: BorderSide(color: t.hair),
        ),
        focusedBorder: OutlineInputBorder(
          borderRadius: BorderRadius.circular(Radii.lg),
          borderSide: BorderSide(color: t.a700, width: 1.4),
        ),
      ),
    );
  }
}
