import 'package:flutter/material.dart';

import '../api.dart';
import '../state.dart';
import 'charts.dart';
import 'forecast_view.dart' show wallDate;
import 'kit.dart';

String _day(double ms) {
  final d = wallDate(ms);
  return '${d.month.toString().padLeft(2, '0')}-${d.day.toString().padLeft(2, '0')}';
}

/// Every observation, the section at the foot of History: folded by default.
/// Open, it shows timeline charts (every hour) then the raw rows for the
/// selected hours, newest first. Returned as slivers so long lists stay lazy.
List<Widget> obsSlivers(BuildContext context, {required bool open, required VoidCallback onToggle, Key? anchor}) {
  final t = Tok.of(context);
  final r = app.raw;
  Widget section(String? meta, Widget child) => KeyedSubtree(
        key: anchor,
        child: Panel(
          title: 'Every observation',
          meta: meta,
          headerKey: const Key('obs-toggle'),
          onHeaderTap: onToggle,
          trailing: Padding(
            padding: const EdgeInsets.only(right: 6),
            child: Icon(open ? Icons.expand_less : Icons.expand_more, size: 20, color: t.fg),
          ),
          child: child,
        ),
      );
  if (!open) return [SliverToBoxAdapter(child: section('not averaged', const SizedBox.shrink()))];
  if (r == null) {
    final failed = app.rawError != null && !app.rawLoading;
    return [
      SliverToBoxAdapter(
        child: section(
          failed ? app.zoneAbbr : 'loading · ${app.zoneAbbr}',
          // While loading, hold a screen of room for the charts, so opening can scroll
          // the header to the top and the page doesn't jump when they arrive.
          ConstrainedBox(
            constraints: BoxConstraints(minHeight: failed ? 0 : MediaQuery.sizeOf(context).height),
            child: Padding(
              padding: const EdgeInsets.all(12),
              child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
                Empty(failed ? app.rawError! : 'loading every observation…'),
                if (failed) Tog('Try again', key: const Key('raw-retry'), onTap: () => app.ensureRaw(retry: true)),
              ]),
            ),
          ),
        ),
      ),
    ];
  }
  final timeline = ((r['timeline'] as List?) ?? const []).cast<Json>();
  final rows = ((r['rows'] as List?) ?? const []).cast<Json>().reversed.toList();
  final primary = r['primary_ident'];
  double x(Json o) => wallMs(o['time_pt'] as String);
  final ticks = <double>[];
  if (timeline.isNotEmpty) {
    final a = x(timeline.first), b = x(timeline.last);
    final span = (b - a) / 86400000;
    final step = span <= 8 ? 1 : (span <= 21 ? 3 : (span <= 60 ? 7 : 14));
    for (var d = (a / 86400000).ceil() * 86400000.0; d <= b; d += step * 86400000.0) {
      ticks.add(d);
    }
  }
  LineChart chart(String title, String unit, List<ChartSeries> s) =>
      LineChart(title: title, unit: unit, series: s, xLabel: _day, xTicks: ticks, gapX: 3 * 3600000, height: 150);

  final head = section(
    '${r['returned'] == r['total_obs'] ? '${r['total_obs']} reports' : '${r['returned']} of ${r['total_obs']}'} · ${app.zoneAbbr}',
    Padding(
      padding: const EdgeInsets.all(12),
      child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        Sub('Charts: all hours, ${r['timeline_from']} → ${r['timeline_to']}'
            // The charts stop at the newest N days even when the window is longer.
            '${r['timeline_truncated'] == true || app.windowDays > ((r['timeline_max_days'] as num?) ?? 90) ? ' (capped at ${r['timeline_max_days'] ?? 90} days)' : ''}'
            '. List: your measured hours, newest first.'),
        const SizedBox(height: 10),
        chart('Wind speed', 'kt', [
          ChartSeries('wind', [for (final o in timeline) Offset(x(o), num_(o['wind_speed']) ?? double.nan)]),
          ChartSeries('gust', [for (final o in timeline) if (o['gust'] != null) Offset(x(o), num_(o['gust'])!)], dash: [4, 3]),
        ]),
        if (r['no_runway_data'] != true) ...[
          const SizedBox(height: 16),
          chart('Crosswind', 'kt', [
            ChartSeries('xwind', [for (final o in timeline) Offset(x(o), num_(o['xwind']) ?? double.nan)],
                dots: true,
                dotColors: [
                  for (final o in timeline)
                    o['active_end_ident'] != null && o['active_end_ident'] != primary ? t.accent : null
                ]),
          ]),
        ],
        const SizedBox(height: 16),
        chart('Ceiling', 'ft', [
          ChartSeries('ceiling', [for (final o in timeline) Offset(x(o), num_(o['ceiling']) ?? double.nan)], dots: true),
        ]),
        if (r['truncated'] == true)
          Padding(padding: const EdgeInsets.only(top: 10), child: Sub('Showing the newest ${r['limit']} observations.')),
      ]),
    ),
  );
  final tableHead = Container(
    margin: const EdgeInsets.only(top: 14),
    padding: const EdgeInsets.symmetric(vertical: 7, horizontal: 12),
    decoration: BoxDecoration(color: t.hover, border: Border.all(color: t.line)),
    child: Row(children: [
      Expanded(flex: 8, child: Sub('time ${app.zoneAbbr}')),
      const SizedBox(width: 6),
      const Expanded(flex: 8, child: Sub('wind')),
      const Expanded(flex: 3, child: Sub('vis')),
      const Expanded(flex: 4, child: Sub('ceil')),
      const Expanded(flex: 4, child: Sub('xw')),
    ]),
  );

  return [
    SliverToBoxAdapter(child: head),
    SliverToBoxAdapter(child: tableHead),
    SliverList.builder(
      itemCount: rows.length,
      itemBuilder: (context, i) {
        final o = rows[i];
        final wind = o['is_vrb'] == true ? 'VRB' : (o['wind_dir'] == null ? '–' : '${pad3(o['wind_dir'])}°');
        final gust = o['gust'] == null ? '' : ' G${fmtInt(o['gust'])}';
        final color = o['breached'] == true ? t.accent : null;
        return Container(
          padding: const EdgeInsets.symmetric(vertical: 7, horizontal: 12),
          decoration: BoxDecoration(
            border: Border(
              left: BorderSide(color: t.line),
              right: BorderSide(color: t.line),
              bottom: BorderSide(color: t.line.withValues(alpha: i == rows.length - 1 ? 1 : .25)),
            ),
          ),
          child: Row(children: [
            Expanded(flex: 8, child: Num('${o['time_pt']}'.substring(5), left: true, color: color)),
            const SizedBox(width: 6),
            Expanded(flex: 8, child: Num('$wind ${fmtInt(o['wind_speed'])}$gust', left: true, color: color)),
            Expanded(flex: 3, child: Num(fmt(o['vis'], 0), left: true, color: color)),
            Expanded(flex: 4, child: Num(o['ceiling'] == null ? '–' : compact(o['ceiling'] as num), left: true, color: color)),
            Expanded(flex: 4, child: Num(fmt(o['xwind']), left: true, color: color)),
          ]),
        );
      },
    ),
  ];
}
