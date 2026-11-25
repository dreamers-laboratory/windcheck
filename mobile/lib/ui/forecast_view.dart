import 'package:flutter/material.dart';

import '../api.dart';
import '../state.dart';
import 'charts.dart';
import 'kit.dart';

const _days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const _months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

DateTime wallDate(double ms) => DateTime.fromMillisecondsSinceEpoch(ms.round(), isUtc: true);
String shortWhen(double ms) {
  final d = wallDate(ms);
  return '${d.month}/${d.day} ${hour12(d.hour)}';
}

String longWhen(String wall) {
  final d = wallDate(wallMs(wall));
  final mm = d.minute.toString().padLeft(2, '0');
  return '${_days[d.weekday - 1]} ${_months[d.month - 1]} ${d.day} · ${(d.hour + 11) % 12 + 1}:$mm ${d.hour < 12 ? 'AM' : 'PM'}';
}

String modelShort(Json m) => switch (m['id']) {
      'nws_grid' => 'NWS',
      'lamp' => 'LAMP',
      final id => '$id'.toUpperCase(),
    };

class ForecastView extends StatefulWidget {
  const ForecastView({super.key});
  @override
  State<ForecastView> createState() => _ForecastViewState();
}

class _ForecastViewState extends State<ForecastView> {
  bool _guide = false;

  void _shift(int hours) {
    final t = app.target ?? app.forecast?['target_pt'] as String?;
    if (t == null) return;
    final d = wallDate(wallMs(t)).add(Duration(hours: hours));
    _setTarget('${ymd(d)}T${d.hour.toString().padLeft(2, '0')}:${d.minute.toString().padLeft(2, '0')}');
  }

  /// New table time; the charts page over to it when it falls off screen.
  void _setTarget(String target) => app.update(() {
        app.target = target;
        _follow(target);
      });

  /// Page the charts to the one that holds [target], if it isn't on screen.
  /// Call inside app.update.
  void _follow(String target) {
    final models = ((app.forecast?['models'] as List?) ?? const []).cast<Json>();
    final span = _dataSpan(models);
    if (span == null) return;
    final maxOffset = _maxOffset(models);
    final start = span.$1 + app.forecastOffset.clamp(0, maxOffset) * _dayMs;
    final x = wallMs(target);
    if (x >= start && x <= start + app.forecastDays * _dayMs) return;
    // Land on the page that holds it, so −1h and +1h both move a whole span.
    final pageNo = ((x - span.$1) / (app.forecastDays * _dayMs)).floor();
    app.forecastOffset = (pageNo * app.forecastDays).clamp(0, maxOffset);
  }

