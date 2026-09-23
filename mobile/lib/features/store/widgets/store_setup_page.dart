import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/router/paths.dart';
import '../../../shared/utils/error_text.dart';
import '../../../shared/widgets/toast.dart';
import '../../onboarding/state/onboarding_store.dart';
import '../models/store.dart';
import '../state/store_provider.dart';

/// Onboarding step after the welcome sheet (docs/OPS_CASE_PLAN.md §2.1):
/// pushes the "你的店" page once per account when the workspace has no store
/// yet. Returns true when it was shown. Waits for any guide on screen
/// (coach marks, the welcome sheet) so the two never overlap.
Future<bool> showStoreSetupIfNeeded(BuildContext context, WidgetRef ref) async {
  final onboarding = ref.read(onboardingProvider.notifier);
  if (!onboarding.shouldShow(Guides.storeSetup)) return false;
  StoreSnapshot snapshot;
  try {
    snapshot = await ref.read(storeProvider.future);
  } catch (_) {
    // Offline or a failed fetch: ask again on the next launch rather than
    // pushing a form that may duplicate a store registered elsewhere.
    return false;
  }
  if (snapshot.hasStore) {
    // Registered on the web or another device: nothing to ask, and no
    // second fetch on every launch.
    unawaited(onboarding.markSeen(Guides.storeSetup));
    return false;
  }
  final queue = ref.read(guideQueueProvider.notifier);
  await queue.whenIdle();
  if (!context.mounted || !queue.claim(Guides.storeSetup)) return false;
  try {
    await context.push<void>(Paths.storeSetup);
  } finally {
    queue.release(Guides.storeSetup);
    // Save and skip both mark inside the page; the back gesture counts as a
    // skip too, otherwise the page would nag on every launch.
    unawaited(onboarding.markSeen(Guides.storeSetup));
  }
  return true;
}

/// "你的店": name, industry (only open categories are selectable) and the
/// platforms the store runs on. Save posts the store; skip just leaves.
class StoreSetupPage extends ConsumerStatefulWidget {
  const StoreSetupPage({super.key});

  @override
  ConsumerState<StoreSetupPage> createState() => _StoreSetupPageState();
}

class _StoreSetupPageState extends ConsumerState<StoreSetupPage> {
  final _name = TextEditingController();
  String? _category;
  final _platforms = <String>{};
  bool _saving = false;

  @override
  void dispose() {
    _name.dispose();
    super.dispose();
  }

  void _leave() {
    if (!mounted) return;
    if (context.canPop()) {
      context.pop();
    } else {
      context.go(Paths.app);
    }
  }

  Future<void> _skip() async {
    unawaited(
      ref.read(onboardingProvider.notifier).markSeen(Guides.storeSetup),
    );
    _leave();
  }

