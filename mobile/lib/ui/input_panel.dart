import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../api.dart';
import '../state.dart';
import 'kit.dart';

/// INPUT INFO: airport, runway, hours, window, minimums, plus the settings
/// tray behind the cog. On phones it folds to a one-line summary after a run.
class InputPanel extends StatefulWidget {
  const InputPanel({super.key, required this.foldable});
  final bool foldable;
  @override
  State<InputPanel> createState() => _InputPanelState();
}

class _InputPanelState extends State<InputPanel> with AutomaticKeepAliveClientMixin {
  @override
  bool get wantKeepAlive => true;

  bool _settings = false, _open = true;
  bool? _openBeforeSplit;
  late final _airport = TextEditingController(text: app.airport);
  late final _vis = TextEditingController(text: compact(app.visMin));
  late final _ceil = TextEditingController(text: compact(app.ceilMin));
  late final _xw = TextEditingController(text: compact(app.xwindMax));

  @override
  void initState() {
    super.initState();
    _open = !(widget.foldable && app.airport.isNotEmpty);
  }

  @override
  void didUpdateWidget(InputPanel old) {
    super.didUpdateWidget(old);
    // Split layout never folds; going back to one column restores the fold it had.
    if (!widget.foldable && old.foldable) {
      _openBeforeSplit = _open;
      _open = true;
    } else if (widget.foldable && !old.foldable) {
      _open = _openBeforeSplit ?? !(app.airport.isNotEmpty);
    }
  }

  @override
  void dispose() {
    for (final c in [_airport, _vis, _ceil, _xw]) {
      c.dispose();
    }
    super.dispose();
  }

