import 'package:flutter/material.dart';

import 'state.dart';
import 'ui/forecast_view.dart';
import 'ui/history_view.dart';
import 'ui/input_panel.dart';
import 'ui/kit.dart';
import 'ui/obs_view.dart';

const appVersion = '1.0.3';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await app.init(); // saved settings must be in place before the form builds
  runApp(const WindcheckApp());
}

class WindcheckApp extends StatelessWidget {
  const WindcheckApp({super.key});
  @override
  Widget build(BuildContext context) => MaterialApp(
        title: 'Windcheck',
        debugShowCheckedModeBanner: false,
        theme: buildTheme(Brightness.light),
        darkTheme: buildTheme(Brightness.dark),
        home: const Home(),
      );
}

enum Tab { forecast, history }

class Home extends StatefulWidget {
  const Home({super.key});
  @override
  State<Home> createState() => _HomeState();
}

class _HomeState extends State<Home> {
  Tab _tab = Tab.forecast;
  bool _obsOpen = false; // Every observation, the section at the foot of History
  final _obsAnchor = GlobalKey();
  // Keep panel and forecast state when the layout flips at the breakpoint.
  final _inputKey = GlobalKey(), _forecastKey = GlobalKey();

  void _select(Tab t) {
    if (t == _tab) return;
    setState(() => _tab = t);
    _syncRaw();
  }

  void _toggleObs() {
    setState(() => _obsOpen = !_obsOpen);
    _syncRaw();
    // Opening it brings its header to the top, so what it shows is on screen.
    if (_obsOpen) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        final c = _obsAnchor.currentContext;
        if (c != null && c.mounted) {
          Scrollable.ensureVisible(c, duration: const Duration(milliseconds: 250), curve: Curves.easeOut);
        }
      });
    }
  }

  // Load every observation only while its section is open on screen; showing it again retries a failure.
  void _syncRaw() {
    app.wantRaw = _tab == Tab.history && _obsOpen;
    if (app.wantRaw) app.ensureRaw(retry: true);
  }

  @override
  Widget build(BuildContext context) {
    final t = Tok.of(context);
    final size = MediaQuery.sizeOf(context);
    final width = size.width;
    // Tablets, and phones in landscape: inputs beside results.
    final split = width >= 900 || (width >= 700 && size.height < 500);
    final panelWidth = width >= 900 ? (width * .28).clamp(320.0, 400.0) : 300.0;
    final pad = width < 380 ? 12.0 : 16.0;
    // System Back on History goes to Forecast; it leaves the app only from Forecast.
    return PopScope(
      canPop: _tab == Tab.forecast,
      onPopInvokedWithResult: (didPop, _) {
        if (!didPop) _select(Tab.forecast);
      },
      child: Scaffold(
        body: SafeArea(
          bottom: false,
          child: Column(children: [
            _header(t, pad),
            Expanded(
              child: split
                  ? Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
                      SizedBox(
                        width: panelWidth,
                        child: SingleChildScrollView(
                            padding: EdgeInsets.fromLTRB(pad, pad, 0, pad + 24),
                            child: InputPanel(key: _inputKey, foldable: false)),
                      ),
                      Expanded(child: _results(t, pad, withInputs: false)),
                    ])
                  : _results(t, pad, withInputs: true),
            ),
          ]),
        ),
      ),
    );
  }

  Widget _header(Tok t, double pad) => Container(
        padding: EdgeInsets.fromLTRB(pad, 6, pad - 8, 6),
        decoration: BoxDecoration(border: Border(bottom: BorderSide(color: t.line))),
        child: Row(children: [
          Text('WINDCHECK',
              style: TextStyle(color: t.brand, fontWeight: FontWeight.w700, fontSize: 14, letterSpacing: 2.5)),
          const Spacer(),
          if (MediaQuery.sizeOf(context).width >= 380) const Label('forecast + history', size: 10),
          IconButton(
            tooltip: 'About',
            onPressed: () => _about(context),
            icon: Icon(Icons.info_outline, size: 18, color: t.brand),
          ),
        ]),
      );

  Widget _results(Tok t, double pad, {required bool withInputs}) {
    return ListenableBuilder(
      listenable: app,
      builder: (context, _) => RefreshIndicator(
        color: t.fg,
        onRefresh: app.run,
        child: CustomScrollView(
          keyboardDismissBehavior: ScrollViewKeyboardDismissBehavior.onDrag,
          slivers: [
            SliverPadding(
              padding: EdgeInsets.fromLTRB(pad, pad, pad, 0),
              sliver: SliverList.list(children: [
                if (withInputs) ...[InputPanel(key: _inputKey, foldable: true), const SizedBox(height: 14)],
                if (app.error != null && _tab != Tab.history) ...[
                  Text(app.error!, style: TextStyle(color: t.accent, fontSize: 12)),
                  const SizedBox(height: 10),
                ],
                Segmented<Tab>(
                  value: _tab,
                  items: const [(Tab.forecast, 'Forecast', 1), (Tab.history, 'History', 1)],
                  onChanged: _select,
                ),
                const SizedBox(height: 14),
                if (_tab == Tab.forecast) ForecastView(key: _forecastKey),
                if (_tab == Tab.history) const HistoryView(),
              ]),
            ),
            if (_tab == Tab.history && ((app.stats?['total_obs'] as num?) ?? 0) > 0)
              SliverPadding(
                padding: EdgeInsets.fromLTRB(pad, 14, pad, 0),
                sliver: SliverMainAxisGroup(slivers: obsSlivers(context, open: _obsOpen, onToggle: _toggleObs, anchor: _obsAnchor)),
              ),
            SliverPadding(
              padding: EdgeInsets.fromLTRB(pad, 18, pad, MediaQuery.paddingOf(context).bottom + 24),
              sliver: const SliverToBoxAdapter(
                child: Sub('Planning aid, not an official weather briefing. Verify with official sources before flight.'),
              ),
            ),
          ],
        ),
      ),
    );
  }

  void _about(BuildContext context) => showModalBottomSheet(
        context: context,
        useSafeArea: true,
        builder: (context) => Padding(
          padding: const EdgeInsets.fromLTRB(16, 16, 16, 28),
          child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.start, children: [
            const Label('About Windcheck', bold: true),
            const SizedBox(height: 10),
            const Text(
                'Wind, crosswind, ceiling and visibility history by hour for any airport, '
                'next to TAF, LAMP, NWS, HRRR, NBM, NAM and GFS guidance. Same data as windcheck.net.'),
            const SizedBox(height: 10),
            const Sub('History: METAR archives (Ogimet, Iowa State Mesonet). Runways: OurAirports. '
                'Not an official weather briefing.'),
            const SizedBox(height: 14),
            const Sub('Version $appVersion · built by Dreamers'),
          ]),
        ),
      );
}
