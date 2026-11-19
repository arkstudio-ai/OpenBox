import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/models/light.dart';
import '../state/light_providers.dart';
import 'production_api.dart';
import 'production_media.dart';

String productionTemplateLabel(I18nState i18n, String template) =>
    i18n.t('light:production.template.$template.title');

class ProductionTag extends StatelessWidget {
  const ProductionTag({super.key, required this.text});

  final String text;

  @override
  Widget build(BuildContext context) => DecoratedBox(
    decoration: BoxDecoration(
      color: context.tokens.hairSoft,
      borderRadius: BorderRadius.circular(24),
    ),
    child: Padding(
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 8),
      child: Text(
        text,
        maxLines: 2,
        overflow: TextOverflow.ellipsis,
        style: TextStyle(color: context.tokens.ink, fontSize: 13),
      ),
    ),
  );
}

class ProductionCopyField extends StatelessWidget {
  const ProductionCopyField({
    super.key,
    required this.controller,
    required this.enabled,
    required this.hint,
    required this.onChanged,
    this.minLines = 5,
  });

  final TextEditingController controller;
  final bool enabled;
  final String hint;
  final ValueChanged<String> onChanged;
  final int minLines;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final border = OutlineInputBorder(
      borderSide: BorderSide(color: t.hair),
      borderRadius: BorderRadius.circular(16),
    );
    return TextField(
      key: const Key('production-script'),
      controller: controller,
      enabled: enabled,
      maxLength: 2400,
      minLines: minLines,
      maxLines: 6,
      onChanged: onChanged,
      decoration: InputDecoration(
        hintText: hint,
        counterText: '',
        filled: true,
        fillColor: t.card,
        border: border,
        enabledBorder: border,
        focusedBorder: border.copyWith(borderSide: BorderSide(color: t.a700)),
      ),
    );
  }
}

class ProductionHeading extends StatelessWidget {
  const ProductionHeading(this.title, {super.key, this.description});

  final String title;
  final String? description;

  @override
  Widget build(BuildContext context) => Column(
    crossAxisAlignment: CrossAxisAlignment.start,
    children: [
      Text(
        title,
        style: TextStyle(
          color: context.tokens.ink,
          fontSize: 28,
          fontWeight: FontWeight.w800,
          height: 1.18,
        ),
      ),
      if (description case final text?) ...[
        const SizedBox(height: 6),
        Text(
          text,
          style: TextStyle(
            color: context.tokens.n800,
            fontSize: 15,
            height: 1.4,
          ),
        ),
      ],
    ],
  );
}

class ProductionPrimary extends StatelessWidget {
  const ProductionPrimary({
    super.key,
    required this.label,
    required this.onPressed,
  });

  final String label;
  final VoidCallback? onPressed;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    return FilledButton(
      onPressed: onPressed,
      style: FilledButton.styleFrom(
        backgroundColor: t.a700,
        foregroundColor: t.bg,
        disabledBackgroundColor: t.hairSoft,
        disabledForegroundColor: t.n700,
        minimumSize: const Size.fromHeight(50),
        textStyle: const TextStyle(fontSize: 16, fontWeight: FontWeight.w600),
      ),
      child: Text(label, textAlign: TextAlign.center),
    );
  }
}

class ProductionTemplates extends ConsumerWidget {
  const ProductionTemplates({
    super.key,
    required this.task,
    required this.selected,
    required this.onChoose,
  });