  void _run() {
    FocusScope.of(context).unfocus();
    app.airport = _airport.text.trim().toUpperCase();
    _airport.text = app.airport;
    app.visMin = double.tryParse(_vis.text) ?? 5;
    app.ceilMin = double.tryParse(_ceil.text) ?? 8000;
    app.xwindMax = double.tryParse(_xw.text) ?? 12;
    if (app.airport.isEmpty || app.hours.isEmpty) {
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
          content: Text(app.airport.isEmpty ? 'Enter an airport code.' : 'Pick at least one hour.')));
      return;
    }
    app.run();
    if (widget.foldable) setState(() => _open = false);
  }

  @override
  Widget build(BuildContext context) {
    super.build(context);
    final t = Tok.of(context);
    return ListenableBuilder(
      listenable: app,
      builder: (context, _) => Panel(
        title: 'Input info',
        trailing: IconButton(
          key: const Key('settings'),
          tooltip: 'Settings',
          visualDensity: VisualDensity.compact,
          onPressed: () => setState(() => _settings = !_settings),
          icon: AnimatedRotation(
            turns: _settings ? .25 : 0,
            duration: const Duration(milliseconds: 320),
            curve: Curves.easeOutCubic,
            child: Icon(Icons.settings_outlined, size: 18, color: t.fg),
          ),
        ),
        child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
          AnimatedSize(
            duration: const Duration(milliseconds: 240),
            curve: Curves.easeOutCubic,
            alignment: Alignment.topCenter,
            child: _settings ? _tray(t) : const SizedBox(width: double.infinity),
          ),
          AnimatedSize(
            duration: const Duration(milliseconds: 240),
            curve: Curves.easeOutCubic,
            alignment: Alignment.topCenter,
            child: _open ? _form(t) : _summary(t),
          ),
        ]),
      ),
    );
  }

  Widget _tray(Tok t) => Container(
        key: const Key('settings-tray'),
        decoration: BoxDecoration(color: t.hover, border: Border(bottom: BorderSide(color: t.line))),
        padding: const EdgeInsets.all(12),
        child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
          const Label('Time zone'),
          const SizedBox(height: 6),
          Material(
            color: t.bg,
            shape: Border.all(color: t.line),
            child: InkWell(
              key: const Key('tz-button'),
              onTap: () => showZonePicker(context),
              child: Padding(
                padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 12),
                child: Row(children: [
                  Expanded(
                      child: Text(app.tzSetting == 'auto' ? 'Auto · ${app.deviceTz}' : app.tzSetting,
                          overflow: TextOverflow.ellipsis)),
                  Icon(Icons.expand_more, size: 18, color: t.fg),
                ]),
              ),
            ),
          ),
          const SizedBox(height: 8),
          Sub('Hours, dates and chart times use ${app.zoneName}. Auto follows this device.'),
        ]),
      );

  Widget _summary(Tok t) {
    final h = app.hours.toList()..sort();
    final contiguous = h.isNotEmpty && h.last - h.first == h.length - 1;
    final hours = h.isEmpty
        ? 'no hours'
        : h.length == 24 ? 'all hours' : contiguous ? '${h.first}–${h.last}' : '${h.length} hrs';
    return InkWell(
      key: const Key('edit-input'),
      onTap: () => setState(() => _open = true),
      child: Padding(
        padding: const EdgeInsets.fromLTRB(12, 12, 12, 12),
        child: Row(children: [
          Expanded(
            child: Text('${app.airport} · $hours ${app.zoneAbbr} · ${dateSpan(app.from, app.to, sep: '–')}',
                style: const TextStyle(fontSize: 12), maxLines: 2),
          ),
          const SizedBox(width: 8),
          Label('Edit', color: t.brand, bold: true),
        ]),
      ),
    );
  }

  Widget _form(Tok t) {
    final cached = (app.weatherInfo?['obs_count'] ?? 0) as num > 0;
    return Padding(
      padding: const EdgeInsets.all(12),
      child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        const Label('Airport (FAA / ICAO)'),
        const SizedBox(height: 6),
        TextField(
          key: const Key('airport'),
          controller: _airport,
          textCapitalization: TextCapitalization.characters,
          inputFormatters: [
            TextInputFormatter.withFunction((_, v) => v.copyWith(text: v.text.toUpperCase())),
          ],
          autocorrect: false,
          enableSuggestions: false,
          textInputAction: TextInputAction.search,
          decoration: const InputDecoration(hintText: 'e.g. KMYF'),
          onSubmitted: (_) => _run(),
        ),
        const SizedBox(height: 14),
        const Label('Runway'),
        const SizedBox(height: 6),
        InputDecorator(
          decoration: const InputDecoration(contentPadding: EdgeInsets.symmetric(horizontal: 9, vertical: 2)),
          child: DropdownButtonHideUnderline(
            child: DropdownButton<String>(
              isExpanded: true,
              isDense: true,
              value: app.runway,
              style: TextStyle(fontFamily: mono, fontSize: 13, color: t.fg),
              dropdownColor: t.bg,
              items: [
                const DropdownMenuItem(value: 'auto', child: Text('Auto by wind')),
                for (final r in app.runways)
                  if (r['heading_deg'] != null)
                    DropdownMenuItem(value: r['ident'] as String, child: Text(runwayLabel(r))),
              ],
              onChanged: (v) => app.update(() => app.runway = v ?? 'auto', rerun: true),
            ),
          ),
        ),
        const SizedBox(height: 14),
        const Label('Hours measured'),
        const SizedBox(height: 6),
        LayoutBuilder(builder: (context, box) {
          const gap = 6.0;
          final w = (box.maxWidth - gap * 5) / 6;
          return Wrap(spacing: gap, runSpacing: gap, children: [
            for (var h = 0; h < 24; h++)
              SizedBox(
                width: w,
                child: Tog(h.toString().padLeft(2, '0'),
                    height: 36,
                    on: app.hours.contains(h),
                    semantics: 'hour $h',
                    onTap: () => app.update(() => app.hours.contains(h) ? app.hours.remove(h) : app.hours.add(h))),
              ),
          ]);
        }),
        const SizedBox(height: 6),
        Row(children: [
          Expanded(child: Tog('All', height: 34, onTap: () => app.update(() => app.hours = {for (var h = 0; h < 24; h++) h}))),
          const SizedBox(width: 6),
          Expanded(child: Tog('10–18', height: 34, onTap: () => app.update(() => app.hours = {for (var h = 10; h <= 18; h++) h}))),
          const SizedBox(width: 6),
          Expanded(child: Tog('Clear', height: 34, onTap: () => app.update(() => app.hours = {}))),
        ]),
        const SizedBox(height: 14),
        const Label('Personal minimums'),
        const SizedBox(height: 6),
        Row(crossAxisAlignment: CrossAxisAlignment.end, children: [
          Expanded(child: _num('vis < (sm)', _vis)),
          const SizedBox(width: 8),
          Expanded(child: _num('ceiling < (ft)', _ceil)),
          const SizedBox(width: 8),
          Expanded(child: _num('xwind max (kt)', _xw)),
        ]),
        const SizedBox(height: 16),
        PrimaryButton(cached ? 'Update' : 'Run query', key: const Key('run'), busy: app.loading, onTap: _run),
        if (app.weatherInfo != null) ...[
          const SizedBox(height: 16),
          const Label('METAR history'),
          const SizedBox(height: 6),
          _cacheBox(t),
          if (canRefreshHistory) ...[
            const SizedBox(height: 8),
            Tog(app.historyLoading ? 'Pulling history…' : 'Refresh / get more history',
                key: const Key('refresh-history'), height: 40, onTap: app.historyLoading ? null : app.refreshHistory),
            if (app.historyNote != null) ...[const SizedBox(height: 6), Sub(app.historyNote!)],
          ],
        ],
      ]),
    );
  }

  Widget _num(String label, TextEditingController c) => Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        Sub(label),
        const SizedBox(height: 4),
        TextField(controller: c, keyboardType: const TextInputType.numberWithOptions(decimal: true)),
      ]);

  Widget _cacheBox(Tok t) {
    final w = app.weatherInfo!;
    Widget row(String k, String v) => Padding(
          padding: const EdgeInsets.symmetric(vertical: 2),
          child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
            SizedBox(width: 64, child: Sub(k)),
            Expanded(child: Text(v, style: const TextStyle(fontSize: 12))),
          ]),
        );
    return DecoratedBox(
      decoration: BoxDecoration(border: Border.all(color: t.line)),
      child: Padding(
        padding: const EdgeInsets.all(10),
        child: Column(children: [
          row('airport', '${w['airport']}${app.substitute != null ? ' (nearest reporting station)' : ''}'),
          row('obs', '${w['obs_count'] ?? 0}'),
          row('range', w['first_obs_pt'] == null ? '—' : '${w['first_obs_pt']} → ${w['last_obs_pt']}'),
        ]),
      ),
    );
  }
}

