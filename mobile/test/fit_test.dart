// Responsive fit suite: every viewport x text scale x screen, served from
// test/fixtures. Fails on any framework error (RenderFlex overflow etc.) or a
// clipped / mid-word-broken table value. Writes a full-page PNG per case to
// test/goldens/ for review (always rewritten, never compared).
//
//   flutter test test/fit_test.dart
import 'dart:io';
import 'dart:math' as math;

import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';
import 'package:flutter/widgets.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'package:windcheck/main.dart';
import 'package:windcheck/state.dart';

// responsive-design suite + the extra phone/tablet sizes.
const viewports = [
  Size(320, 568), Size(360, 800), Size(375, 667), Size(390, 844), Size(430, 932), Size(844, 390),
  Size(768, 1024), Size(1024, 1366), Size(1366, 1024), Size(1366, 768), Size(1512, 982),
  Size(1920, 1080), Size(2560, 1080), Size(2560, 1440),
];
const scales = [1.0, 1.3];
const screens = ['forecast', 'history', 'obs', 'settings', 'inputs'];

// Real mono metrics instead of Ahem boxes (Ahem is ~1.7x wider than mono).
// ponytail: macOS host font; without it the run falls back to Ahem.
const menlo = '/System/Library/Fonts/Menlo.ttc';

final fixtures = {
  for (final n in ['info', 'stats', 'raw', 'forecast']) '/api/$n': File('test/fixtures/$n.json').readAsStringSync()
};
http.Client mock() => MockClient((r) async =>
    http.Response(fixtures[r.url.path]!, 200, headers: {'content-type': 'application/json; charset=utf-8'}));

Future<void> open(WidgetTester tester, String screen) async {
  if (screen == 'obs') {
    // Every observation sits at the foot of History, below the fold: lay the page out first.
    await open(tester, 'history');
    await fullPage(tester);
  }
  final key = switch (screen) {
    'history' => 'seg-History',
    'obs' => 'obs-toggle',
    'settings' => 'settings',
    'inputs' => 'edit-input', // phones only; the split layout never folds
    _ => null,
  };
  if (key == null) return;
  final target = find.byKey(Key(key));
  if (target.evaluate().isEmpty) return;
  await tester.ensureVisible(target);
  await tester.pumpAndSettle();
  await tester.tap(target);
  await tester.pumpAndSettle();
}

/// Grow the view to the full scroll height so every sliver is laid out and painted.
Future<void> fullPage(WidgetTester tester) async {
  for (var i = 0; i < 4; i++) {
    final more = tester
        .stateList<ScrollableState>(find.byType(Scrollable))
        .where((s) => s.position.axis == Axis.vertical)
        .fold(0.0, (a, s) => math.max(a, s.position.maxScrollExtent));
    if (more < 1) return;
    tester.view.physicalSize += Offset(0, more.ceilToDouble());
    await tester.pumpAndSettle();
  }
}

void main() {
  autoUpdateGoldenFiles = true; // screenshots for humans, not comparisons

  setUpAll(() async {
    if (File(menlo).existsSync()) {
      final bytes = File(menlo).readAsBytesSync();
      await (FontLoader('monospace')..addFont(Future.value(ByteData.sublistView(bytes)))).load();
    }
  });

  for (final vp in viewports) {
    for (final scale in scales) {
      for (final screen in screens) {
        final name = '${vp.width.round()}x${vp.height.round()}@$scale-$screen';
        testWidgets(name, (tester) async {
          tester.view
            ..physicalSize = vp
            ..devicePixelRatio = 1;
          tester.platformDispatcher.textScaleFactorTestValue = scale;
          addTearDown(tester.view.reset);
          addTearDown(tester.platformDispatcher.clearTextScaleFactorTestValue);
          SharedPreferences.setMockInitialValues({'airport': 'KMYF'});
          tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(
              const MethodChannel('flutter_timezone'), (_) async => 'America/Los_Angeles');

          final errors = <FlutterErrorDetails>[];
          final cut = <String>[], warn = <String>[];
          final onError = FlutterError.onError;
          FlutterError.onError = errors.add;
          try {
            await http.runWithClient(() async {
              await app.init();
              await tester.pumpWidget(const WindcheckApp());
              await tester.pumpAndSettle();
              await open(tester, screen);
              await fullPage(tester);

              for (final e in find.byType(RichText).evaluate()) {
                final p = e.renderObject! as RenderParagraph;
                final text = p.text.toPlainText();
                final where = '"$text" (w ${p.size.width.toStringAsFixed(1)})';
                final broken = p.getMinIntrinsicWidth(double.infinity) > p.size.width + .5;
                final wraps = p.getMaxIntrinsicWidth(double.infinity) > p.size.width + .5;
                final value = p.text.style?.fontSize == 11.5; // forecast / history / obs table cells
                if (p.didExceedMaxLines || broken) {
                  (value ? cut : warn).add('${p.didExceedMaxLines ? 'clipped' : 'mid-word break'} $where');
                } else if (wraps && value) {
                  warn.add('wrapped cell $where');
                } else if (value && p.getMaxIntrinsicWidth(double.infinity) > p.size.width - 3) {
                  warn.add('cell fills its column, touches neighbour $where');
                }
              }
              await expectLater(find.byType(WindcheckApp), matchesGoldenFile('goldens/$name.png'));
            }, mock);
          } finally {
            FlutterError.onError = onError;
          }

          final seen = <String>{};
          final unique = [for (final e in errors) if (seen.add(e.exceptionAsString())) e];
          // ignore: avoid_print
          print('FIT $name ${unique.isEmpty && cut.isEmpty ? 'PASS' : 'FAIL'} '
              'errors=${errors.length} clipped=${cut.length} warn=${warn.length}'
              '${[for (final e in unique) '\n  ERROR $e', for (final c in cut) '\n  CUT $c', for (final w in warn) '\n  WARN $w'].join()}');
          expect(unique.map((e) => e.exceptionAsString()), isEmpty, reason: 'framework errors');
          expect(cut, isEmpty, reason: 'table values clipped or broken mid-word');
        });
      }
    }
  }
}
