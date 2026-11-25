import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter_timezone/flutter_timezone.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'api.dart';

const pacific = 'America/Los_Angeles';
const commonZones = [
  ('America/Los_Angeles', 'Pacific'), ('America/Denver', 'Mountain'),
  ('America/Phoenix', 'Arizona'), ('America/Chicago', 'Central'),
  ('America/New_York', 'Eastern'), ('America/Anchorage', 'Alaska'),
  ('Pacific/Honolulu', 'Hawaii'), ('UTC', 'UTC / Zulu'),
];

enum Window { d30, d90, cached, custom }

String ymd(DateTime d) =>
    '${d.year}-${d.month.toString().padLeft(2, '0')}-${d.day.toString().padLeft(2, '0')}';

/// Server wall-clock strings ("2026-10-26T11:00" or "2026-10-26 11:00") are
/// already in the chosen zone; charts plot them as-is (wall-clock time).
double wallMs(String s) => DateTime.parse('${s.trim().substring(0, 16)}Z')
    .millisecondsSinceEpoch
    .toDouble();

class AppState extends ChangeNotifier {
  SharedPreferences? _prefs;
  String deviceTz = pacific;
  String tzSetting = 'auto';
  String get tz => tzSetting == 'auto' ? deviceTz : tzSetting;

  String airport = '';
  Set<int> hours = {for (var h = 10; h <= 18; h++) h};
  String runway = 'auto'; // 'auto' or a runway end ident of the typed airport
  Window window = Window.d30;
  DateTime? customFrom, customTo;
  double visMin = 5, ceilMin = 8000, xwindMax = 12;

  Json? info; // the typed airport
  Json? weatherInfo; // station the weather comes from (may be a substitute)
  Json? stats, raw, forecast;
  bool loading = false, rawLoading = false, forecastLoading = false;
  String? error, rawError, forecastError;
  String? from, to; // resolved window, yyyy-mm-dd
  String? target; // forecast table time, wall clock in tz
  int forecastDays = 5; // chart span, kept across tab switches
  int forecastOffset = 0; // days from the first forecast hour, paged by the span
  bool wantRaw = false; // Every observation is open on screen: load raw after each run
  bool historyLoading = false;
  String? historyNote; // progress or error from the last history refresh
  int _seq = 0;
  String? _lastCode;
  String? _lastZone;
  Map<String, String>? _query;

  String? get substitute {
    final w = weatherInfo?['airport'], t = info?['airport'];
    return w != null && t != null && w != t ? w as String : null;
  }

  List<Json> get runways =>
      ((info?['runways'] as List?) ?? const []).cast<Json>();

  /// "PDT"-style label taken from the server's own formatted times.
  /// Short zone label: generic for common zones ("PT"), else the server's own
  /// abbreviation from a formatted time ("IST"), else the city.
  String get zoneAbbr {
    const generic = {
      'America/Los_Angeles': 'PT', 'America/Denver': 'MT', 'America/Phoenix': 'MST',
      'America/Chicago': 'CT', 'America/New_York': 'ET', 'America/Anchorage': 'AKT',
      'Pacific/Honolulu': 'HST', 'UTC': 'UTC', 'Etc/UTC': 'UTC',
    };
    if (generic[tz] != null) return generic[tz]!;
    final s = (forecast?['target_label'] ?? weatherInfo?['last_obs_pt']) as String?;
    final parts = s?.split(' ') ?? const [];
    return parts.length >= 3 ? parts.last : tz.split('/').last.replaceAll('_', ' ');
  }

  String get zoneName => tz == 'UTC' ? 'UTC' : '${tz.split('/').last.replaceAll('_', ' ')} time';

  Future<void> init() async {
    final prefs = _prefs = await SharedPreferences.getInstance();
    try {
      deviceTz = (await FlutterTimezone.getLocalTimezone()).identifier;
    } catch (_) {}
    if (deviceTz.isEmpty) deviceTz = pacific;
    tzSetting = prefs.getString('tz') ?? 'auto';
    airport = prefs.getString('airport') ?? '';
    final h = prefs.getStringList('hours');
    if (h != null) hours = h.map(int.parse).toSet();
    visMin = prefs.getDouble('visMin') ?? visMin;
    ceilMin = prefs.getDouble('ceilMin') ?? ceilMin;
    xwindMax = prefs.getDouble('xwindMax') ?? xwindMax;
    window = Window.values.asNameMap()[prefs.getString('window')] ?? Window.d30;
    notifyListeners();
    if (airport.isNotEmpty) unawaited(run());
  }

  void _save() {
    final p = _prefs;
    if (p == null) return;
    p.setString('airport', airport);
    p.setStringList('hours', [for (final h in hours) '$h']);
    p.setDouble('visMin', visMin);
    p.setDouble('ceilMin', ceilMin);
    p.setDouble('xwindMax', xwindMax);
    if (window != Window.custom) p.setString('window', window.name);
  }

  void update(void Function() change, {bool rerun = false}) {
    change();
    notifyListeners();
    if (rerun && (loading || forecastLoading || stats != null || forecast != null || error != null)) {
      unawaited(run());
    }
  }

