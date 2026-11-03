import 'package:bossip_mobile/shared/utils/format.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  test('a Light balance shows at most one decimal, rounded down', () {
    expect(formatCreditsBrief('10.157126000000'), '10.1');
    expect(formatCreditsBrief('14.000000000000'), '14');
    expect(formatCreditsBrief('0.045'), '0');
    expect(formatCreditsBrief('3.75'), '3.7');
    expect(formatCreditsBrief(null), '—');
    expect(formatCreditsBrief('abc'), '—');
  });
}
