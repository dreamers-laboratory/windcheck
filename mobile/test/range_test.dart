// Range navigation on the Forecast and History tabs, served from test/fixtures.
import 'dart:convert';
import 'dart:io';

import 'package:flutter/services.dart';
import 'package:flutter/widgets.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:windcheck/main.dart';
import 'package:windcheck/state.dart';
import 'package:windcheck/ui/kit.dart';
import 'package:windcheck/ui/forecast_view.dart' show ForecastView;
import 'package:windcheck/ui/history_view.dart';

final fixtures = {
  for (final n in ['info', 'stats', 'raw', 'forecast']) '/api/$n': File('test/fixtures/$n.json').readAsStringSync()
};
// The app keeps one client for its lifetime, so tests steer failures through these.
var rawFailures = 0, rawCalls = 0;
var rawDelay = Duration.zero;
http.Client mock() => MockClient((r) async {
      if (r.url.path == '/api/raw') {
        rawCalls++;
        await Future<void>.delayed(rawDelay);
        if (rawFailures > 0) {
          rawFailures--;
          throw const SocketException('down');
        }
      }
      return http.Response(fixtures[r.url.path]!, 200, headers: {'content-type': 'application/json; charset=utf-8'});
    });

Future<void> boot(WidgetTester tester) async {
  tester.view
    ..physicalSize = const Size(390, 3200) // tall, so lazy slivers below the fold are built
    ..devicePixelRatio = 1;
  addTearDown(tester.view.reset);
  SharedPreferences.setMockInitialValues({'airport': 'KMYF'});
  tester.binding.defaultBinaryMessenger
      .setMockMethodCallHandler(const MethodChannel('flutter_timezone'), (_) async => 'America/Los_Angeles');
  app.wantRaw = false; // the app starts on Forecast, section folded; don't inherit the last test's state
  await app.init();
  await tester.pumpWidget(const WindcheckApp());
  await tester.pumpAndSettle();
}

bool enabled(WidgetTester tester, String key) => tester.widget<Tog>(find.byKey(Key(key))).onTap != null;