  Future<void> setTz(String value) async {
    tzSetting = value;
    update(() {
      target = null;
      forecastOffset = 0;
    }, rerun: true);
    await _prefs?.setString('tz', value);
  }

  Future<List<String>> zones() async {
    try {
      final all = await FlutterTimezone.getAvailableTimezones();
      // Region/City names only; legacy ids like "ACT" aren't valid server zones.
      return [for (final z in all) if (z.identifier.contains('/')) z.identifier]..sort();
    } catch (_) {
      return [for (final z in commonZones) z.$1];
    }
  }

  Future<void> run() async {
    final code = airport.trim().toUpperCase();
    if (code.isEmpty) return;
    airport = code;
    final airportChanged = code != _lastCode;
    if (airportChanged || tz != _lastZone) {
      target = null;
      forecastOffset = 0;
      if (airportChanged) runway = 'auto';
      info = weatherInfo = stats = raw = forecast = null;
      from = to = null;
    }
    _lastCode = code;
    _lastZone = tz;
    final seq = ++_seq;
    loading = true;
    error = null;
    raw = null;
    rawError = null;
    rawLoading = false; // any in-flight raw reply is dropped by _seq
    _query = null;
    notifyListeners();
    _save();
    unawaited(_loadForecast(code, seq));
    try {
      final i = await getJson('/api/info', {'airport': code, 'tz': tz});
      var src = i;
      final nearest = (i['nearest'] as List?) ?? const [];
      if ((i['obs_count'] ?? 0) == 0 && nearest.isNotEmpty) {
        final n = await getJson('/api/info', {'airport': '${(nearest.first as Json)['ident']}', 'tz': tz});
        if ((n['obs_count'] ?? 0) > 0) src = n;
      }
      if (seq != _seq) return;
      if (i['name'] == null && (i['obs_count'] ?? 0) == 0 && nearest.isEmpty) {
        throw ApiError('No airport matches “$code”. Try the FAA (MYF) or ICAO (KMYF) code.');
      }
      final rws = ((i['runways'] as List?) ?? const []).cast<Json>();
      if (runway != 'auto' && !rws.any((r) => r['ident'] == runway)) runway = 'auto';
      final (f, t) = _window(src);
      final q = _buildQuery(src['airport'] as String, i['airport'] as String, rws, f, t);
      final s = await getJson('/api/stats', q);
      if (seq != _seq) return;
      // Commit together so facts, dates and numbers always match.
      info = i;
      weatherInfo = src;
      from = f;
      to = t;
      _query = q;
      stats = s;
    } catch (e) {
      if (seq == _seq) {
        error = '$e';
        // A step shows its window before the reply; on failure go back to the one on screen.
        final shown = stats;
        if (shown != null && shown['date_from'] != null) {
          from = shown['date_from'] as String;
          to = shown['date_to'] as String?;
        }
      }
    }
    if (seq == _seq) {
      loading = false;
      notifyListeners();
      if (wantRaw && stats != null) unawaited(ensureRaw());
    }
  }

  /// Days in the resolved history window (inclusive), 0 if unknown.
  int get windowDays {
    final f = from == null ? null : DateTime.tryParse(from!), t = to == null ? null : DateTime.tryParse(to!);
    return f == null || t == null || t.isBefore(f)
        ? 0
        : DateTime.utc(t.year, t.month, t.day).difference(DateTime.utc(f.year, f.month, f.day)).inDays + 1;
  }

  DateTime? get _lastObsDay {
    final s = weatherInfo?['last_obs_pt'] as String?;
    final d = s == null ? null : DateTime.tryParse(s.substring(0, 10));
    return d == null ? null : DateTime.utc(d.year, d.month, d.day);
  }

  DateTime? get _firstObsDay {
    final s = weatherInfo?['first_obs_pt'] as String?;
    final d = s == null ? null : DateTime.tryParse(s.substring(0, 10));
    return d == null ? null : DateTime.utc(d.year, d.month, d.day);
  }

  /// Earlier windows exist only while this one starts after the first cached observation.
  bool get canStepBack {
    final first = _firstObsDay, f = from == null ? null : DateTime.tryParse(from!);
    return windowDays > 0 && f != null && (first == null || DateTime.utc(f.year, f.month, f.day).isAfter(first));
  }

  /// A preset: show its dates at once (the reply may take a while), then query.
  void setWindow(Window w) => update(() {
        window = w;
        final src = weatherInfo;
        if (src != null) {
          final (f, t) = _window(src);
          from = f;
          to = t;
        }
      }, rerun: true);

  bool get canStepForward {
    final last = _lastObsDay, t = to == null ? null : DateTime.tryParse(to!);
    return windowDays > 0 && last != null && t != null && DateTime.utc(t.year, t.month, t.day).isBefore(last);
  }