Future<void> showZonePicker(BuildContext context) => showModalBottomSheet(
      context: context,
      isScrollControlled: true,
      useSafeArea: true,
      builder: (_) => const _ZonePicker(),
    );

class _ZonePicker extends StatefulWidget {
  const _ZonePicker();
  @override
  State<_ZonePicker> createState() => _ZonePickerState();
}

class _ZonePickerState extends State<_ZonePicker> {
  List<String> _all = const [];
  String _q = '';

  @override
  void initState() {
    super.initState();
    app.zones().then((z) => mounted ? setState(() => _all = z) : null);
  }

  @override
  Widget build(BuildContext context) {
    final t = Tok.of(context);
    final q = _q.toLowerCase().replaceAll(' ', '_');
    bool hit(String s) => q.isEmpty || s.toLowerCase().contains(q);
    Widget item(String value, String text, {String? sub, String group = ''}) => ListTile(
          key: Key('tz-$group$value'),
          dense: true,
          title: Text(text, style: const TextStyle(fontSize: 13)),
          subtitle: sub == null ? null : Text(sub, style: TextStyle(fontSize: 11, color: t.muted)),
          trailing: app.tzSetting == value ? Icon(Icons.check, size: 18, color: t.fg) : null,
          onTap: () {
            app.setTz(value);
            Navigator.pop(context);
          },
        );
    return Column(children: [
      Container(
        padding: const EdgeInsets.fromLTRB(16, 8, 4, 8),
        decoration: BoxDecoration(border: Border(bottom: BorderSide(color: t.line))),
        child: Row(children: [
          const Expanded(child: Label('Time zone', bold: true)),
          IconButton(tooltip: 'Close', onPressed: () => Navigator.pop(context), icon: const Icon(Icons.close, size: 20)),
        ]),
      ),
      Padding(
        padding: const EdgeInsets.all(12),
        child: TextField(
          key: const Key('tz-search'),
          autocorrect: false,
          decoration: const InputDecoration(hintText: 'Search zones', prefixIcon: Icon(Icons.search, size: 18)),
          onChanged: (v) => setState(() => _q = v.trim()),
        ),
      ),
      Expanded(
        child: ListView(children: [
          if (hit('auto') || hit(app.deviceTz)) item('auto', 'Auto', sub: 'This device · ${app.deviceTz}'),
          for (final (id, name) in commonZones)
            if (hit(id) || hit(name)) item(id, name, sub: id),
          if (_all.isNotEmpty) Padding(padding: const EdgeInsets.fromLTRB(16, 14, 16, 6), child: Sub('All zones')),
          for (final z in _all)
            if (hit(z)) item(z, z.replaceAll('_', ' '), group: 'all-'),
        ]),
      ),
    ]);
  }
}
