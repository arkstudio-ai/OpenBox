import 'package:bossip_mobile/features/light/production/dish_import_sheet.dart';
import 'package:bossip_mobile/features/light/production/production_api.dart';
import 'package:bossip_mobile/shared/appearance/theme_builder.dart';
import 'package:bossip_mobile/shared/appearance/tokens.dart';
import 'package:bossip_mobile/shared/i18n/i18n.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:integration_test/integration_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

void main() {
  IntegrationTestWidgetsFlutterBinding.ensureInitialized();

  testWidgets(
    'review dish and price pairs from text and photos before saving',
    (tester) async {
      SharedPreferences.setMockInitialValues({'bossip:lang': 'zh-CN'});
      final prefs = await SharedPreferences.getInstance();
      final bundle = await I18nBundle.load();
      var saved = const [ProductionDish('红烧肉', '¥32')];
      final requested = <DishImageSource>[];

      await tester.pumpWidget(
        ProviderScope(
          overrides: [
            i18nProvider.overrideWith(() => I18nController(bundle, prefs)),
          ],
          child: MaterialApp(
            theme: buildBossipTheme(BossipThemeName.default_, Brightness.light),
            home: Scaffold(
              body: Builder(
                builder: (context) => Center(
                  child: TextButton(
                    child: const Text('导入菜单'),
                    onPressed: () async {
                      final result =
                          await showModalBottomSheet<List<ProductionDish>>(
                            context: context,
                            isScrollControlled: true,
                            showDragHandle: true,
                            builder: (_) => DishImportSheet(
                              existing: saved,
                              hasMaterials: true,
                              recognize: (source) async {
                                requested.add(source);
                                return const [ProductionDish('柠檬茶', '¥12/杯')];
                              },
                            ),
                          );
                      if (result != null) saved = result;
                    },
                  ),
                ),
              ),
            ),
          ),
        ),
      );

      await tester.tap(find.text('导入菜单'));
      await tester.pumpAndSettle();
      await tester.enterText(
        find.byKey(const Key('dish-import-text')),
        '红烧肉：¥38/份\nCơm gà 45.000 ₫\n季节时蔬',
      );
      FocusManager.instance.primaryFocus?.unfocus();
      await tester.pumpAndSettle();
      await tester.tap(find.text('识别并核对'));
      await tester.pumpAndSettle();
      expect(find.text('¥38/份'), findsOneWidget);
      expect(find.text('45.000 ₫'), findsOneWidget);
      expect(saved.single.price, '¥32');
      expect(
        tester
            .widget<FilledButton>(find.widgetWithText(FilledButton, '确认导入'))
            .onPressed,
        isNull,
      );
      await tester.ensureVisible(find.text('确认价格留空'));
      await tester.tap(find.text('确认价格留空'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('确认导入'));
      await tester.pumpAndSettle();
      expect(saved.map((dish) => (dish.name, dish.price)), [
        ('红烧肉', '¥32'),
        ('Cơm gà', '45.000 ₫'),
        ('季节时蔬', ''),
      ]);

      await tester.tap(find.text('导入菜单'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('识别图片'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('识别已选素材'));
      await tester.pumpAndSettle();
      expect(find.text('柠檬茶'), findsOneWidget);
      expect(find.text('¥12/杯'), findsOneWidget);
      expect(requested, [DishImageSource.materials]);
      expect(saved.length, 3);
      await tester.tap(find.text('确认导入'));
      await tester.pumpAndSettle();
      expect(saved.last.price, '¥12/杯');
    },
  );
}