  /// Slides the history window back or forward by its own length, never
  /// past the latest cached observation.
  void stepWindow(int dir) {
    final days = windowDays;
    if (days == 0) return;
    final t = DateTime.parse(to!);
    var end = DateTime.utc(t.year, t.month, t.day + dir * days);
    final last = _lastObsDay, first = _firstObsDay;
    if (last != null && end.isAfter(last)) end = last;
    // Never start before the first cached observation; keep the length.
    if (first != null && DateTime.utc(end.year, end.month, end.day - days + 1).isBefore(first)) {
      end = DateTime.utc(first.year, first.month, first.day + days - 1);
    }
    update(() {
      customTo = end;
      customFrom = DateTime.utc(end.year, end.month, end.day - days + 1);
      // Landing back on the latest 30 or 90 days lights that preset again.
      window = last != null && end == last && (days == 30 || days == 90)
          ? (days == 30 ? Window.d30 : Window.d90)
          : Window.custom;
      // Show the new window now so quick taps keep stepping from it.
      from = ymd(customFrom!);
      to = ymd(end);
    }, rerun: true);
  }

  /// Window ends at the latest cached observation (already a date in tz).
  (String?, String?) _window(Json src) {
    final last = src['last_obs_pt'] as String?;
    final now = DateTime.now();
    final end = last != null ? DateTime.parse(last.substring(0, 10)) : DateTime(now.year, now.month, now.day);
    return switch (window) {
      Window.d30 || Window.d90 =>
        (ymd(DateTime(end.year, end.month, end.day - (window == Window.d30 ? 29 : 89))), ymd(end)),
      Window.cached => (src['recent_contiguous_from'] as String? ?? from, src['recent_contiguous_to'] as String? ?? to),
      Window.custom => (customFrom == null ? from : ymd(customFrom!), customTo == null ? to : ymd(customTo!)),
    };
  }

  Map<String, String> _buildQuery(String weather, String typed, List<Json> rws, String? from, String? to) {
    final rw = rws.where((r) => r['ident'] == runway).firstOrNull;
    return {
      'airport': weather,
      'runway_heading': rw?['heading_deg'] == null ? 'auto' : '${rw!['heading_deg']}',
      if (rw?['heading_deg'] != null) 'runway_ident': runway,
      if (weather != typed) 'runways_airport': typed,
      'hours': (hours.toList()..sort()).join(','),
      'date_from': ?from,
      'date_to': ?to,
      'vis_min_sm': '$visMin',
      'ceiling_min_ft': '$ceilMin',
      'xwind_max_kt': '$xwindMax',
      'tz': tz,
    };
  }

  Future<void> _loadForecast(String code, int seq) async {
    forecastLoading = true;
    forecastError = null;
    notifyListeners();
    try {
      final f = await getJson('/api/forecast', {'airport': code, 'tz': tz});
      if (seq == _seq) forecast = f;
    } catch (e) {
      if (seq == _seq) {
        forecastError = '$e'.contains('not found')
            ? 'No airport matches “$code”. Try the FAA (MYF) or ICAO (KMYF) code.'
            : '$e';
      }
    }
    if (seq == _seq) {
      forecastLoading = false;
      notifyListeners();
    }
  }

  /// [retry] clears a failed load (opening the tab, or Try again).
  Future<void> ensureRaw({bool retry = false}) async {
    final q = _query;
    if (retry && !rawLoading) rawError = null;
    if (raw != null || rawLoading || rawError != null || q == null || loading) return;
    final seq = _seq;
    rawLoading = true;
    notifyListeners();
    try {
      final r = await getJson('/api/raw', q);
      if (seq == _seq) raw = r;
    } catch (e) {
      if (seq == _seq) rawError = '$e';
    }
    if (seq == _seq) {
      rawLoading = false;
      notifyListeners();
    }
  }

  /// Pulls up to a year of METAR history for the station in view, then
  /// re-runs. Only offered when the server doesn't need reCAPTCHA.
  Future<void> refreshHistory() async {
    final station = '${weatherInfo?['airport'] ?? airport}';
    if (station.isEmpty || historyLoading) return;
    historyLoading = true;
    historyNote = 'starting…';
    notifyListeners();
    try {
      await for (final ev in postLines('/api/fetch_stream', {'tz': tz},
          {'airport': station, 'mode': 'recent', 'days': 365})) {
        switch (ev['type']) {
          case 'month_start':
            historyNote = 'pulling ${ev['year']}-${'${ev['month']}'.padLeft(2, '0')} · '
                '${(ev['index'] as num) + 1}/${ev['total']}';
          case 'error':
            throw ApiError('${ev['message']}');
          case 'done':
            historyNote = null;
        }
        notifyListeners();
      }
    } catch (e) {
      historyNote = '$e';
    }
    historyLoading = false;
    notifyListeners();
    if (historyNote == null) await run();
  }

  /// Nearest forecast point to the table time, within 1.5 h (as the web does).
  Json? pointAt(Json model) {
    final t = target ?? forecast?['target_pt'] as String?;
    if (t == null) return null;
    final want = wallMs(t);
    Json? best;
    var bestD = double.infinity;
    for (final p in ((model['points'] as List?) ?? const []).cast<Json>()) {
      final d = (wallMs(p['time_pt'] as String) - want).abs();
      if (d < bestD) {
        bestD = d;
        best = p;
      }
    }
    return bestD <= 1.51 * 3600000 ? best : null;
  }
}

final app = AppState();
