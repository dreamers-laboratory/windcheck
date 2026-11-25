import 'package:flutter_test/flutter_test.dart';
import 'package:windcheck/ui/kit.dart';

void main() {
  test('runway label uses magnetic metadata, not the true calculation bearing', () {
    expect(runwayLabel({'ident': '21', 'heading_deg': 225,
      'heading_magnetic_deg': 212}), '21 (212°M)');
    expect(runwayLabel({'ident': '03', 'heading_magnetic_deg': 32}), '03 (032°M)');
  });
  test('old cache and missing magnetic metadata show only the runway ID', () {
    expect(runwayLabel({'ident': '21', 'heading_deg': 225}), '21');
    expect(runwayLabel({'ident': '21', 'heading_magnetic_deg': null}), '21');
    expect(runwayLabel({'ident': '21', 'heading_magnetic_deg': double.nan}), '21');
  });
}