void main() {
  // Real mono metrics; Ahem's wide boxes overflow the header row.
  setUpAll(() async {
    const menlo = '/System/Library/Fonts/Menlo.ttc';
    if (File(menlo).existsSync()) {
      final bytes = File(menlo).readAsBytesSync();
      await (FontLoader('monospace')..addFont(Future.value(ByteData.sublistView(bytes)))).load();
    }
  });

  testWidgets('moving the table time past the charts pages them along', (tester) async {
    await http.runWithClient(() async {
      await boot(tester);
      app.update(() {
        app.forecastDays = 2;
        app.forecastOffset = 0;
      });
      await tester.pumpAndSettle();
      await tester.ensureVisible(find.byKey(const Key('fc-prev')));
      expect(enabled(tester, 'fc-prev'), isFalse);
      // Fixture target is Oct 26 11:00; the first 48 h page (from Oct 25) ends Oct 27 00:00.
      final later = find.text('+1H');
      await tester.ensureVisible(later);
      for (var i = 0; i < 14; i++) {
        await tester.tap(later);
        await tester.pump();
      }
      await tester.pumpAndSettle();
      expect(app.target, '2026-10-27T01:00');
      expect(app.forecastOffset, 1); // day 2 of a 2.2-day fixture, clamped to the last full page
      await tester.ensureVisible(find.byKey(const Key('fc-prev')));
      expect(enabled(tester, 'fc-prev'), isTrue);
      expect(find.textContaining(RegExp('showing mon oct 26', caseSensitive: false)), findsOneWidget);
    }, mock);
  });

  testWidgets('history range bar shows the window it just stepped to', (tester) async {
    await http.runWithClient(() async {
      await boot(tester);
      await tester.tap(find.byKey(const Key('seg-History')));
      await tester.pumpAndSettle();
      String span() => tester.widget<Text>(find.descendant(of: find.byKey(const Key('range-span')), matching: find.byType(Text))).data!;
      final before = span();
      expect(enabled(tester, 'range-next'), isFalse);
      await tester.tap(find.byKey(const Key('range-prev')));
      await tester.pumpAndSettle();
      final after = span();
      expect(after, isNot(before));
      expect(after, contains('${app.windowDays} days'));
      expect(app.window, Window.custom);
      expect(enabled(tester, 'range-next'), isTrue);
      await tester.tap(find.byKey(const Key('range-next')));
      await tester.pumpAndSettle();
      expect(span(), before);
    }, mock);
  });

  testWidgets('quick taps on the forecast arrows each move a page', (tester) async {
    await http.runWithClient(() async {
      await boot(tester);
      // Stretch one model to 16 days so there are several 48 h pages.
      final f = Map<String, dynamic>.from(app.forecast!);
      final base = DateTime.utc(2026, 10, 25);
      String at(int h) => base.add(Duration(hours: h)).toIso8601String().substring(0, 16);
      f['models'] = [
        <String, dynamic>{
          ...((f['models'] as List).first as Map<String, dynamic>),
          'points': [for (var h = 0; h < 16 * 24; h++) <String, dynamic>{'time_pt': at(h), 'wind_speed_kt': 5}],
        },
      ];
      app.update(() {
        app.forecast = f;
        app.forecastDays = 2;
        app.forecastOffset = 0;
      });
      await tester.pumpAndSettle();
      final next = find.byKey(const Key('fc-next'));
      await tester.ensureVisible(next);
      await tester.tap(next);
      await tester.tap(next); // before the next frame
      await tester.pumpAndSettle();
      expect(app.forecastOffset, 4);
    }, mock);
  });

  testWidgets('changing the chart span keeps the table time on screen', (tester) async {
    await http.runWithClient(() async {
      await boot(tester);
      final f = Map<String, dynamic>.from(app.forecast!);
      final base = DateTime.utc(2026, 10, 25);
      String at(int h) => base.add(Duration(hours: h)).toIso8601String().substring(0, 16);
      f['models'] = [
        <String, dynamic>{
          ...((f['models'] as List).first as Map<String, dynamic>),
          'points': [for (var h = 0; h < 16 * 24; h++) <String, dynamic>{'time_pt': at(h), 'wind_speed_kt': 5}],
        },
      ];
      app.update(() {
        app.forecast = f;
        app.forecastDays = 16;
        app.forecastOffset = 0;
        app.target = '2026-11-03T12:00'; // day 9
      });
      await tester.pumpAndSettle();
      final h48 = find.byKey(const Key('seg-48h'));
      await tester.ensureVisible(h48);
      await tester.tap(h48);
      await tester.pumpAndSettle();
      expect((app.forecastDays, app.forecastOffset), (2, 8)); // the Nov 2–4 page holds Nov 3
    }, mock);
  });

  testWidgets('Every observation sits folded under History and loads only when opened', (tester) async {
    await http.runWithClient(() async {
      await boot(tester);
      rawCalls = 0;
      await tester.tap(find.byKey(const Key('seg-History')));
      await tester.pumpAndSettle();
      final toggle = find.byKey(const Key('obs-toggle'));
      expect(toggle, findsOneWidget);
      expect(find.textContaining(RegExp('charts: all hours', caseSensitive: false)), findsNothing);
      expect(rawCalls, 0); // folded: nothing fetched
      // Below the History panel, not above it.
      expect(tester.getTopLeft(toggle).dy, greaterThan(tester.getBottomLeft(find.byType(HistoryView)).dy));
      tester.view.physicalSize = const Size(390, 844); // a phone screen, so opening has to scroll
      await tester.pumpAndSettle();
      final page = find.descendant(of: find.byType(CustomScrollView), matching: find.byType(Scrollable)).first;
      await tester.scrollUntilVisible(toggle, 200, scrollable: page);
      await tester.ensureVisible(toggle);
      expect(tester.getTopLeft(toggle).dy, greaterThan(400)); // reached from below: low on the screen
      rawDelay = const Duration(seconds: 2); // a real network: the header must come up before the data does
      addTearDown(() => rawDelay = Duration.zero);
      await tester.tap(toggle);
      await tester.pump(); // opens; the scroll starts after this frame
      await tester.pump(const Duration(milliseconds: 16)); // its first tick
      await tester.pump(const Duration(milliseconds: 400));
      expect(app.rawLoading, isTrue);
      expect(tester.getTopLeft(toggle).dy, lessThan(120)); // brought up to the top
      await tester.pump(const Duration(seconds: 2));
      await tester.pumpAndSettle();
      rawDelay = Duration.zero;
      expect(rawCalls, 1);
      expect(tester.getTopLeft(toggle).dy, lessThan(120)); // and stays there once the charts arrive
      tester.view.physicalSize = const Size(390, 3200);
      tester.state<ScrollableState>(page).position.jumpTo(0);
      await tester.pumpAndSettle();
      expect(find.textContaining(RegExp('charts: all hours', caseSensitive: false)), findsOneWidget);
      final reportCount = jsonDecode(fixtures['/api/raw']!)['total_obs'];
      expect(find.textContaining(RegExp('$reportCount reports', caseSensitive: false)), findsOneWidget);
      // Stays open across tabs without fetching again.
      await tester.tap(find.byKey(const Key('seg-Forecast')));
      await tester.pumpAndSettle();
      expect(app.wantRaw, isFalse);
      await tester.tap(find.byKey(const Key('seg-History')));
      await tester.pumpAndSettle();
      expect(find.textContaining(RegExp('charts: all hours', caseSensitive: false)), findsOneWidget);
      expect(rawCalls, 1);
      await tester.tap(toggle);
      await tester.pumpAndSettle();
      expect(find.textContaining(RegExp('charts: all hours', caseSensitive: false)), findsNothing);
      expect(app.wantRaw, isFalse);
    }, mock);
  });

  testWidgets('a failed Every observation load can be retried', (tester) async {
    await http.runWithClient(() async {
      await boot(tester);
      rawFailures = 1;
      rawCalls = 0;
      await tester.tap(find.byKey(const Key('seg-History')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('obs-toggle')));
      await tester.pumpAndSettle();
      expect(find.textContaining('Can’t reach'), findsOneWidget);
      await tester.tap(find.byKey(const Key('raw-retry')));
      await tester.pumpAndSettle();
      expect(find.textContaining('Can’t reach'), findsNothing);
      expect(app.raw, isNotNull);
      // Coming back to History with the section open also retries a failure.
      app.update(() {
        app.raw = null;
        app.rawError = 'Can’t reach x';
      });
      await tester.tap(find.byKey(const Key('seg-Forecast')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('seg-History')));
      await tester.pumpAndSettle();
      expect(app.raw, isNotNull);
      expect(rawCalls, 3);
    }, mock);
  });

  test('date spans show a year only once they leave this one', () {
    final y = DateTime.now().year;
    String n(String s) => s.replaceAll('\u00a0', ' '); // dates use no-break spaces
    expect(n(dateSpan('$y-10-06', '$y-11-04')), 'Oct 6 → Nov 4');
    expect(n(dateSpan('2025-02-27', '2025-12-31')), y == 2025 ? 'Feb 27 → Dec 31' : 'Feb 27 → Dec 31, 2025');
    expect(n(dateSpan('2023-06-22', '2024-04-24')), 'Jun 22, 2023 → Apr 24, 2024');
    expect(n(dateSpan('2023-06-22', '2024-04-24', sep: '–')), 'Jun 22, 2023–Apr 24, 2024');
  });

  testWidgets('system Back goes from History to Forecast, then leaves', (tester) async {
    await http.runWithClient(() async {
      await boot(tester);
      Future<void> go(String tab) async {
        await tester.tap(find.byKey(Key('seg-$tab')));
        await tester.pumpAndSettle();
      }
      Future<void> back() async {
        await tester.binding.handlePopRoute();
        await tester.pumpAndSettle();
      }
      String shown() => find.byType(ForecastView).evaluate().isNotEmpty ? 'forecast' : 'history';
      final calls = <MethodCall>[];
      tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(SystemChannels.platform, (c) async {
        calls.add(c);
        return null;
      });
      bool left() => calls.any((c) => c.method == 'SystemNavigator.pop');

      await go('History');
      await back();
      expect(shown(), 'forecast');
      expect(left(), isFalse);
      // Forecast → History → Forecast: Back leaves from Forecast, it doesn't reopen History.
      await go('History');
      await go('Forecast');
      await back();
      expect(shown(), 'forecast');
      expect(left(), isTrue);
    }, mock);
  });
}
