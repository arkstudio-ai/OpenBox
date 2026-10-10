import 'package:bossip_mobile/features/memory/models/memory_models.dart';
import 'package:bossip_mobile/features/memory/widgets/memory_list.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:intl/date_symbol_data_local.dart';

import '../voice/voice_fakes.dart';

MemoryRecord _memory(
  String id,
  String summary,
  DateTime updated, {
  String? owner,
  String? factKey,
  DateTime? expiresAt,
}) => MemoryRecord(
  id: id,
  summary: summary,
  status: 'ACTIVE',
  revision: 1,
  updatedAt: updated,
  owner: owner,
  factKey: factKey,
  expiresAt: expiresAt,
);

Future<void> _list(
  WidgetTester tester,
  List<MemoryRecord> memories, {
  required bool timeline,
}) async {
  await tester.pumpWidget(
    ProviderScope(
      overrides: [await zhI18n(tester)],
      child: MaterialApp(
        theme: testTheme(),
        home: Scaffold(
          body: SingleChildScrollView(
            child: MemoryList(
              memories: memories,
              timeline: timeline,
              query: '',
              scopeName: (_) => '',
              onOpen: (_) {},
              onEdit: (_) {},
              onForget: (_) {},
            ),
          ),
        ),
      ),
    ),
  );
  await tester.pumpAndSettle();
}

void main() {
  setUpAll(() => initializeDateFormatting('zh_CN'));

  test('periods follow the local day', () {
    final now = DateTime(2026, 10, 8, 15);
    expect(memoryPeriod(DateTime(2026, 10, 8, 0, 5), now: now), 'today');
    expect(memoryPeriod(DateTime(2026, 10, 7, 23), now: now), 'yesterday');
    expect(memoryPeriod(DateTime(2026, 10, 2, 9), now: now), 'week');
    expect(memoryPeriod(DateTime(2026, 9, 30), now: now), 'earlier');
    expect(memoryPeriod(null, now: now), 'earlier');
  });

  testWidgets(
    'browsing reads as a timeline and says how each memory came to be',
    (tester) async {
      final now = DateTime.now();
      final today = DateTime(now.year, now.month, now.day);
      await _list(tester, [
        _memory(
          'a',
          '用户嫌回答太长，希望先说结论',
          now,
          owner: 'SYSTEM_VERIFIED',
          factKey: 'personal.style.length',
        ),
        _memory(
          'b',
          '用户这周五要加班',
          now,
          owner: 'SYSTEM_VERIFIED',
          // Lasts to the end of its last day: expires at the next midnight.
          expiresAt: DateTime(2026, 10, 10),
        ),
        _memory(
          'c',
          '用户周末一般会去游泳',
          today.subtract(const Duration(hours: 1)),
          owner: 'USER_CONFIRMED',
        ),
        _memory(
          'd',
          '用户对花生过敏',
          today.subtract(const Duration(days: 40)),
        ),
      ], timeline: true);
      expect(find.text('今天'), findsOneWidget);
      expect(find.text('昨天'), findsOneWidget);
      expect(find.text('更早'), findsOneWidget);
      expect(find.textContaining('从对话里学到 · 相处方式'), findsOneWidget);
      expect(find.textContaining('到 10月9日'), findsOneWidget);
      expect(find.textContaining('你确认过的'), findsOneWidget);
    },
  );

  testWidgets('a search stays one list', (tester) async {
    await _list(tester, [
      _memory('a', '用户爱吃辣', DateTime.now()),
    ], timeline: false);
    expect(find.text('今天'), findsNothing);
    expect(find.text('用户爱吃辣'), findsOneWidget);
  });
}