  final LightProduction task;
  final String selected;
  final ValueChanged<String> onChoose;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final i18n = ref.watch(i18nProvider);
    final t = context.tokens;
    final entry = ref.watch(lightEntryProvider(task.kind.id));
    final samples = <String, LightSample>{
      for (final subtype in entry?.subtypes ?? const <LightSubtype>[])
        for (final s in subtype.samples) s.id: s,
    };
    return Column(
      children: [
        for (final template in task.kind.templates)
          Padding(
            padding: const EdgeInsets.only(bottom: 12),
            child: Semantics(
              selected: template == selected,
              button: true,
              child: Material(
                color: template == selected ? t.hairSoft : t.card,
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(18),
                  side: BorderSide(
                    color: template == selected ? t.a700 : t.hair,
                    width: template == selected ? 1.6 : 1,
                  ),
                ),
                child: InkWell(
                  key: Key('production-template-$template'),
                  borderRadius: BorderRadius.circular(18),
                  onTap: () => onChoose(template),
                  child: Padding(
                    padding: const EdgeInsets.all(12),
                    child: Column(
                      children: [
                        Row(
                          children: [
                            if (task.kind != ProductionKind.footage) ...[
                              _TemplatePreview(
                                template: template,
                                preview:
                                    samples[switch (template) {
                                          'warm_red' => 'menu_red',
                                          'monochrome' => 'menu_mono',
                                          'wok' => 'dish_closeup',
                                          _ => '',
                                        }]
                                        ?.thumbUrl,
                                caseLabel: i18n.t('light:production.playCase'),
                              ),
                              const SizedBox(width: 14),
                            ],
                            Expanded(
                              child: Column(
                                crossAxisAlignment: CrossAxisAlignment.start,
                                children: [
                                  Text(
                                    productionTemplateLabel(i18n, template),
                                    style: TextStyle(
                                      color: t.ink,
                                      fontSize: 17,
                                      fontWeight: FontWeight.w700,
                                    ),
                                  ),
                                  const SizedBox(height: 5),
                                  Text(
                                    i18n.t(
                                      'light:production.template.$template.body',
                                    ),
                                    style: TextStyle(
                                      color: t.n800,
                                      fontSize: 14,
                                      height: 1.45,
                                    ),
                                  ),
                                ],
                              ),
                            ),
                            if (template == selected) ...[
                              const SizedBox(width: 8),
                              Icon(Icons.check_circle, size: 22, color: t.a700),
                            ],
                          ],
                        ),
                        if (task.kind == ProductionKind.footage &&
                            task.materials.isNotEmpty) ...[
                          const SizedBox(height: 12),
                          ProductionMediaStrip(
                            media: task.materials,
                            height: 77,
                          ),
                        ],
                      ],
                    ),
                  ),
                ),
              ),
            ),
          ),
      ],
    );
  }
}

class _TemplatePreview extends StatelessWidget {
  const _TemplatePreview({
    required this.template,
    required this.preview,
    required this.caseLabel,
  });

  final String template;
  final String? preview;
  final String caseLabel;

  @override
  Widget build(BuildContext context) {
    final t = context.tokens;
    final recipe = const [
      'soda_story',
      'twist',
      'wan_story',
      'silk_road',
    ].contains(template);
    final asset = switch (template) {
      'soda_story' => sodaStoryPoster,
      'silk_road' => silkRoadPoster,
      'wan_story' => wanStoryPoster,
      'twist' => crawfishCasePoster,
      'wok' => 'assets/first_video/signature.jpg',
      _ => null,
    };
    final fallback = ColoredBox(
      color: t.surface,
      child: Icon(Icons.restaurant_menu, color: t.ink),
    );
    return ClipRRect(
      borderRadius: BorderRadius.circular(12),
      child: SizedBox(
        width: 88,
        height: 116,
        child: Stack(
          fit: StackFit.expand,
          children: [
            if (asset != null)
              Image.asset(asset, fit: BoxFit.cover)
            else if (preview case final url?)
              Image.network(
                url,
                fit: BoxFit.cover,
                errorBuilder: (_, _, _) => fallback,
              )
            else
              fallback,
            if (recipe)
              Center(
                child: IconButton.filled(
                  key: Key(switch (template) {
                    'soda_story' => 'production-soda-preview',
                    'silk_road' => 'production-silk-road-preview',
                    'wan_story' => 'production-wan-preview',
                    _ => 'production-case-preview',
                  }),
                  tooltip: caseLabel,
                  style: IconButton.styleFrom(
                    backgroundColor: Colors.white,
                    foregroundColor: const Color(0xff3e3929),
                  ),
                  onPressed: () =>
                      showProductionVideo(context, switch (template) {
                        'soda_story' => sodaStoryVideo,
                        'silk_road' => silkRoadVideo,
                        'wan_story' => wanStoryVideo,
                        _ => crawfishCaseVideo,
                      }, bundled: true),
                  icon: const Icon(Icons.play_arrow),
                ),
              ),
          ],
        ),
      ),
    );
  }
}