  Future<void> _pick() async {
    final t = app.target ?? app.forecast?['target_pt'] as String?;
    if (t == null) return;
    final cur = wallDate(wallMs(t));
    final local = DateTime(cur.year, cur.month, cur.day);
    final now = DateTime.now();
    final span = _dataSpan(((app.forecast?['models'] as List?) ?? const []).cast<Json>());
    final end = span == null ? null : wallDate(span.$2);
    final first = DateTime(now.year, now.month, now.day - 1),
        last = end == null ? DateTime(now.year, now.month, now.day + 17) : DateTime(end.year, end.month, end.day);
    final day = await showDatePicker(
        context: context,
        initialDate: local.isBefore(first) ? first : (local.isAfter(last) ? last : local),
        firstDate: first,
        lastDate: last);
    if (day == null || !mounted) return;
    final time = await showTimePicker(context: context, initialTime: TimeOfDay(hour: cur.hour, minute: cur.minute));
    if (time == null) return;
    _setTarget('${ymd(day)}T${time.hour.toString().padLeft(2, '0')}:${time.minute.toString().padLeft(2, '0')}');
  }

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(listenable: app, builder: (context, _) {
      final t = Tok.of(context);
      final f = app.forecast;
      if (f == null) {
        return Empty(app.forecastLoading
            ? 'loading model guidance…'
            : app.forecastError ?? 'enter an airport to see model guidance');
      }
      final models = ((f['models'] as List?) ?? const []).cast<Json>();
      final target = app.target ?? f['target_pt'] as String;
      return LayoutBuilder(builder: (context, box) {
      final wide = box.maxWidth >= 560;
      return Panel(
        title: 'Forecast',
        meta: app.forecastLoading ? 'updating…' : '${f['airport']} · guidance',
        child: Padding(
          padding: const EdgeInsets.all(12),
          child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
            if (app.forecastError != null) ...[
              Text('Update failed: ${app.forecastError}', style: TextStyle(color: t.accent, fontSize: 12)),
              const SizedBox(height: 10),
            ],
            Label('Table time (${app.zoneAbbr})'),
            const SizedBox(height: 6),
            Row(children: [
              SizedBox(width: 48, child: Tog('−1h', height: 40, onTap: () => _shift(-1), semantics: 'one hour earlier')),
              const SizedBox(width: 6),
              Expanded(child: Tog(longWhen(target), key: const Key('target'), height: 40, onTap: _pick)),
              const SizedBox(width: 6),
              SizedBox(width: 48, child: Tog('+1h', height: 40, onTap: () => _shift(1), semantics: 'one hour later')),
            ]),
            const SizedBox(height: 10),
            _table(t, models, wide),
            const SizedBox(height: 18),
            ..._rangeBar(models),
            const SizedBox(height: 14),
            ..._charts(models, target, box.maxWidth >= 800 ? 220 : 170),
            const SizedBox(height: 14),
            _modelGuide(t, f),
          ]),
        ),
      );
      });
    });
  }

  Widget _table(Tok t, List<Json> models, bool wide) {
    final flex = wide ? [6, 9, 6, 6, 4, 5] : [6, 9, 6, 6, 4]; // model column holds the i
    Widget cells(List<Widget> c, {bool head = false}) => Container(
          padding: const EdgeInsets.symmetric(vertical: 7, horizontal: 6),
          decoration: BoxDecoration(
              color: head ? t.hover : null, border: Border(bottom: BorderSide(color: t.line.withValues(alpha: head ? 1 : .25)))),
          child: Row(children: [for (var i = 0; i < c.length; i++) Expanded(flex: flex[i], child: c[i])]),
        );
    Text cell(String s, {Color? color, int lines = 2}) =>
        Text(s, maxLines: lines, style: TextStyle(fontSize: 11.5, color: color, height: 1.25));
    return DecoratedBox(
      position: DecorationPosition.foreground,
      decoration: BoxDecoration(border: Border.all(color: t.line)),
      child: Column(children: [
        cells([
          // The whole header cell opens the guide; the dot alone is a small target.
          GestureDetector(
            behavior: HitTestBehavior.opaque,
            onTap: () => showModelsGuide(context, models),
            child: Row(children: [
              const Flexible(child: FittedBox(fit: BoxFit.scaleDown, alignment: Alignment.centerLeft, child: Sub('model'))),
              InfoDot(onTap: () => showModelsGuide(context, models)),
            ]),
          ),
          for (final h in ['wind', 'xwind', 'ceil', 'vis', if (wide) 'DA']) Sub(h),
        ], head: true),
        for (final m in models)
          () {
            final p = app.pointAt(m);
            if (p == null) {
              return Container(
                padding: const EdgeInsets.symmetric(vertical: 7, horizontal: 6),
                decoration: BoxDecoration(border: Border(bottom: BorderSide(color: t.line.withValues(alpha: .25)))),
                child: Row(children: [
                  Expanded(flex: flex[0], child: cell(modelShort(m))),
                  Expanded(
                      flex: flex.skip(1).fold(0, (a, b) => a + b),
                      child: cell('not returned · ${_reason(m['missing_reason'])}', color: t.muted, lines: 3)),
                ]),
              );
            }
            final dir = p['wind_dir_deg'] == null ? 'VRB' : '${pad3(p['wind_dir_deg'])}°';
            final gust = p['gust_kt'] == null ? '' : ' G${fmtInt(p['gust_kt'])}';
            final ceil = p['ceiling_ft'] == null
                ? '–'
                : (p['ceiling_label'] != null && '${p['ceiling_label']}'.startsWith('no ceiling') ? 'none' : compact(p['ceiling_ft'] as num));
            final da = p['density_altitude_ft'];
            return cells([
              cell(modelShort(m)),
              cell(p['wind_speed_kt'] == null ? 'not returned' : '$dir ${fmtInt(p['wind_speed_kt'])}$gust',
                  color: p['wind_speed_kt'] == null ? t.muted : null),
              cell(p['xwind_kt'] == null ? '–' : '${fmt(p['xwind_kt'])}${p['active_end_ident'] == null ? '' : ' ${p['active_end_ident']}'}'),
              cell(ceil),
              cell(p['visibility_sm'] == null ? '–' : fmt(p['visibility_sm'], 0)),
              if (wide) cell(da == null ? '–' : compact(da as num), color: p['density_altitude_high'] == true ? t.accent : null),
            ]);
          }(),
      ]),
    );
  }

  /// Server reasons are short phrases, except raw network errors.
  static String _reason(Object? r) {
    final s = '${r ?? "outside this model's horizon"}';
    return RegExp(r'ConnectionPool|Max retries|Errno|timed out|Timeout', caseSensitive: false).hasMatch(s)
        ? 'source unreachable right now'
        : s;
  }

  /// First and last forecast hour across models (wall-clock ms), or null.
  static (double, double)? _dataSpan(List<Json> models) {
    final all = [
      for (final m in models)
        for (final p in ((m['points'] as List?) ?? const []).cast<Json>()) wallMs(p['time_pt'] as String)
    ];
    return all.isEmpty ? null : (all.reduce((a, b) => a < b ? a : b), all.reduce((a, b) => a > b ? a : b));
  }

  static const _dayMs = 86400000.0;

  /// Pages left in this span: the chart window can move this far right.
  int _maxOffset(List<Json> models) {
    final span = _dataSpan(models);
    if (span == null) return 0;
    final days = ((span.$2 - span.$1) / _dayMs).ceil();
    return (days - app.forecastDays).clamp(0, 99);
  }

  /// ‹ 48H 5D 16D › and the dates on screen; arrows page by the span.
  List<Widget> _rangeBar(List<Json> models) {
    final maxOffset = _maxOffset(models);
    final offset = app.forecastOffset.clamp(0, maxOffset);
    final span = _dataSpan(models);
    // Read the offset at tap time: quick taps can land before the next frame.
    void page(int dir) => app.update(() =>
        app.forecastOffset = (app.forecastOffset.clamp(0, maxOffset) + dir * app.forecastDays).clamp(0, maxOffset));
    return [
      Row(children: [
        SizedBox(
          width: 44,
          child: Tog('‹', key: const Key('fc-prev'), height: 36, size: 20, semantics: 'earlier forecast',
              onTap: offset > 0 ? () => page(-1) : null),
        ),
        const SizedBox(width: 6),
        Expanded(
          child: Segmented<int>(
            height: 36,
            value: app.forecastDays,
            items: const [(2, '48h', 1), (5, '5 days', 1), (16, '16 days', 1)],
            onChanged: (d) => app.update(() {
              app.forecastDays = d;
              app.forecastOffset = 0;
              // A new span still shows the table time.
              final t = app.target ?? app.forecast?['target_pt'] as String?;
              if (t != null) _follow(t);
            }),
          ),
        ),
        const SizedBox(width: 6),
        SizedBox(
          width: 44,
          child: Tog('›', key: const Key('fc-next'), height: 36, size: 20, semantics: 'later forecast',
              onTap: offset < maxOffset ? () => page(1) : null),
        ),
      ]),
      if (span != null) ...[
        const SizedBox(height: 6),
        Sub(() {
          final a = span.$1 + offset * _dayMs;
          final b = [a + app.forecastDays * _dayMs, span.$2].reduce((x, y) => x < y ? x : y);
          String d(double ms) => longWhen(DateTime.fromMillisecondsSinceEpoch(ms.round(), isUtc: true)
                  .toIso8601String()
                  .substring(0, 16))
              .split(' · ')
              .first;
          return 'Showing ${d(a)} → ${d(b)}';
        }()),
      ],
    ];
  }

  List<Widget> _charts(List<Json> models, String target, double height) {
    final pts = <String, List<Json>>{
      for (final m in models) modelShort(m): ((m['points'] as List?) ?? const []).cast<Json>()
    };
    final data = _dataSpan(models);
    if (data == null) return const [Empty('no model returned chartable guidance')];
    final start = data.$1 + app.forecastOffset.clamp(0, _maxOffset(models)) * _dayMs;
    final end = start + app.forecastDays * 86400000;
    final dayStart = (start / 86400000).ceil() * 86400000.0;
    final step = app.forecastDays <= 2 ? 1 : (app.forecastDays <= 5 ? 1 : 3);
    final ticks = [for (var d = dayStart; d <= end; d += step * 86400000.0) d];
    LineChart chart(String title, String unit, String key) => LineChart(
          title: title,
          unit: unit,
          legend: true,
          height: height,
          xMin: start,
          xMax: end,
          gapX: 7 * 3600000,
          xTicks: ticks,
          xLabel: shortWhen,
          tickLabel: (x) => shortWhen(x).split(' ').first,
          series: [
            for (final m in models)
              if (pts[modelShort(m)]!.any((p) {
                final x = wallMs(p['time_pt'] as String);
                return p[key] != null && x >= start && x <= end;
              }))
                ChartSeries(modelShort(m), [
                  for (final p in pts[modelShort(m)]!)
                    if (wallMs(p['time_pt'] as String) >= start && wallMs(p['time_pt'] as String) <= end)
                      Offset(wallMs(p['time_pt'] as String), num_(p[key]) ?? double.nan),
                ], dash: modelDash[m['id']]),
          ],
        );
    return [
      chart('Forecast wind speed', 'kt', 'wind_speed_kt'),
      const SizedBox(height: 16),
      chart('Forecast crosswind', 'kt', 'xwind_kt'),
      const SizedBox(height: 16),
      chart('Forecast ceiling', 'ft', 'ceiling_ft'),
    ];
  }

  Widget _modelGuide(Tok t, Json f) {
    final info = ((f['tool_info'] as List?) ?? const []).cast<Json>();
    return DecoratedBox(
      position: DecorationPosition.foreground,
      decoration: BoxDecoration(border: Border.all(color: t.line)),
      child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        InkWell(
          onTap: () => setState(() => _guide = !_guide),
          child: Padding(
            padding: const EdgeInsets.all(12),
            child: Row(children: [
              const Expanded(child: Label('Forecast model guide')),
              Text(_guide ? '−' : '+', style: const TextStyle(fontSize: 14)),
            ]),
          ),
        ),
        AnimatedSize(
          duration: const Duration(milliseconds: 220),
          alignment: Alignment.topCenter,
          child: !_guide
              ? const SizedBox(width: double.infinity)
              : Padding(
                  padding: const EdgeInsets.fromLTRB(12, 0, 12, 12),
                  child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
                    for (final m in info)
                      Padding(
                        padding: const EdgeInsets.only(top: 10),
                        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                          Label('${m['label']} · ${m['full_name'] ?? ''}', bold: true),
                          const SizedBox(height: 3),
                          Text('${m['what'] ?? ''}', style: const TextStyle(fontSize: 12)),
                          Sub('range ${m['range'] ?? '–'} · ${m['spatial'] ?? ''}'),
                          if (m['limits'] != null) Sub('${m['limits']}'),
                        ]),
                      ),
                  ]),
                ),
        ),
      ]),
    );
  }
}

