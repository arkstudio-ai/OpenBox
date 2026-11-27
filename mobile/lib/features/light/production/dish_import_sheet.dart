import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/i18n/i18n.dart';
import '../../../shared/utils/error_text.dart';
import 'production_api.dart';

enum DishImageSource { materials, album, library }

final _namedPrice = RegExp(
  r'^(.+?)\s*[:：,，\t]\s*((?:[¥￥$₫€]|VND|USD|CNY)?\s*\d[\d.,]*(?:\s*(?:元|块|₫|đ|VND|USD|CNY))?(?:\s*[/／]\s*[^\s]+)?)$',
  caseSensitive: false,
);
final _spacedPrice = RegExp(
  r'^(.+?)(?:\s+|(?=[¥￥$₫€]))((?:[¥￥$₫€]\s*)?\d[\d.,]*(?:\s*(?:元|块|₫|đ|VND|USD|CNY))?(?:\s*[/／]\s*[^\s]+)?)$',
  caseSensitive: false,
);
final _markedPrice = RegExp(
  r'^(.+?)(\d[\d.,]*\s*(?:元|块|₫|đ)(?:[/／]\s*[^\s]+)?)$',
);

List<ProductionDish> parseDishList(String input) => [
  for (final entry
      in input.split(RegExp(r'[\r\n;；]+')).map((text) => text.trim()))
    if (entry.isNotEmpty) _parseDish(entry),
];

ProductionDish _parseDish(String line) {
  final match =
      _namedPrice.firstMatch(line) ??
      _spacedPrice.firstMatch(line) ??
      _markedPrice.firstMatch(line);
  return match == null
      ? ProductionDish(line, '')
      : ProductionDish(match.group(1)!.trim(), match.group(2)!.trim());
}

class _DishRow {
  _DishRow(ProductionDish dish) : name = dish.name, price = dish.price;

  String name;
  String price;
}

class DishImportSheet extends ConsumerStatefulWidget {
  const DishImportSheet({
    super.key,
    required this.existing,
    required this.recognize,
    required this.hasMaterials,
    this.showImages = false,
  });

  final List<ProductionDish> existing;
  final Future<List<ProductionDish>?> Function(DishImageSource) recognize;
  final bool hasMaterials;
  final bool showImages;

  @override
  ConsumerState<DishImportSheet> createState() => _DishImportSheetState();
}

class _DishImportSheetState extends ConsumerState<DishImportSheet> {
  final _text = TextEditingController();
  late bool _images = widget.showImages;
  List<_DishRow>? _rows;
  bool _busy = false;
  bool _allowBlank = false;
  bool _replace = false;
  Object? _error;

  @override
  void dispose() {
    _text.dispose();
    super.dispose();
  }

  String _key(String name) => name.trim().toLowerCase();

  List<ProductionDish> _merge(List<_DishRow> rows) {
    final dishes = [...widget.existing];
    for (final row in rows) {
      final dish = ProductionDish(row.name.trim(), row.price.trim());
      final duplicate = dishes.indexWhere(
        (item) => _key(item.name) == _key(dish.name),
      );
      if (duplicate < 0) {
        dishes.add(dish);
      } else if (_replace) {
        dishes[duplicate] = dish;
      }
    }
    return dishes;
  }

  void _preview(List<ProductionDish> dishes) {
    setState(() {
      _rows = dishes.map(_DishRow.new).toList();
      _allowBlank = false;
      _replace = false;
      _error = null;
    });
  }

