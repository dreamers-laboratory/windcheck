import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:integration_test/integration_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:windcheck/main.dart' as entry;
import 'package:windcheck/state.dart';

void main() {
  final binding = IntegrationTestWidgetsFlutterBinding.ensureInitialized();
  testWidgets('live magnetic runway labels on iOS', (tester) async {
    // This target is only for disposable Windcheck QA simulators.
    await (await SharedPreferences.getInstance()).clear();
    await entry.main();
    await tester.pumpAndSettle();
    await tester.enterText(find.byKey(const Key('airport')), 'KSMO');
    await tester.testTextInput.receiveAction(TextInputAction.search);
    final end = DateTime.now().add(const Duration(minutes: 2));
    while (app.info == null && DateTime.now().isBefore(end)) {
      await tester.pump(const Duration(milliseconds: 250));
    }
    expect(app.info?['airport'], 'KSMO', reason: app.error);
    final runway = app.runways.singleWhere((r) => r['ident'] == '21');
    expect(runway['heading_magnetic_deg'], 212);
    expect(runway['heading_deg'], 225);

    await tester.tap(find.byKey(const Key('edit-input')));
    await tester.pumpAndSettle();
    final picker = find.byType(DropdownButton<String>);
    await tester.ensureVisible(picker);
    await tester.tap(picker);
    await tester.pumpAndSettle();
    expect(find.text('21 (212°M)').hitTestable(), findsOneWidget);
    expect(find.textContaining('225°M'), findsNothing);
    await binding.takeScreenshot('01-magnetic-options');
    await tester.tap(find.text('21 (212°M)').hitTestable());
    await tester.pumpAndSettle();
    expect(app.runway, '21');
    await binding.takeScreenshot('02-selected-runway');
    await tester.tap(picker);
    await tester.pumpAndSettle();
    expect(find.text('03 (032°M)').hitTestable(), findsOneWidget);
    await binding.takeScreenshot('03-reciprocal-runway');
    expect(tester.takeException(), isNull);
    binding.reportData!.addAll({'airport': 'KSMO', 'magnetic': 212,
      'trueCalculationBearing': runway['heading_deg'],
      'runtime': 'iOS Simulator; framework-driven input; live API'});
  }, timeout: const Timeout(Duration(minutes: 4)));
}