class ProductionMediaStrip extends StatelessWidget {
  const ProductionMediaStrip({
    super.key,
    required this.media,
    this.height = 100,
  });

  final List<ProductionMedia> media;
  final double height;

  @override
  Widget build(BuildContext context) {
    final visible = media.take(3).toList();
    if (visible.isEmpty) return const SizedBox.shrink();
    return SizedBox(
      height: height,
      child: Row(
        children: [
          for (final (index, item) in visible.indexed) ...[
            if (index > 0) const SizedBox(width: 8),
            Expanded(child: ProductionThumbnail(media: item)),
          ],
          if (visible.length == 1) const Spacer(),
        ],
      ),
    );
  }
}

class ProductionDishes extends ConsumerWidget {
  const ProductionDishes({
    super.key,
    required this.dishes,
    required this.onChange,
  });

  final List<ProductionDish> dishes;
  final ValueChanged<List<ProductionDish>> onChange;

  Future<void> _edit(BuildContext context, int? index) async {
    final edited = await showDialog<ProductionDish>(
      context: context,
      builder: (_) => _DishDialog(dish: index == null ? null : dishes[index]),
    );
    if (edited == null || !context.mounted) return;
    final next = [...dishes];
    if (index == null) {
      next.add(edited);
    } else if (edited.name.isEmpty) {
      next.removeAt(index);
    } else {
      next[index] = edited;
    }
    onChange(next);
  }

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final i18n = ref.watch(i18nProvider);
    final t = context.tokens;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        for (final (index, dish) in dishes.indexed) ...[
          Material(
            color: t.card,
            borderRadius: BorderRadius.circular(14),
            child: ListTile(
              onTap: () => _edit(context, index),
              title: Text(dish.name),
              trailing: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  if (dish.price.isNotEmpty) ...[
                    Text(dish.price),
                    const SizedBox(width: 12),
                  ],
                  Icon(Icons.edit_outlined, size: 18, color: t.n800),
                ],
              ),
            ),
          ),
          const SizedBox(height: 6),
        ],
        OutlinedButton.icon(
          key: const Key('production-dish-add'),
          onPressed: dishes.length >= 30 ? null : () => _edit(context, null),
          icon: const Icon(Icons.add_circle_outline),
          label: Text(i18n.t('light:production.addDish')),
        ),
      ],
    );
  }
}

class _DishDialog extends ConsumerStatefulWidget {
  const _DishDialog({required this.dish});

  final ProductionDish? dish;

  @override
  ConsumerState<_DishDialog> createState() => _DishDialogState();
}

class _DishDialogState extends ConsumerState<_DishDialog> {
  late final _name = TextEditingController(text: widget.dish?.name);
  late final _price = TextEditingController(text: widget.dish?.price);

  @override
  void dispose() {
    _name.dispose();
    _price.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final i18n = ref.watch(i18nProvider);
    return AlertDialog(
      title: Text(
        i18n.t(
          widget.dish == null
              ? 'light:production.addDish'
              : 'light:production.editDish',
        ),
      ),
      scrollable: true,
      content: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          TextField(
            key: const Key('production-dish-name'),
            controller: _name,
            autofocus: true,
            maxLength: 100,
            textInputAction: TextInputAction.next,
            onChanged: (_) => setState(() {}),
            decoration: InputDecoration(
              labelText: i18n.t('light:production.dishName'),
              counterText: '',
            ),
          ),
          TextField(
            key: const Key('production-dish-price'),
            controller: _price,
            maxLength: 32,
            decoration: InputDecoration(
              labelText: i18n.t('light:production.dishPrice'),
              counterText: '',
            ),
          ),
        ],
      ),
      actions: [
        if (widget.dish != null)
          TextButton(
            onPressed: () =>
                Navigator.pop(context, const ProductionDish('', '')),
            child: Text(i18n.t('light:production.remove')),
          ),
        TextButton(
          onPressed: () => Navigator.pop(context),
          child: Text(i18n.t('common:action.cancel')),
        ),
        FilledButton(
          key: const Key('production-dish-save'),
          onPressed: _name.text.trim().isEmpty
              ? null
              : () => Navigator.pop(
                  context,
                  ProductionDish(_name.text.trim(), _price.text.trim()),
                ),
          child: Text(i18n.t('common:action.save')),
        ),
      ],
    );
  }
}