  Future<void> _recognize(DishImageSource source) async {
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final dishes = await widget.recognize(source);
      if (mounted && dishes != null) _preview(dishes);
    } catch (error) {
      if (mounted) setState(() => _error = error);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final i18n = ref.watch(i18nProvider);
    final t = context.tokens;
    String text(String name, {Map<String, Object?>? vars}) =>
        i18n.t('light:production.dishImport.$name', vars: vars);
    final rows = _rows;
    final merged = rows == null ? null : _merge(rows);
    final blank = rows?.any((row) => row.price.trim().isEmpty) ?? false;
    final seen = widget.existing.map((dish) => _key(dish.name)).toSet();
    final duplicates = <_DishRow>{
      if (rows != null)
        for (final row in rows)
          if (!seen.add(_key(row.name))) row,
    };
    final valid =
        rows != null &&
        rows.isNotEmpty &&
        rows.every(
          (row) =>
              row.name.trim().isNotEmpty &&
              row.name.length <= 100 &&
              row.price.length <= 32,
        ) &&
        merged!.length <= 30 &&
        (!blank || _allowBlank);

    return SafeArea(
      child: AnimatedPadding(
        duration: const Duration(milliseconds: 120),
        padding: EdgeInsets.only(
          bottom: MediaQuery.viewInsetsOf(context).bottom,
        ),
        child: FractionallySizedBox(
          heightFactor: .8,
          child: Column(
            children: [
              Padding(
                padding: const EdgeInsets.fromLTRB(16, 0, 8, 8),
                child: Row(
                  children: [
                    Expanded(
                      child: Text(
                        text(rows == null ? 'title' : 'review'),
                        style: Theme.of(context).textTheme.titleLarge,
                      ),
                    ),
                    IconButton(
                      tooltip: i18n.t('common:action.close'),
                      onPressed: () => Navigator.pop(context),
                      icon: const Icon(Icons.close),
                    ),
                  ],
                ),
              ),
              Expanded(
                child: ListView(
                  padding: const EdgeInsets.symmetric(horizontal: 16),
                  children: [
                    if (rows == null) ...[
                      SegmentedButton<bool>(
                        segments: [
                          ButtonSegment(
                            value: false,
                            label: Text(text('text')),
                          ),
                          ButtonSegment(
                            value: true,
                            label: Text(text('photos')),
                          ),
                        ],
                        selected: {_images},
                        onSelectionChanged: _busy
                            ? null
                            : (value) => setState(() => _images = value.first),
                      ),
                      const SizedBox(height: 12),
                      if (!_images) ...[
                        TextField(
                          key: const Key('dish-import-text'),
                          controller: _text,
                          minLines: 4,
                          maxLines: 7,
                          maxLength: 6000,
                          onChanged: (_) => setState(() {}),
                          decoration: InputDecoration(hintText: text('hint')),
                        ),
                        OutlinedButton.icon(
                          onPressed: () async {
                            final clipboard = await Clipboard.getData(
                              Clipboard.kTextPlain,
                            );
                            final value = clipboard?.text;
                            if (!mounted || value == null) return;
                            _text.text = value.length > 6000
                                ? value.substring(0, 6000)
                                : value;
                            setState(() {});
                          },
                          icon: const Icon(Icons.content_paste_outlined),
                          label: Text(text('paste')),
                        ),
                      ] else ...[
                        Text(text('photoHint')),
                        if (widget.hasMaterials)
                          OutlinedButton(
                            onPressed: _busy
                                ? null
                                : () => _recognize(DishImageSource.materials),
                            child: Text(text('materials')),
                          ),
                        OutlinedButton(
                          onPressed: _busy
                              ? null
                              : () => _recognize(DishImageSource.album),
                          child: Text(text('album')),
                        ),
                        OutlinedButton(
                          onPressed: _busy
                              ? null
                              : () => _recognize(DishImageSource.library),
                          child: Text(i18n.t('light:production.library')),
                        ),
                        if (_busy) const LinearProgressIndicator(),
                        if (_error != null)
                          Text(
                            errorText(i18n, _error!),
                            style: TextStyle(color: t.dangerInk),
                          ),
                      ],
                    ] else ...[
                      Text(
                        text(
                          rows.isEmpty ? 'empty' : 'count',
                          vars: {'count': rows.length},
                        ),
                      ),
                      if (duplicates.isNotEmpty)
                        SwitchListTile.adaptive(
                          contentPadding: EdgeInsets.zero,
                          title: Text(text('replace')),
                          subtitle: Text(text('duplicates')),
                          value: _replace,
                          onChanged: (value) =>
                              setState(() => _replace = value),
                        ),
                      for (final row in rows)
                        Padding(
                          key: ObjectKey(row),
                          padding: const EdgeInsets.only(top: 8, bottom: 4),
                          child: Row(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Expanded(
                                flex: 3,
                                child: TextFormField(
                                  initialValue: row.name,
                                  decoration: InputDecoration(
                                    labelText: i18n.t(
                                      'light:production.dishName',
                                    ),
                                    errorText: duplicates.contains(row)
                                        ? text('sameName')
                                        : null,
                                  ),
                                  maxLength: 100,
                                  onChanged: (value) =>
                                      setState(() => row.name = value),
                                ),
                              ),
                              const SizedBox(width: 8),
                              Expanded(
                                flex: 2,
                                child: TextFormField(
                                  initialValue: row.price,
                                  decoration: InputDecoration(
                                    labelText: row.price.trim().isEmpty
                                        ? text('checkPrice')
                                        : text('price'),
                                  ),
                                  maxLength: 32,
                                  onChanged: (value) =>
                                      setState(() => row.price = value),
                                ),
                              ),
                              IconButton(
                                tooltip: i18n.t('common:action.delete'),
                                onPressed: () =>
                                    setState(() => rows.remove(row)),
                                icon: const Icon(Icons.close, size: 18),
                              ),
                            ],
                          ),
                        ),
                      if (blank)
                        CheckboxListTile(
                          contentPadding: EdgeInsets.zero,
                          title: Text(text('allowBlank')),
                          value: _allowBlank,
                          onChanged: (value) =>
                              setState(() => _allowBlank = value ?? false),
                        ),
                      if (merged!.length > 30)
                        Text(
                          text('tooMany'),
                          style: TextStyle(color: t.dangerInk),
                        ),
                    ],
                  ],
                ),
              ),
              Padding(
                padding: const EdgeInsets.fromLTRB(16, 8, 16, 12),
                child: Row(
                  children: [
                    if (rows != null)
                      TextButton(
                        onPressed: () => setState(() => _rows = null),
                        child: Text(text('back')),
                      ),
                    if (rows != null || !_images)
                      Expanded(
                        child: FilledButton(
                          onPressed: rows == null
                              ? (_text.text.trim().isEmpty
                                    ? null
                                    : () => _preview(parseDishList(_text.text)))
                              : (valid
                                    ? () => Navigator.pop(context, merged)
                                    : null),
                          child: Text(text(rows == null ? 'read' : 'apply')),
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
