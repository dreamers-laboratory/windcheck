import 'dart:math' as math;

import 'package:flutter/material.dart';

import 'kit.dart';

class ChartSeries {
  const ChartSeries(this.label, this.points, {this.dash, this.dots = false, this.dotColors});
  final String label;
  final List<Offset> points; // x = ms or index, y = value (NaN = gap)
  final List<double>? dash;
  final bool dots;
  final List<Color?>? dotColors;
}

/// Minimal SVG-style chart: line series (or bars), dotted grid, compact
/// labels, and a touch readout pinned to the top row. Drag sideways to scrub.
class LineChart extends StatefulWidget {
  const LineChart({
    super.key,
    required this.title,
    required this.series,
    required this.xLabel,
    this.unit = '',
    this.height = 170,
    this.bars = false,
    this.gapX,
    this.xTicks,
    this.tickLabel,
    this.legend = false,
    this.empty = 'no data in this range',
    this.xMin,
    this.xMax,
    this.xTitle,
  });
  final String title, unit, empty;
  final List<ChartSeries> series;
  final String Function(double x) xLabel;
  final double height;
  final bool bars, legend;
  final double? gapX; // break lines across gaps wider than this
  final List<double>? xTicks;
  final String Function(double x)? tickLabel; // axis labels, if shorter than xLabel
  final double? xMin, xMax; // shared span so stacked charts line up
  final String? xTitle; // e.g. "hour (PT)", centred under the ticks

  @override
  State<LineChart> createState() => _LineChartState();
}

class _LineChartState extends State<LineChart> {
  double? _touch; // 0..1 across the plot
  final Set<String> _hidden = {};

  @override
  Widget build(BuildContext context) {
    final t = Tok.of(context);
    final shown = [for (final s in widget.series) if (!_hidden.contains(s.label)) s];
    final hasData = widget.series.any((s) => s.points.any((p) => p.dy.isFinite));
    return Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
      Padding(
        padding: const EdgeInsets.only(bottom: 6),
        child: Sub('${widget.title}${widget.unit.isEmpty ? '' : ' (${widget.unit})'}'),
      ),
      if (widget.legend && widget.series.isNotEmpty)
        Padding(
          padding: const EdgeInsets.only(bottom: 6),
          child: Wrap(spacing: 11, runSpacing: 0, children: [
            for (final s in widget.series)
              InkWell(
                onTap: () => setState(() => _hidden.contains(s.label) ? _hidden.remove(s.label) : _hidden.add(s.label)),
                child: Padding(
                  padding: const EdgeInsets.symmetric(vertical: 11), // ~38dp target
                  child: Row(mainAxisSize: MainAxisSize.min, children: [
                    CustomPaint(size: const Size(18, 8), painter: _KeyPainter(s.dash, _hidden.contains(s.label) ? t.muted.withValues(alpha: .4) : t.fg)),
                    const SizedBox(width: 5),
                    Text(s.label,
                        style: TextStyle(
                            fontSize: 10,
                            color: _hidden.contains(s.label) ? t.muted.withValues(alpha: .5) : t.fg,
                            decoration: _hidden.contains(s.label) ? TextDecoration.lineThrough : null)),
                  ]),
                ),
              ),
          ]),
        ),
      DecoratedBox(
        decoration: BoxDecoration(border: Border.all(color: t.line)),
        child: SizedBox(
          height: widget.height,
          child: !hasData
              ? Center(child: Text(widget.empty, style: TextStyle(color: t.muted, fontSize: 11)))
              : LayoutBuilder(builder: (context, box) {
                  void at(Offset p) => setState(() => _touch = ((p.dx - _Painter.left) /
                          (box.maxWidth - _Painter.left - _Painter.right))
                      .clamp(0.0, 1.0));
                  return GestureDetector(
                    behavior: HitTestBehavior.opaque,
                    onTapDown: (d) => at(d.localPosition),
                    onHorizontalDragStart: (d) => at(d.localPosition),
                    onHorizontalDragUpdate: (d) => at(d.localPosition),
                    child: Semantics(
                      label: '${widget.title} chart',
                      child: CustomPaint(
                        size: Size(box.maxWidth, widget.height),
                        painter: _Painter(widget, shown, t, _touch),
                      ),
                    ),
                  );
                }),
        ),
      ),
    ]);
  }
}