  Future<void> _save(String category) async {
    final name = _name.text.trim();
    if (name.isEmpty || _saving) return;
    setState(() => _saving = true);
    // Read before awaiting: the element may be gone by the time this lands.
    final onboarding = ref.read(onboardingProvider.notifier);
    final toast = ref.read(toastProvider.notifier);
    final i18n = ref.read(i18nProvider);
    try {
      await ref
          .read(storeProvider.notifier)
          .create(
            name: name,
            category: category,
            mainPlatforms: _platforms.toList()..sort(),
          );
      unawaited(onboarding.markSeen(Guides.storeSetup));
      _leave();
    } catch (error) {
      toast.error(errorText(i18n, error));
      if (mounted) setState(() => _saving = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final i18n = ref.watch(i18nProvider);
    final snapshot = ref.watch(storeProvider).valueOrNull ??
        const StoreSnapshot();
    final open = snapshot.openCategories;
    final category = _category != null && open.contains(_category)
        ? _category!
        : (open.contains('food') ? 'food' : open.firstOrNull);
    final canSave = _name.text.trim().isNotEmpty &&
        category != null &&
        !_saving;
    return Scaffold(
      backgroundColor: t.bg,
      appBar: AppBar(
        backgroundColor: t.bg,
        title: Text(
          i18n.t('onboarding:store.title'),
          style: TextStyle(
            fontSize: FontSizes.lg,
            fontWeight: FontWeight.w500,
            color: t.ink,
          ),
        ),
      ),
      body: SafeArea(
        child: Column(
          children: [
            Expanded(
              child: ListView(
                padding: const EdgeInsets.fromLTRB(20, 4, 20, 20),
                keyboardDismissBehavior:
                    ScrollViewKeyboardDismissBehavior.onDrag,
                children: [
                  Text(
                    i18n.t('onboarding:store.lede'),
                    style: TextStyle(
                      fontSize: FontSizes.sm,
                      height: 1.6,
                      color: t.n700,
                    ),
                  ),
                  const SizedBox(height: 18),
                  _field(
                    t,
                    i18n.t('onboarding:store.name'),
                    TextField(
                      key: const Key('store-name'),
                      controller: _name,
                      maxLength: 80,
                      enabled: !_saving,
                      textInputAction: TextInputAction.done,
                      onChanged: (_) => setState(() {}),
                      style: TextStyle(fontSize: FontSizes.sm, color: t.ink),
                      decoration: _decoration(
                        t,
                        i18n.t('onboarding:store.namePlaceholder'),
                      ),
                    ),
                  ),
                  _field(
                    t,
                    i18n.t('onboarding:store.category'),
                    Wrap(
                      spacing: 6,
                      runSpacing: 6,
                      children: [
                        for (final id in storeCategories)
                          _categoryChip(t, i18n, id, category, open),
                      ],
                    ),
                  ),
                  _field(
                    t,
                    i18n.t('onboarding:store.platforms'),
                    Wrap(
                      spacing: 6,
                      runSpacing: 6,
                      children: [
                        for (final id in snapshot.platforms)
                          _platformChip(t, i18n, id),
                      ],
                    ),
                  ),
                  Text(
                    i18n.t('onboarding:store.platformsHint'),
                    style: TextStyle(fontSize: FontSizes.xs, color: t.n500),
                  ),
                ],
              ),
            ),
            Padding(
              padding: const EdgeInsets.fromLTRB(20, 8, 20, 16),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  FilledButton(
                    key: const Key('store-save'),
                    onPressed: canSave ? () => _save(category) : null,
                    style: FilledButton.styleFrom(
                      backgroundColor: t.a700,
                      foregroundColor: t.bg,
                      disabledBackgroundColor: t.a700.withValues(alpha: 0.4),
                      disabledForegroundColor: t.bg,
                      minimumSize: const Size.fromHeight(52),
                      shape: RoundedRectangleBorder(
                        borderRadius: BorderRadius.circular(Radii.full),
                      ),
                    ),
                    child: Text(
                      i18n.t(
                        _saving
                            ? 'onboarding:store.saving'
                            : 'onboarding:store.save',
                      ),
                      style: const TextStyle(
                        fontSize: FontSizes.lg,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                  ),
                  const SizedBox(height: 4),
                  TextButton(
                    key: const Key('store-skip'),
                    onPressed: _saving ? null : _skip,
                    style: TextButton.styleFrom(
                      minimumSize: const Size.fromHeight(44),
                    ),
                    child: Text(
                      i18n.t('onboarding:store.skip'),
                      style: TextStyle(fontSize: FontSizes.sm, color: t.n600),
                    ),
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _categoryChip(
    BossipTokens t,
    I18nState i18n,
    String id,
    String? current,
    List<String> open,
  ) {
    final enabled = open.contains(id) && !_saving;
    final selected = id == current;
    final label = i18n.t('onboarding:store.categories.$id');
    return ChoiceChip(
      key: Key('store-category-$id'),
      label: Text(
        open.contains(id)
            ? label
            : '$label · ${i18n.t('onboarding:store.comingSoon')}',
        style: const TextStyle(fontSize: FontSizes.sm),
      ),
      selected: selected,
      showCheckmark: false,
      selectedColor: t.a200,
      backgroundColor: t.card,
      disabledColor: t.card,
      labelStyle: TextStyle(color: enabled ? t.ink : t.n500),
      side: BorderSide(color: selected ? t.a700 : t.hair),
      onSelected: enabled ? (_) => setState(() => _category = id) : null,
    );
  }

  Widget _platformChip(BossipTokens t, I18nState i18n, String id) {
    final selected = _platforms.contains(id);
    return FilterChip(
      key: Key('store-platform-$id'),
      label: Text(
        i18n.t('onboarding:store.platformNames.$id'),
        style: const TextStyle(fontSize: FontSizes.sm),
      ),
      selected: selected,
      showCheckmark: false,
      selectedColor: t.a200,
      backgroundColor: t.card,
      labelStyle: TextStyle(color: t.ink),
      side: BorderSide(color: selected ? t.a700 : t.hair),
      onSelected: _saving
          ? null
          : (on) => setState(() => on ? _platforms.add(id) : _platforms.remove(id)),
    );
  }

  Widget _field(BossipTokens t, String label, Widget child) => Padding(
    padding: const EdgeInsets.only(bottom: 16),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(label, style: TextStyle(fontSize: FontSizes.xs, color: t.n600)),
        const SizedBox(height: 6),
        child,
      ],
    ),
  );

  InputDecoration _decoration(BossipTokens t, String hint) => InputDecoration(
    hintText: hint,
    hintStyle: TextStyle(fontSize: FontSizes.sm, color: t.n500),
    isDense: true,
    counterText: '',
    filled: true,
    fillColor: t.card,
    contentPadding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
    enabledBorder: OutlineInputBorder(
      borderRadius: BorderRadius.circular(Radii.md),
      borderSide: BorderSide(color: t.hair),
    ),
    focusedBorder: OutlineInputBorder(
      borderRadius: BorderRadius.circular(Radii.md),
      borderSide: BorderSide(color: t.accent),
    ),
    disabledBorder: OutlineInputBorder(
      borderRadius: BorderRadius.circular(Radii.md),
      borderSide: BorderSide(color: t.hair),
    ),
  );
}
