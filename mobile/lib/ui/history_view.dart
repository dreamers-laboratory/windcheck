import 'package:flutter/material.dart';

import '../api.dart';
import '../state.dart';
import 'charts.dart';
import 'kit.dart';
import 'range_bar.dart';

/// Overview: one row per selected hour, plus four hour-of-day charts.
class HistoryView extends StatelessWidget {
  const HistoryView({super.key});

  @override
  Widget build(BuildContext context) {
    return ListenableBuilder(listenable: app, builder: (context, _) {
      final t = Tok.of(context);
      final s = app.stats;
      if (s == null) {
        return Empty(app.loading ? 'loading history…' : app.error ?? 'run a query to see results');
      }
      final rows = ((s['rows'] as List?) ?? const []).cast<Json>();
      return LayoutBuilder(builder: (context, box) {
      final wide = box.maxWidth >= 700;
      final primary = s['primary_ident'];
      final labels = [for (final r in rows) hour12(r['hour_pt'] as int)];
      String xl(double x) => x.round() >= 0 && x.round() < labels.length ? labels[x.round()] : '';
      final ticks = [for (var i = 0; i < rows.length; i++) i.toDouble()];
      Offset at(int i, Object? v) => Offset(i.toDouble(), num_(v) ?? double.nan);
      double? mean(Json r, String k) => num_((r[k] as Map?)?['mean']);

      final charts = <Widget>[
        LineChart(title: 'Wind speed by hour', unit: 'kt', xLabel: xl, xTicks: ticks, xTitle: 'hour (${app.zoneAbbr})', legend: true, series: [
          ChartSeries('mean', [for (var i = 0; i < rows.length; i++) at(i, mean(rows[i], 'wind_speed'))], dots: true),
          ChartSeries('gust', [for (var i = 0; i < rows.length; i++) at(i, mean(rows[i], 'gust'))], dash: [4, 3]),
        ]),
        if (s['no_runway_data'] != true)
          LineChart(title: 'Crosswind by hour', unit: 'kt', xLabel: xl, xTicks: ticks, xTitle: 'hour (${app.zoneAbbr})', series: [
            ChartSeries('xwind', [for (var i = 0; i < rows.length; i++) at(i, mean(rows[i], 'xwind'))],
                dots: true,
                dotColors: [
                  for (final r in rows)
                    r['is_mixed'] == true || (r['active_end_ident'] != null && r['active_end_ident'] != primary)
                        ? t.accent
                        : null
                ]),
          ]),
        LineChart(title: 'Ceiling by hour', unit: 'ft', xLabel: xl, xTicks: ticks, xTitle: 'hour (${app.zoneAbbr})', series: [
          ChartSeries('ceiling', [for (var i = 0; i < rows.length; i++) at(i, mean(rows[i], 'ceiling'))], dots: true),
        ]),
        LineChart(title: 'Beneath minimums', unit: '%', bars: true, xLabel: xl, xTicks: ticks, xTitle: 'hour (${app.zoneAbbr})', series: [
          ChartSeries('below', [
            for (var i = 0; i < rows.length; i++)
              at(i, (rows[i]['obs_count'] as num) > 0 ? 100 * (rows[i]['ct_min_breached'] as num) / (rows[i]['obs_count'] as num) : null)
          ]),
        ]),
      ];

      return Panel(
        title: 'Historical',
        meta: app.loading ? 'updating…' : 'selected hours · ${app.zoneAbbr}',
        child: Padding(
          padding: const EdgeInsets.all(12),
          child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
            const HistoryRangeBar(),
            const SizedBox(height: 12),
            _facts(t, s),
            if (app.error != null) ...[
              const SizedBox(height: 10),
              Text('Update failed: ${app.error}', style: TextStyle(color: t.accent, fontSize: 12)),
            ],
            if (app.substitute != null) ...[
              const SizedBox(height: 10),
              Container(
                padding: const EdgeInsets.all(10),
                decoration: BoxDecoration(border: Border.all(color: t.accent)),
                child: Text(
                    '${app.info?['airport']} has no METAR feed. Weather from ${app.substitute} '
                    '(nearest reporting station) against ${app.info?['airport']} runways.',
                    style: const TextStyle(fontSize: 12)),
              ),
            ],
            const SizedBox(height: 14),
            if ((s['total_obs'] ?? 0) == 0)
              Empty('No observations in ${app.from} → ${app.to}. '
                  'History runs through ${app.weatherInfo?['last_obs_pt'] ?? 'nothing cached yet'}.')
            else ...[
              Wrap(spacing: 16, runSpacing: 18, children: [
                for (final c in charts)
                  LayoutBuilder(builder: (context, box) => SizedBox(
                      width: wide ? (box.maxWidth - 16) / 2 : box.maxWidth, child: c)),
              ]),
              const SizedBox(height: 18),
              _table(t, rows, wide, primary),
              const SizedBox(height: 10),
              const Sub('avg dir is speed-weighted and skips VRB/calm. xwind excludes VRB or missing direction. '
                  'Blue = hour used a non-primary runway end. Beneath minimums = low vis, low ceiling or xwind above your max.'),
            ],
          ]),
        ),
      );
      });
    });
  }

  Widget _facts(Tok t, Json s) {
    final ident = s['runway_ident'];
    final runways = ((s['runways'] as List?) ?? const []).cast<Json>();
    final selected = runways.where((r) => r['ident'] == ident);
    final label = ident == null
        ? '${pad3(s['runway_heading'])}°T'
        : 'RWY ${runwayLabel(selected.isEmpty ? {'ident': ident} : selected.first)}';
    final rw = s['no_runway_data'] == true
        ? 'no runway data'
        : '$label${s['runway_auto'] == true ? ' auto' : ''}';
    final facts = [
      ('airport', '${app.info?['airport'] ?? s['airport']}'),
      ('runway', rw),
      ('vis<', '${compact(s['vis_min_sm'] as num)} sm'),
      ('ceil<', '${compact(s['ceiling_min_ft'] as num)} ft'),
      ('xw max', '${compact(s['xwind_max_kt'] as num)} kt'),
      ('obs', '${s['total_obs']}'),
    ];
    return Wrap(spacing: 14, runSpacing: 6, children: [
      for (final (k, v) in facts)
        Row(mainAxisSize: MainAxisSize.min, children: [
          Sub(k),
          const SizedBox(width: 5),
          Text(v, style: const TextStyle(fontSize: 12)),
        ]),
    ]);
  }

  Widget _table(Tok t, List<Json> rows, bool wide, Object? primary) {
    final cols = <(String, int, String Function(Json))>[
      ('hour', 5, (r) => wide ? hourLabel(r['hour_pt'] as int) : hour12(r['hour_pt'] as int)),
      ('n', 3, (r) => '${r['obs_count']}'),
      if (wide) ('dir', 4, (r) => r['avg_dir_deg'] == null ? '–' : '${pad3(r['avg_dir_deg'])}°'),
      ('wind', 4, (r) => fmt((r['wind_speed'] as Map?)?['mean'])),
      if (wide) ('w sd', 4, (r) => fmt((r['wind_speed'] as Map?)?['stdev'])),
      ('gust', 4, (r) => fmt((r['gust'] as Map?)?['mean'])),
      ('xw', 4, (r) => fmt((r['xwind'] as Map?)?['mean'])),
      if (wide) ('xw max', 4, (r) => fmt((r['xwind'] as Map?)?['max'])),
      if (wide) ('rwy', 4, (r) => '${r['active_end_ident'] ?? '–'}${r['is_mixed'] == true ? '+' : ''}'),
      ('ceil', 4, (r) => (r['ceiling'] as Map?)?['mean'] == null ? '–' : compact(((r['ceiling'] as Map)['mean'] as num).round())),
      ('below', 4, (r) => (r['obs_count'] as num) == 0 ? '–' : '${(100 * (r['ct_min_breached'] as num) / (r['obs_count'] as num)).round()}%'),
    ];
    Widget line(List<String> v, {bool head = false, Color? rwyColor}) => Container(
          padding: const EdgeInsets.symmetric(vertical: 7, horizontal: 6),
          decoration: BoxDecoration(
              color: head ? t.hover : null,
              border: Border(bottom: BorderSide(color: t.line.withValues(alpha: head ? 1 : .25)))),
          child: Row(children: [
            for (var i = 0; i < cols.length; i++)
              Expanded(
                flex: cols[i].$2,
                child: head
                    ? Align(alignment: i == 0 ? Alignment.centerLeft : Alignment.centerRight, child: Sub(v[i]))
                    : Num(v[i], left: i == 0, color: cols[i].$1 == 'rwy' ? rwyColor : null),
              ),
          ]),
        );
    return DecoratedBox(
      position: DecorationPosition.foreground,
      decoration: BoxDecoration(border: Border.all(color: t.line)),
      child: Column(children: [
        line([for (final c in cols) c.$1], head: true),
        for (final r in rows)
          line([for (final c in cols) c.$3(r)],
              rwyColor: r['active_end_ident'] != null && r['active_end_ident'] != primary ? t.accent : null),
      ]),
    );
  }
}
