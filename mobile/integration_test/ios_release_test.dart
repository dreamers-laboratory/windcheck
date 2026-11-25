import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:integration_test/integration_test.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:windcheck/main.dart' as entry;
import 'package:windcheck/state.dart';
import 'package:windcheck/ui/charts.dart';

void main() {
  final binding = IntegrationTestWidgetsFlutterBinding.ensureInitialized();

  testWidgets('live iOS release smoke and screenshots', (tester) async {
    // Run only on a disposable QA simulator: this clears this app's settings.
    final prefs = await SharedPreferences.getInstance();
    await prefs.clear();
    await entry.main();
    await tester.pumpAndSettle();
    Future<void> capture(String name) async {
      await tester.pumpAndSettle();
      // Let the real iOS compositor present the frame before UIKit captures it.
      await tester.runAsync(
        () => Future<void>.delayed(const Duration(milliseconds: 300)),
      );
      await binding.takeScreenshot(name);
    }

    await capture('00-inputs');

    Future<void> waitUntil(bool Function() ready) async {
      final end = DateTime.now().add(const Duration(minutes: 3));
      while (!ready() && DateTime.now().isBefore(end)) {
        await tester.pump(const Duration(milliseconds: 250));
      }
      expect(
        ready(),
        isTrue,
        reason: 'Live API did not settle: ${app.error}; ${app.forecastError}',
      );
      await tester.pumpAndSettle();
      expect(tester.takeException(), isNull);
    }

    Future<void> tapKey(String key) async {
      final scroll = tester.state<ScrollableState>(
        find
            .descendant(
              of: find.byType(CustomScrollView),
              matching: find.byType(Scrollable),
            )
            .first,
      );
      scroll.position.jumpTo(0);
      await tester.pumpAndSettle();
      final finder = find.byKey(Key(key));
      await tester.ensureVisible(finder);
      await tester.pumpAndSettle();
      await tester.tap(finder);
      await tester.pumpAndSettle();
    }

    await tester.tap(find.byKey(const Key('airport')));
    await tester.enterText(find.byKey(const Key('airport')), 'KMYF');
    await tester.pumpAndSettle();
    await capture('01-airport-input');
    await tester.testTextInput.receiveAction(TextInputAction.search);
    await waitUntil(() => !app.loading && !app.forecastLoading);
    expect(app.info?['airport'], 'KMYF');
    expect(app.stats, isNotNull);
    expect(app.forecast?['airport'], 'KMYF');
    final models = (app.forecast!['models'] as List)
        .cast<Map<String, dynamic>>();
    expect(models.where((m) => m['status'] == 'ok'), isNotEmpty);
    await capture('02-forecast-table');

    final initialTarget = app.target ?? app.forecast?['target_pt'];
    await tester.tap(find.text('+1H'));
    await tester.pumpAndSettle();
    expect(app.target, isNot(initialTarget));
    await tester.tap(find.text('−1H'));
    await tester.pumpAndSettle();

    await Scrollable.ensureVisible(
      tester.element(find.byType(LineChart).first),
      alignment: 0,
    );
    await tester.pumpAndSettle();
    await capture('03-forecast-charts');

    await tapKey('seg-History');
    expect(app.stats?['rows'], isNotEmpty);
    await capture('04-history');

    // Every observation is folded at the foot of History, below the fold.
    final obs = find.byKey(const Key('obs-toggle'));
    await tester.scrollUntilVisible(obs, 400,
        scrollable: find.descendant(of: find.byType(CustomScrollView), matching: find.byType(Scrollable)).first);
    await tester.tap(obs);
    await tester.pumpAndSettle();
    await waitUntil(() => !app.rawLoading && app.raw != null);
    expect(app.raw?['rows'], isNotEmpty);
    final rawRows = (app.raw!['rows'] as List).length;
    await capture('05-all-observations');

    await tapKey('settings');
    await capture('06-settings');
    await tapKey('tz-button');
    await tester.enterText(find.byKey(const Key('tz-search')), 'New York');
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('tz-America/New_York')), findsOneWidget);
    await capture('07-time-zone');
    await tester.tap(find.byKey(const Key('tz-America/New_York')));
    await waitUntil(() => !app.loading && !app.forecastLoading);
    expect(app.tz, 'America/New_York');
    await app.setTz('America/Los_Angeles');
    await waitUntil(() => !app.loading && !app.forecastLoading);

    await tapKey('seg-Forecast');
    await tester.tap(find.byTooltip('About'));
    await tester.pumpAndSettle();
    expect(find.textContaining(entry.appVersion), findsOneWidget);
    await capture('08-about');

    binding.reportData!.addAll({
      'runtime': 'iOS Simulator; native Flutter engine; framework-driven input',
      'api': 'https://windcheck.net',
      'airport': app.info?['airport'],
      'version': entry.appVersion,
      'models': [
        for (final m in models) {'id': m['id'], 'status': m['status']},
      ],
      'historicalObservations': app.stats?['total_obs'],
      'rawRows': rawRows,
      'timeZoneSwitch': true,
    });
  }, timeout: const Timeout(Duration(minutes: 10)));
}
