import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:intl/intl.dart';

import '../../../shared/appearance/tokens.dart';
import '../../../shared/appearance/type_scale.dart';
import '../../../shared/i18n/i18n.dart';
import 'admin_layout.dart';

class AdminFilterField {
  const AdminFilterField(
    this.key,
    this.label, {
    this.options,
    this.date = false,
  });
  final String key, label;
  final Map<String, String>? options;
  final bool date;
}

Future<Map<String, String>?> showAdminFilters(
  BuildContext context, {
  required Map<String, String> values,
  required List<AdminFilterField> fields,
}) => showModalBottomSheet<Map<String, String>>(
  context: context,
  isScrollControlled: true,
  useSafeArea: true,
  backgroundColor: context.tokens.card,
  builder: (_) => _Filters(values: values, fields: fields),
);

class _Filters extends ConsumerStatefulWidget {
  const _Filters({required this.values, required this.fields});
  final Map<String, String> values;
  final List<AdminFilterField> fields;
  @override
  ConsumerState<_Filters> createState() => _FiltersState();
}

class _FiltersState extends ConsumerState<_Filters> {
  late final _values = Map<String, String>.from(widget.values);
  late final _controllers = {
    for (final f in widget.fields)
      f.key: TextEditingController(text: _values[f.key] ?? ''),
  };
  @override
  void dispose() {
    for (final controller in _controllers.values) {
      controller.dispose();
    }
    super.dispose();
  }

  bool get _invalid =>
      (_values['from']?.isNotEmpty ?? false) &&
      (_values['to']?.isNotEmpty ?? false) &&
      _values['from']!.compareTo(_values['to']!) > 0;

  Future<void> _date(AdminFilterField field) async {
    final picked = await showDatePicker(
      context: context,
      useRootNavigator: false,
      initialDate:
          DateTime.tryParse(_values[field.key] ?? '') ?? DateTime.now(),
      firstDate: DateTime(2000),
      lastDate: DateTime(2100),
    );
    if (picked != null && mounted) {
      setState(() {
        _values[field.key] = DateFormat('yyyy-MM-dd').format(picked);
        _controllers[field.key]!.text = _values[field.key]!;
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    final i = ref.watch(i18nProvider);
    return AdminSheet(
      content: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            children: [
              Expanded(
                child: Text(
                  i.t('admin-billing:filters.label'),
                  style: const TextStyle(
                    fontSize: FontSizes.xl2,
                    fontWeight: FontWeight.w500,
                  ),
                ),
              ),
              IconButton(
                tooltip: i.t('common:action.close'),
                icon: const Icon(Icons.close),
                onPressed: () => Navigator.pop(context),
              ),
            ],
          ),
          for (final field in widget.fields)
            Padding(
              padding: const EdgeInsets.only(top: 16),
              child: field.options != null
                  ? DropdownButtonFormField<String>(
                      isExpanded: true,
                      initialValue:
                          field.options!.containsKey(_values[field.key])
                          ? _values[field.key]
                          : 'all',
                      decoration: InputDecoration(labelText: field.label),
                      items: [
                        for (final entry in field.options!.entries)
                          DropdownMenuItem(
                            value: entry.key,
                            child: Text(
                              entry.value,
                              overflow: TextOverflow.ellipsis,
                            ),
                          ),
                      ],
                      onChanged: (value) =>
                          setState(() => _values[field.key] = value ?? 'all'),
                    )
                  : TextField(
                      controller: _controllers[field.key],
                      readOnly: field.date,
                      onTap: field.date ? () => _date(field) : null,
                      onChanged: (value) =>
                          setState(() => _values[field.key] = value),
                      decoration: InputDecoration(
                        labelText: field.label,
                        suffixIcon: field.date
                            ? IconButton(
                                tooltip: i.t('admin-billing:list.reset'),
                                onPressed: () => setState(() {
                                  _values[field.key] = '';
                                  _controllers[field.key]!.clear();
                                }),
                                icon: const Icon(Icons.clear),
                              )
                            : null,
                      ),
                    ),
            ),
          if (_invalid)
            Padding(
              padding: const EdgeInsets.only(top: 12),
              child: Text(
                i.t('admin-billing:filters.invalidRange'),
                style: TextStyle(color: context.tokens.danger),
              ),
            ),
        ],
      ),
      footer: AdminActionBar(
        secondary: OutlinedButton(
          onPressed: () => Navigator.pop(context, <String, String>{}),
          child: Text(i.t('admin-billing:list.reset')),
        ),
        primary: FilledButton(
          key: const ValueKey('apply-admin-filters'),
          onPressed: _invalid ? null : () => Navigator.pop(context, _values),
          child: Text(i.t('admin-billing:list.search')),
        ),
      ),
    );
  }
}

class AdminSearchBar extends ConsumerWidget {
  const AdminSearchBar({
    super.key,
    required this.controller,
    required this.hint,
    required this.onSearch,
    this.onFilter,
    this.filterCount = 0,
    this.onRefresh,
    this.disabled = false,
  });
  final TextEditingController controller;
  final String hint;
  final ValueChanged<String> onSearch;
  final VoidCallback? onFilter;
  final VoidCallback? onRefresh;
  final int filterCount;
  final bool disabled;
  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final i = ref.watch(i18nProvider);
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 8),
      child: Row(
        children: [
          Expanded(
            child: TextField(
              controller: controller,
              enabled: !disabled,
              onSubmitted: onSearch,
              textInputAction: TextInputAction.search,
              decoration: InputDecoration(
                hintText: hint,
                isDense: true,
                filled: true,
                fillColor: context.tokens.hairSoft,
                contentPadding: const EdgeInsets.symmetric(
                  horizontal: 12,
                  vertical: 14,
                ),
                border: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(Radii.md),
                  borderSide: BorderSide.none,
                ),
                enabledBorder: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(Radii.md),
                  borderSide: BorderSide.none,
                ),
                focusedBorder: OutlineInputBorder(
                  borderRadius: BorderRadius.circular(Radii.md),
                  borderSide: BorderSide(color: context.tokens.n400),
                ),
                suffixIcon: IconButton(
                  tooltip: i.t('admin-billing:list.search'),
                  onPressed: disabled
                      ? null
                      : () {
                          FocusScope.of(context).unfocus();
                          onSearch(controller.text);
                        },
                  icon: const Icon(Icons.search, size: 20),
                ),
              ),
            ),
          ),
          if (onFilter != null)
            IconButton(
              tooltip: i.t('admin-billing:filters.label'),
              onPressed: disabled ? null : onFilter,
              icon: Badge(
                isLabelVisible: filterCount > 0,
                backgroundColor: context.tokens.ink,
                textColor: context.tokens.bg,
                label: Text('$filterCount'),
                child: const Icon(Icons.tune, size: 20),
              ),
            ),
          if (onRefresh != null)
            IconButton(
              tooltip: i.t('admin:mobile.refresh'),
              onPressed: disabled ? null : onRefresh,
              icon: const Icon(Icons.refresh, size: 20),
            ),
        ],
      ),
    );
  }
}