class _KeyPainter extends CustomPainter {
  _KeyPainter(this.dash, this.color);
  final List<double>? dash;
  final Color color;
  @override
  void paint(Canvas c, Size s) {
    final p = Path()..moveTo(0, s.height / 2)..lineTo(s.width, s.height / 2);
    c.drawPath(dash == null ? p : dashed(p, dash!),
        Paint()..color = color..strokeWidth = 1.4..style = PaintingStyle.stroke);
  }

  @override
  bool shouldRepaint(_KeyPainter o) => o.color != color || o.dash != dash;
}

Path dashed(Path src, List<double> pattern) {
  final out = Path();
  for (final m in src.computeMetrics()) {
    var d = 0.0, i = 0;
    while (d < m.length) {
      final len = pattern[i % pattern.length];
      if (i.isEven) out.addPath(m.extractPath(d, d + len), Offset.zero);
      d += len;
      i++;
    }
  }
  return out;
}

class _Painter extends CustomPainter {
  _Painter(this.w, this.series, this.t, this.touch);
  static const left = 38.0, right = 10.0;
  double get bottom => w.xTitle == null ? 20.0 : 32.0;
  // Room for a two-line readout when several series report at once.
  double get top => w.series.length > 2 ? 36.0 : 24.0;
  final LineChart w;
  final List<ChartSeries> series;
  final Tok t;
  final double? touch;

  TextPainter _text(String s, double size, Color c, {double? max}) =>
      TextPainter(
          text: TextSpan(text: s, style: TextStyle(fontFamily: mono, fontSize: size, color: c)),
          textDirection: TextDirection.ltr,
          maxLines: 1,
          ellipsis: '…')
        ..layout(maxWidth: max ?? double.infinity);

