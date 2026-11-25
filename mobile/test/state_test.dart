// Pins the state races and the saved-settings bug found in review.
import 'dart:async';
import 'dart:io';

import 'package:flutter/services.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:windcheck/main.dart' as entry;
import 'package:windcheck/state.dart';

final fixtures = {
  for (final n in ['info', 'stats', 'raw', 'forecast']) '/api/$n': File('test/fixtures/$n.json').readAsStringSync()
};

/// Serves fixtures; `/api/raw` waits on [hold] when one is set.
Completer<void>? hold;
Completer<void>? tzGate; // stands in for platform-channel latency at startup
Completer<void>? requestGate;
bool failRequests = false;
final forecastZones = <String>[];
http.Client server() => MockClient((r) async {
      if (r.url.path == '/api/forecast') forecastZones.add(r.url.queryParameters['tz']!);
      await requestGate?.future;
      if (failRequests) throw http.ClientException('offline');
      if (r.url.path == '/api/raw' && hold != null) await hold!.future;
      return http.Response(fixtures[r.url.path]!, 200, headers: {'content-type': 'application/json; charset=utf-8'});
    });

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUp(() {
    hold = null;
    tzGate = null;
    requestGate = null;
    failRequests = false;
    forecastZones.clear();
    TestDefaultBinaryMessengerBinding.instance.defaultBinaryMessenger.setMockMethodCallHandler(
        const MethodChannel('flutter_timezone'), (_) async {
      await tzGate?.future;
      return 'America/Los_Angeles';
    });
  });

  testWidgets('saved minimums reach the form and survive a run', (tester) async {
    SharedPreferences.setMockInitialValues({'airport': 'KMYF', 'visMin': 3.0, 'ceilMin': 2500.0, 'xwindMax': 9.0});
    await http.runWithClient(() async {
      tzGate = Completer();
      final started = entry.main(); // the real startup path
      await tester.pump(); // a frame while init is still waiting on the platform
      tzGate!.complete();
      await started;
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('edit-input')));
      await tester.pumpAndSettle();
      final texts = [for (final f in tester.widgetList<TextField>(find.byType(TextField))) f.controller!.text];
      expect(texts, containsAll(['KMYF', '3', '2500', '9']));
      await tester.ensureVisible(find.byKey(const Key('run')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('run')));
      await tester.pumpAndSettle();
      final prefs = await SharedPreferences.getInstance();
      expect(prefs.getDouble('xwindMax'), 9.0);
    }, server);
  });

  test('a run during an in-flight raw load does not leave Every observation stuck', () async {
    SharedPreferences.setMockInitialValues({});
    await http.runWithClient(() async {
      final s = AppState();
      await s.init();
      s.airport = 'KMYF';
      await s.run();
      hold = Completer();
      final first = s.ensureRaw(); // in flight
      expect(s.rawLoading, isTrue);
      s.wantRaw = true;
      final rerun = s.run(); // must reset rawLoading and reload raw itself
      hold!.complete();
      await Future.wait([first, rerun]);
      await pumpEventQueue();
      expect(s.rawLoading, isFalse);
      expect(s.raw, isNotNull);
    }, server);
  });

  test('changing airport clears previous airport data before requests finish', () async {
    SharedPreferences.setMockInitialValues({});
    await http.runWithClient(() async {
      final s = AppState();
      await s.init();
      s.airport = 'KMYF';
      await s.run();
      await pumpEventQueue();
      expect(s.stats, isNotNull);
      expect(s.forecast, isNotNull);
      requestGate = Completer();
      failRequests = true;
      s.airport = 'L78';
      final pending = s.run();
      expect(s.info, isNull);
      expect(s.weatherInfo, isNull);
      expect(s.stats, isNull);
      expect(s.forecast, isNull);
      requestGate!.complete();
      await pending;
      await pumpEventQueue();
      expect(s.stats, isNull);
      expect(s.forecast, isNull);
    }, server);
  });

  test('failed refresh preserves same-airport results with error status', () async {
    SharedPreferences.setMockInitialValues({});
    await http.runWithClient(() async {
      final s = AppState();
      await s.init();
      s.airport = 'KMYF';
      await s.run();
      await pumpEventQueue();
      final stats = s.stats, forecast = s.forecast;
      expect(stats, isNotNull);
      expect(forecast, isNotNull);
      failRequests = true;
      await s.run();
      await pumpEventQueue();
      expect(s.stats, same(stats));
      expect(s.forecast, same(forecast));
      expect(s.error, isNotNull);
      expect(s.forecastError, isNotNull);
    }, server);
  });

  test('a failed time-zone change never relabels old-zone weather', () async {
    SharedPreferences.setMockInitialValues({});
    await http.runWithClient(() async {
      final s = AppState();
      await s.init();
      s.airport = 'KMYF';
      await s.run();
      await pumpEventQueue();
      expect(s.forecast, isNotNull);
      failRequests = true;
      await s.setTz('America/New_York');
      await pumpEventQueue();
      expect(s.stats, isNull);
      expect(s.forecast, isNull);
      expect(s.error, isNotNull);
    }, server);
  });

  test('changing zone on first load restarts forecast in the selected zone', () async {
    SharedPreferences.setMockInitialValues({});
    await http.runWithClient(() async {
      final s = AppState();
      await s.init();
      s.airport = 'KMYF';
      requestGate = Completer();
      final initial = s.run();
      await s.setTz('America/New_York');
      requestGate!.complete();
      await initial;
      await pumpEventQueue();
      expect(forecastZones.last, 'America/New_York');
      expect(s.loading, isFalse);
    }, server);
  });

  test('raw is not fetched with the previous query while a run is loading', () async {
    SharedPreferences.setMockInitialValues({});
    await http.runWithClient(() async {
      final s = AppState();
      await s.init();
      s.airport = 'KMYF';
      await s.run();
      final rerun = s.run();
      await s.ensureRaw(); // tapped mid-run: must wait for the new query
      expect(s.raw, isNull);
      await rerun;
    }, server);
  });

  testWidgets('zone search can be typed and cleared', (tester) async {
    SharedPreferences.setMockInitialValues({});
    await http.runWithClient(() async {
      await entry.main();
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('settings')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('tz-button')));
      await tester.pumpAndSettle();
      await tester.enterText(find.byKey(const Key('tz-search')), 'new y');
      await tester.pumpAndSettle();
      await tester.enterText(find.byKey(const Key('tz-search')), '');
      await tester.pumpAndSettle();
      expect(tester.takeException(), isNull);
      expect(find.byKey(const Key('tz-America/New_York')), findsOneWidget);
    }, server);
  });

  test('history window steps by its own length and stops at the latest data', () async {
    SharedPreferences.setMockInitialValues({});
    await http.runWithClient(() async {
      final s = AppState();
      await s.init();
      s.airport = 'KMYF';
      await s.run(); // fixture history ends 2026-05-27; 30d window
      expect((s.from, s.to, s.windowDays, s.canStepForward), ('2026-04-28', '2026-05-27', 30, false));
      s.stepWindow(-1);
      await pumpEventQueue();
      expect((s.from, s.to, s.window), ('2026-03-29', '2026-04-27', Window.custom));
      expect(s.canStepForward, isTrue);
      s.stepWindow(-1);
      await pumpEventQueue();
      s.stepWindow(1);
      s.stepWindow(1); // two quick taps both count
      await pumpEventQueue();
      expect((s.from, s.to, s.windowDays), ('2026-04-28', '2026-05-27', 30));

      // A window not aligned to the data end: stepping forward clamps at May 27, keeps its length.
      s.update(() {
        s.customFrom = DateTime.utc(2026, 5, 1);
        s.customTo = DateTime.utc(2026, 5, 14);
        s.window = Window.custom;
      }, rerun: true);
      await pumpEventQueue();
      expect((s.from, s.to), ('2026-05-01', '2026-05-14'));
      s.stepWindow(1);
      await pumpEventQueue();
      expect((s.from, s.to, s.windowDays, s.canStepForward), ('2026-05-14', '2026-05-27', 14, false));
    }, server);
  });

  test('a new airport opens the forecast charts on the first page', () async {
    SharedPreferences.setMockInitialValues({});
    await http.runWithClient(() async {
      final s = AppState();
      await s.init();
      s.airport = 'KMYF';
      await s.run();
      s.forecastOffset = 3;
      s.airport = 'KSAN';
      await s.run();
      expect(s.forecastOffset, 0);
    }, server);
  });

  test('a failed step goes back to the window whose numbers are on screen', () async {
    SharedPreferences.setMockInitialValues({});
    await http.runWithClient(() async {
      final s = AppState();
      await s.init();
      s.airport = 'KMYF';
      await s.run();
      final shown = s.stats!;
      failRequests = true;
      s.stepWindow(-1);
      expect(s.from, '2026-03-29'); // shown at once
      await pumpEventQueue();
      expect(s.error, isNotNull);
      expect((s.from, s.to), (shown['date_from'], shown['date_to']));
      expect(identical(s.stats, shown), isTrue);
    }, server);
  });

  test('stepping stops at the first cached day and relights 30d at the latest 30', () async {
    SharedPreferences.setMockInitialValues({});
    await http.runWithClient(() async {
      final s = AppState();
      await s.init();
      s.airport = 'KMYF';
      await s.run(); // fixture history 2024-11-01 → 2026-05-27
      expect(s.canStepBack, isTrue);
      s.stepWindow(-1);
      await pumpEventQueue();
      expect(s.window, Window.custom);
      s.stepWindow(1);
      await pumpEventQueue();
      expect((s.from, s.to, s.window), ('2026-04-28', '2026-05-27', Window.d30));

      s.update(() {
        s.customFrom = DateTime.utc(2024, 11, 10);
        s.customTo = DateTime.utc(2024, 12, 9);
        s.window = Window.custom;
      }, rerun: true);
      await pumpEventQueue();
      s.stepWindow(-1); // would start in October 2024, before the data
      await pumpEventQueue();
      expect((s.from, s.to, s.windowDays, s.canStepBack), ('2024-11-01', '2024-11-30', 30, false));
    }, server);
  });

  test('a preset shows its dates before the reply comes back', () async {
    SharedPreferences.setMockInitialValues({});
    await http.runWithClient(() async {
      final s = AppState();
      await s.init();
      s.airport = 'KMYF';
      await s.run();
      requestGate = Completer<void>();
      s.setWindow(Window.d90);
      expect((s.from, s.to), ('2026-02-27', '2026-05-27'));
      requestGate!.complete();
      await pumpEventQueue();
    }, server);
  });
}
