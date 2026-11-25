import 'package:flutter/material.dart';

import '../state.dart';
import 'kit.dart';

/// History range: ‹ 30D 90D ALL CACHED › plus the span itself, which opens
/// the date-range picker. Sits at the top of the History tab.
class HistoryRangeBar extends StatelessWidget {
  const HistoryRangeBar({super.key});

  Future<void> _pick(BuildContext context) async {
    final now = DateTime.now();
    final from = app.from == null ? null : DateTime.parse(app.from!);
    final to = app.to == null ? null : DateTime.parse(app.to!);
    final r = await showDateRangePicker(
      context: context,
      firstDate: DateTime(2000),
      lastDate: now,
      initialDateRange: from == null || to == null || to.isAfter(now) ? null : DateTimeRange(start: from, end: to),
    );
    if (r == null) return;
    app.update(() {
      app.customFrom = r.start;
      app.customTo = r.end;
      app.window = Window.custom;
    }, rerun: true);
  }

  // Listens itself: parents build it as const, which Flutter never rebuilds.
  @override
  Widget build(BuildContext context) => ListenableBuilder(listenable: app, builder: (context, _) => _bar(context));

  Widget _bar(BuildContext context) {
    final t = Tok.of(context);
    final days = app.windowDays;
    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      Row(children: [
        SizedBox(
          width: 44,
          child: Tog('‹', key: const Key('range-prev'), height: 38, size: 20, semantics: 'previous period',
              onTap: app.canStepBack ? () => app.stepWindow(-1) : null),
        ),
        const SizedBox(width: 6),
        Expanded(
          child: Segmented<Window>(
            height: 38,
            value: app.window,
            items: const [(Window.d30, '30d', 2), (Window.d90, '90d', 2), (Window.cached, 'All cached', 4)],
            onChanged: app.setWindow,
          ),
        ),
        const SizedBox(width: 6),
        SizedBox(
          width: 44,
          child: Tog('›', key: const Key('range-next'), height: 38, size: 20, semantics: 'next period',
              onTap: app.canStepForward ? () => app.stepWindow(1) : null),
        ),
      ]),
      const SizedBox(height: 6),
      Material(
        color: t.bg,
        shape: Border.all(color: t.line),
        child: InkWell(
          key: const Key('range-span'),
          onTap: () => _pick(context),
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 10),
            child: Row(children: [
              Expanded(
                child: Text(
                  app.from == null ? 'Pick dates' : '${dateSpan(app.from, app.to)}${days > 0 ? ' · $days days' : ''}',
                  style: const TextStyle(fontSize: 13),
                ),
              ),
              Icon(Icons.calendar_today_outlined, size: 16, color: t.fg),
            ]),
          ),
        ),
      ),
    ]);
  }
}