  @override
  void paint(Canvas c, Size size) {
    final pts = [for (final s in w.series) ...s.points];
    if (pts.isEmpty) return;
    var x0 = w.xMin ?? pts.map((p) => p.dx).reduce(math.min);
    var x1 = w.xMax ?? pts.map((p) => p.dx).reduce(math.max);
    if (w.bars) {
      x0 -= 0.5;
      x1 += 0.5;
    }
    if (x1 <= x0) x1 = x0 + 1;
    final ys = [for (final p in pts) if (p.dy.isFinite) p.dy];
    final top_ = ys.isEmpty ? 1.0 : math.max(ys.reduce(math.max), 1.0);
    final step = _nice(top_ * 1.05 / 3);
    final y1 = step * (top_ * 1.05 / step).ceil();
    const y0 = 0.0;
    final pw = size.width - left - right, ph = size.height - top - bottom;
    double sx(double x) => left + (x - x0) / (x1 - x0) * pw;
    double sy(double y) => top + ph - (y - y0) / (y1 - y0) * ph;

    final grid = Paint()..color = t.muted.withValues(alpha: .45)..strokeWidth = .6;
    for (var v = 0.0; v <= y1 + step / 2; v += step) {
      final y = sy(v);
      c.drawPath(dashed(Path()..moveTo(left, y)..lineTo(size.width - right, y), [1, 3]), grid);
      final tp = _text(compact(v), 9, t.muted);
      tp.paint(c, Offset(left - 5 - tp.width, y - tp.height / 2));
    }
    // x ticks
    final ticks = w.xTicks ?? [for (var i = 0; i <= 4; i++) x0 + (x1 - x0) * i / 4];
    double lastRight = -1;
    for (final x in ticks) {
      if (x < x0 || x > x1) continue;
      final tp = _text((w.tickLabel ?? w.xLabel)(x), 9, t.muted);
      final px = (sx(x) - tp.width / 2).clamp(left - 4, size.width - 4 - tp.width);
      if (px < lastRight + 8) continue;
      lastRight = px + tp.width;
      tp.paint(c, Offset(px, size.height - bottom + 5));
    }
    c.drawLine(Offset(left, top + ph), Offset(size.width - right, top + ph), Paint()..color = t.line..strokeWidth = 1);
    if (w.xTitle != null) {
      final tp = _text(w.xTitle!.toUpperCase(), 9, t.muted);
      tp.paint(c, Offset(left + (pw - tp.width) / 2, size.height - tp.height - 3));
    }

    final ink = Paint()..color = t.fg..strokeWidth = 1.4..style = PaintingStyle.stroke..strokeJoin = StrokeJoin.round;
    for (final s in series) {
      if (w.bars) {
        final bw = math.max(2.0, pw / (x1 - x0) * .62);
        for (final p in s.points) {
          if (!p.dy.isFinite) continue;
          c.drawRect(Rect.fromLTRB(sx(p.dx) - bw / 2, sy(p.dy), sx(p.dx) + bw / 2, sy(0)), Paint()..color = t.fg);
        }
        continue;
      }
      final path = Path();
      var pen = false;
      Offset? prev;
      for (final p in s.points) {
        final gap = prev != null && w.gapX != null && p.dx - prev.dx > w.gapX!;
        if (!p.dy.isFinite || gap) pen = false;
        if (p.dy.isFinite) {
          if (pen) {
            path.lineTo(sx(p.dx), sy(p.dy));
          } else {
            path.moveTo(sx(p.dx), sy(p.dy));
            path.addOval(Rect.fromCircle(center: Offset(sx(p.dx), sy(p.dy)), radius: .9)); // lone points stay visible
          }
          pen = true;
        }
        prev = p;
      }
      c.drawPath(s.dash == null ? path : dashed(path, s.dash!), ink);
      if (s.dots) {
        final r = s.points.length > 150 ? 1.3 : 2.3; // dense timelines stay legible
        for (var i = 0; i < s.points.length; i++) {
          final p = s.points[i];
          if (!p.dy.isFinite) continue;
          c.drawCircle(Offset(sx(p.dx), sy(p.dy)), r, Paint()..color = s.dotColors?[i] ?? t.fg);
        }
      }
    }

    // Touch readout pinned to the top row, crosshair under the finger.
    final f = touch;
    if (f != null) {
      final x = x0 + f * (x1 - x0);
      Offset? nearest(ChartSeries s, double at) {
        Offset? best;
        for (final p in s.points) {
          if (!p.dy.isFinite) continue;
          if (best == null || (p.dx - at).abs() < (best.dx - at).abs()) best = p;
        }
        return best;
      }
      // Snap to the point closest to the finger across all series, then
      // read each series only where it has a value near that time.
      double? snapX;
      for (final s in series) {
        final b = nearest(s, x);
        if (b != null && (snapX == null || (b.dx - x).abs() < (snapX - x).abs())) snapX = b.dx;
      }
      final tol = w.gapX ?? (x1 - x0) / 40;
      final parts = <String>[];
      for (final s in series) {
        final b = snapX == null ? null : nearest(s, snapX);
        if (b == null || (b.dx - snapX!).abs() > tol) continue;
        parts.add('${series.length > 1 ? '${s.label} ' : ''}${compact(b.dy)}');
      }
      if (parts.isEmpty) return; // nothing shown under the finger (e.g. every series hidden)
      final cx = sx(snapX ?? x);
      c.drawLine(Offset(cx, top), Offset(cx, top + ph), Paint()..color = t.fg.withValues(alpha: .5)..strokeWidth = 1);
      final label = '${w.xLabel(snapX ?? x)} · ${parts.join(' · ')}${w.unit.isEmpty ? '' : ' ${w.unit}'}';
      (TextPainter(
              text: TextSpan(text: label, style: TextStyle(fontFamily: mono, fontSize: 10, color: t.fg, height: 1.2)),
              textDirection: TextDirection.ltr,
              maxLines: top > 24 ? 2 : 1,
              ellipsis: '…')
            ..layout(maxWidth: size.width - 12))
          .paint(c, const Offset(6, 5));
    }
  }

  static double _nice(double v) {
    final p = math.pow(10, (math.log(v) / math.ln10).floor()).toDouble();
    for (final m in [1, 2, 2.5, 5, 10]) {
      if (m * p >= v) return m * p;
    }
    return 10 * p;
  }

  @override
  bool shouldRepaint(_Painter o) => true;
}

/// Monochrome line patterns per forecast model (same idea as the web legend).
const modelDash = <String, List<double>?>{
  'taf': [4, 3],
  'lamp': null,
  'nws_grid': [7, 3],
  'hrrr': [2, 2],
  'nbm': [9, 3, 2, 3],
  'nam': [1, 3],
  'gfs': [13, 4],
};