/// "Which forecast to trust": a short bottom sheet behind the i by Model.
Future<void> showModelsGuide(BuildContext context, List<Json> models) {
  const horizons = [
    ('Next few hours', 'TAF if this airport has one. LAMP updates hourly from the latest observations.'),
    ('Today, tomorrow', 'TAF, then HRRR and NAM 3 km for the timing of wind shifts, fog and low cloud.'),
    ('2 to 7 days', 'NBM and NWS gridpoint: blends, the steadiest guess.'),
    ('Beyond a week', 'NBM or GFS, for the trend only.'),
  ];
  final info = ((app.forecast?['tool_info'] as List?) ?? const []).cast<Json>();
  return showModalBottomSheet(
    context: context,
    isScrollControlled: true,
    useSafeArea: true,
    builder: (context) {
      final t = Tok.of(context);
      Widget row(String k, String v) => Padding(
            padding: const EdgeInsets.only(bottom: 10),
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              Sub(k),
              const SizedBox(height: 2),
              Text(v, style: const TextStyle(fontSize: 13, height: 1.35)),
            ]),
          );
      return DraggableScrollableSheet(
        expand: false,
        initialChildSize: .7,
        maxChildSize: .92,
        builder: (context, scroll) => ListView(controller: scroll, padding: const EdgeInsets.fromLTRB(16, 8, 16, 28), children: [
          Row(children: [
            const Expanded(child: Label('Which forecast to trust', bold: true)),
            IconButton(tooltip: 'Close', onPressed: () => Navigator.pop(context), icon: const Icon(Icons.close, size: 20)),
          ]),
          const SizedBox(height: 6),
          for (final (k, v) in horizons) row(k, v),
          Text('When the models agree, trust the forecast more; a wide spread means expect change. A planning aid, not a briefing.',
              style: TextStyle(fontSize: 11, color: t.muted, height: 1.35)),
          const SizedBox(height: 18),
          const Label('The models', bold: true),
          const SizedBox(height: 10),
          for (final m in info) row('${m['label']}', '${m['use'] ?? m['what'] ?? ''}'),
        ]),
      );
    },
  );
}
