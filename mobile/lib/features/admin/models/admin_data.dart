import 'package:intl/intl.dart';

import '../../../shared/models/json.dart';

/// Read-only wire records preserve server Decimal strings and unknown states.
/// No client-side aggregation of cross-workspace money or credits.
class AdminRecord {
  const AdminRecord(this.data);
  final Map<String, dynamic> data;
  String string(String key, [String fallback = '']) {
    final value = data[key];
    return value is String || value is num ? value.toString() : fallback;
  }

  int integer(String key, [int fallback = 0]) => asInt(data[key]) ?? fallback;
  bool flag(String key) => data[key] == true;
  AdminRecord record(String key) => AdminRecord(asMap(data[key]));
  List<AdminRecord> records(String key) => asList(
    data[key],
  ).whereType<Map<String, dynamic>>().map(AdminRecord.new).toList();
  List<String> strings(String key) =>
      asList(data[key]).whereType<String>().toList();
}

class AdminPage {
  const AdminPage({
    this.items = const [],
    this.total = 0,
    this.offset = 0,
    this.limit = 20,
  });
  factory AdminPage.fromJson(Map<String, dynamic> data) {
    if (data['items'] is! List) {
      throw const FormatException('Expected an admin page');
    }
    final row = AdminRecord(data);
    final items = row.records('items');
    return AdminPage(
      items: items,
      total: row.integer('total', items.length),
      offset: row.integer('offset'),
      limit: row.integer('limit', 20),
    );
  }
  final List<AdminRecord> items;
  final int total;
  final int offset;
  final int limit;
}

class FleetData {
  const FleetData(this.pool, this.desktops, this.alerts, this.snapshot);
  final AdminRecord pool;
  final AdminPage desktops;
  final AdminPage alerts;
  final AdminRecord snapshot;
}

typedef AdminScope = ({String userId, String? workspaceId});

String adminDate(String value, String language) {
  final date = DateTime.tryParse(value);
  if (date == null) return '—';
  return DateFormat.yMd(
    language.replaceAll('-', '_'),
  ).add_Hm().format(date.toLocal());
}

/// Integer fen are formatted exactly once; no floating-point rounding.
String adminMoney(AdminRecord order) {
  if (order.data['amount_fen'] is! num) return '—';
  final fen = order.integer('amount_fen');
  final magnitude = fen.abs();
  final whole = magnitude ~/ 100;
  final fraction = (magnitude % 100).toString().padLeft(2, '0');
  final sign = fen < 0 ? '-' : '';
  final currency = order.string('currency', 'CNY');
  final symbol = switch (currency) {
    'CNY' => '¥',
    'USD' => r'$',
    'EUR' => '€',
    _ => '$currency ',
  };
  return '$sign$symbol$whole.$fraction';
}

Map<String, dynamic> adminFilters(Map<String, String> values) => {
  for (final entry in values.entries)
    if (entry.value.trim().isNotEmpty && entry.value != 'all')
      entry.key: entry.value.trim(),
};
