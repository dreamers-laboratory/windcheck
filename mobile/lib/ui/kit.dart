import 'dart:io' show Platform;

import 'package:flutter/material.dart';

/// Design tokens, mirroring windcheck.net: monochrome, 1px rules, mono type.
class Tok {
  const Tok(this.bg, this.fg, this.muted, this.line, this.hover, this.brand, this.accent);
  final Color bg, fg, muted, line, hover, brand, accent;

  static const light = Tok(Color(0xFFFFFFFF), Color(0xFF000000), Color(0xFF555555),
      Color(0xFF000000), Color(0xFFF3F3F3), Color(0xFF1558E6), Color(0xFF1F6FEB));
  static const dark = Tok(Color(0xFF0A0A0A), Color(0xFFF2F2F2), Color(0xFF9A9A9A),
      Color(0xFFE8E8E8), Color(0xFF1B1B1B), Color(0xFF6F9BFF), Color(0xFF6EA8FF));

  static Tok of(BuildContext c) =>
      Theme.of(c).brightness == Brightness.dark ? dark : light;
}

final mono = Platform.isIOS ? 'Menlo' : 'monospace';

ThemeData buildTheme(Brightness b) {
  final t = b == Brightness.dark ? Tok.dark : Tok.light;
  final side = BorderSide(color: t.line);
  final square = OutlineInputBorder(borderRadius: BorderRadius.zero, borderSide: side);
  return ThemeData(
    brightness: b,
    fontFamily: mono,
    scaffoldBackgroundColor: t.bg,
    splashFactory: InkRipple.splashFactory,
    // Monochrome scheme so Material pickers and sheets match the 1px look.
    colorScheme: ColorScheme.fromSeed(seedColor: t.brand, brightness: b).copyWith(
        primary: t.fg, onPrimary: t.bg, secondary: t.fg, onSecondary: t.bg, tertiary: t.fg,
        primaryContainer: t.hover, onPrimaryContainer: t.fg, secondaryContainer: t.hover,
        onSecondaryContainer: t.fg, surface: t.bg, onSurface: t.fg, surfaceTint: Colors.transparent,
        surfaceContainerHigh: t.bg, surfaceContainerHighest: t.hover, outline: t.line,
        tertiaryContainer: t.hover, onTertiaryContainer: t.fg), // time picker AM/PM
    dialogTheme: DialogThemeData(
        backgroundColor: t.bg, shape: RoundedRectangleBorder(side: BorderSide(color: t.line))),
    datePickerTheme: DatePickerThemeData(
        backgroundColor: t.bg, headerBackgroundColor: t.bg, headerForegroundColor: t.fg,
        rangePickerHeaderHeadlineStyle: const TextStyle(fontSize: 18), // fits "Feb 27 – Dec 31, 2025"
        shape: RoundedRectangleBorder(side: BorderSide(color: t.line))),
    timePickerTheme: TimePickerThemeData(
        backgroundColor: t.bg, shape: RoundedRectangleBorder(side: BorderSide(color: t.line))),
    snackBarTheme: SnackBarThemeData(backgroundColor: t.fg, contentTextStyle: TextStyle(color: t.bg, fontFamily: mono)),
    textTheme: TextTheme(bodyMedium: TextStyle(fontSize: 13, color: t.fg, height: 1.35)),
    dividerColor: t.line,
    inputDecorationTheme: InputDecorationTheme(
      isDense: true,
      contentPadding: const EdgeInsets.symmetric(horizontal: 9, vertical: 11),
      border: square,
      enabledBorder: square,
      focusedBorder: square.copyWith(borderSide: BorderSide(color: t.fg, width: 2)),
      hintStyle: TextStyle(color: t.muted),
    ),
    textSelectionTheme: TextSelectionThemeData(cursorColor: t.fg),
    bottomSheetTheme: BottomSheetThemeData(
        backgroundColor: t.bg, shape: const RoundedRectangleBorder(), showDragHandle: false),
  );
}

String up(String s) => s.toUpperCase();

class Label extends StatelessWidget {
  const Label(this.text, {super.key, this.color, this.bold = false, this.size = 11});
  final String text;
  final Color? color;
  final bool bold;
  final double size;
  @override
  Widget build(BuildContext context) => Text(up(text),
      style: TextStyle(
          fontSize: size,
          letterSpacing: size * (bold ? 0.16 : 0.1),
          fontWeight: bold ? FontWeight.w700 : FontWeight.w400,
          color: color ?? Tok.of(context).fg,
          height: 1.25));
}

class Sub extends StatelessWidget {
  const Sub(this.text, {super.key});
  final String text;
  @override
  Widget build(BuildContext context) => Text(up(text),
      style: TextStyle(fontSize: 10, letterSpacing: 0.6, color: Tok.of(context).muted, height: 1.35));
}

/// Bordered section with a ruled header row.
class Panel extends StatelessWidget {
  const Panel({super.key, required this.title, required this.child, this.trailing, this.meta, this.onHeaderTap, this.headerKey});
  final String title;
  final String? meta;
  final Widget child;
  final Widget? trailing;
  final VoidCallback? onHeaderTap; // makes the whole title row a button
  final Key? headerKey;
  @override
  Widget build(BuildContext context) {
    final t = Tok.of(context);
    return DecoratedBox(
      position: DecorationPosition.foreground, // fills inside never cover the rule
      decoration: BoxDecoration(border: Border.all(color: t.line)),
      child: ColoredBox(
        color: t.bg,
        child: Column(crossAxisAlignment: CrossAxisAlignment.stretch, children: [
        InkWell(
          key: headerKey,
          onTap: onHeaderTap,
          child: Container(
            constraints: BoxConstraints(minHeight: onHeaderTap == null ? 34 : 44),
            padding: const EdgeInsets.only(left: 12, right: 4),
            decoration: BoxDecoration(border: Border(bottom: BorderSide(color: t.line))),
            child: Row(children: [
              Expanded(child: Label(title, bold: true)),
              if (meta != null) Padding(padding: const EdgeInsets.only(right: 8), child: Sub(meta!)),
              ?trailing,
            ]),
          ),
        ),
        child,
        ]),
      ),
    );
  }
}

/// Square toggle / button. `on` fills it.
class Tog extends StatelessWidget {
  const Tog(this.text, {super.key, this.on = false, this.onTap, this.height = 40, this.semantics, this.size = 11});
  final String text;
  final bool on;
  final VoidCallback? onTap; // null = disabled, drawn muted
  final double height;
  final String? semantics;
  final double size;
  @override
  Widget build(BuildContext context) {
    final t = Tok.of(context);
    return Semantics(
      button: true,
      selected: on,
      label: semantics,
      child: Material(
        color: on ? t.fg : t.bg,
        shape: Border.all(color: t.line),
        child: InkWell(
          onTap: onTap,
          child: SizedBox(
            height: height,
            child: Center(
                child: Padding(
                  padding: const EdgeInsets.symmetric(horizontal: 4),
                  child: FittedBox(
                    fit: BoxFit.scaleDown,
                    child: Text(up(text),
                        maxLines: 1,
                        style: TextStyle(
                            fontSize: size, letterSpacing: size > 11 ? 0 : 1.1,
                            color: on ? t.bg : (onTap == null ? t.muted.withValues(alpha: .45) : t.fg))),
                  ),
                )),
          ),
        ),
      ),
    );
  }
}

class PrimaryButton extends StatelessWidget {
  const PrimaryButton(this.text, {super.key, this.onTap, this.busy = false});
  final String text;
  final VoidCallback? onTap;
  final bool busy;
  @override
  Widget build(BuildContext context) {
    final t = Tok.of(context);
    return Material(
      color: t.fg,
      child: InkWell(
        onTap: busy ? null : onTap,
        child: SizedBox(
          height: 46,
          child: Center(
            child: busy
                ? SizedBox.square(
                    dimension: 16, child: CircularProgressIndicator(strokeWidth: 1.5, color: t.bg))
                : Text(up(text), style: TextStyle(color: t.bg, fontSize: 13, letterSpacing: 2.6)),
          ),
        ),
      ),
    );
  }
}

class Empty extends StatelessWidget {
  const Empty(this.text, {super.key});
  final String text;
  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 28),
        child: Text(text, textAlign: TextAlign.center, style: TextStyle(color: Tok.of(context).muted)),
      );
}

String fmt(Object? v, [int dp = 1]) => v is num && v.isFinite ? v.toStringAsFixed(dp) : '–';
String fmtInt(Object? v) => v is num && v.isFinite ? v.round().toString() : '–';
String pad3(Object? v) => v is num ? v.round().toString().padLeft(3, '0') : '–';

String runwayLabel(Map<String, dynamic> runway) {
  final magnetic = runway['heading_magnetic_deg'];
  final heading = magnetic is num && magnetic.isFinite ? ' (${pad3(magnetic)}°M)' : '';
  return '${runway['ident'] ?? '–'}$heading';
}
String compact(num v) => v.abs() >= 10000
    ? '${(v / 1000).toStringAsFixed(v % 1000 == 0 ? 0 : 1)}k'
    : (v == v.roundToDouble() ? v.round().toString() : v.toStringAsFixed(1));

String hourLabel(int h) => '${h.toString().padLeft(2, '0')} (${(h + 11) % 12 + 1}${h < 12 ? 'a' : 'p'})';
const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/// "2026-09-26" -> "Sep 26".
String shortDate(String? ymd) {
  final d = ymd == null ? null : DateTime.tryParse(ymd);
  return d == null ? '' : '${months[d.month - 1]}\u00a0${d.day}'; // no line break inside a date
}

/// "Oct 6 → Nov 4" this year; years appear once the span leaves it
/// ("Feb 27 → Dec 31, 2025", "Jun 22, 2023 → Apr 24, 2024").
String dateSpan(String? from, String? to, {String sep = ' → '}) {
  final a = from == null ? null : DateTime.tryParse(from), b = to == null ? null : DateTime.tryParse(to);
  if (a == null || b == null) return '${shortDate(from)}$sep${shortDate(to)}';
  final now = DateTime.now().year;
  if (a.year == now && b.year == now) return '${shortDate(from)}$sep${shortDate(to)}';
  if (a.year == b.year) return '${shortDate(from)}$sep${shortDate(to)},\u00a0${b.year}';
  return '${shortDate(from)},\u00a0${a.year}$sep${shortDate(to)},\u00a0${b.year}';
}

/// One bordered strip of options; the chosen one is filled.
class Segmented<T> extends StatelessWidget {
  const Segmented({super.key, required this.items, required this.value, required this.onChanged, this.height = 38});
  final List<(T, String, int)> items; // value, label, flex
  final T value;
  final ValueChanged<T> onChanged;
  final double height;
  @override
  Widget build(BuildContext context) {
    final t = Tok.of(context);
    return DecoratedBox(
      position: DecorationPosition.foreground,
      decoration: BoxDecoration(border: Border.all(color: t.line)),
      child: Row(children: [
        for (var i = 0; i < items.length; i++)
          Expanded(
            flex: items[i].$3,
            child: Semantics(
              button: true,
              selected: items[i].$1 == value,
              child: Material(
                key: Key('seg-${items[i].$2}'),
                color: items[i].$1 == value ? t.fg : t.bg,
                child: InkWell(
                  onTap: () => onChanged(items[i].$1),
                  child: Container(
                    height: height,
                    alignment: Alignment.center,
                    decoration: i == 0 ? null : BoxDecoration(border: Border(left: BorderSide(color: t.line))),
                    padding: const EdgeInsets.symmetric(horizontal: 4),
                    child: FittedBox(
                      fit: BoxFit.scaleDown, // large text sizes shrink to fit rather than clip
                      child: Text(up(items[i].$2),
                          maxLines: 1,
                          style: TextStyle(fontSize: 11, letterSpacing: 1.1, color: items[i].$1 == value ? t.bg : t.fg)),
                    ),
                  ),
                ),
              ),
            ),
          ),
      ]),
    );
  }
}

/// Table number: shrinks slightly instead of wrapping mid-value.
class Num extends StatelessWidget {
  const Num(this.text, {super.key, this.color, this.left = false});
  final String text;
  final Color? color;
  final bool left;
  @override
  Widget build(BuildContext context) => Padding(
        padding: EdgeInsets.only(left: left ? 0 : 4, right: left ? 6 : 0), // column gutter
        child: FittedBox(
          fit: BoxFit.scaleDown,
          alignment: left ? Alignment.centerLeft : Alignment.centerRight,
          child: Text(text, maxLines: 1, style: TextStyle(fontSize: 11.5, color: color)),
        ),
      );
}

String hour12(int h) => '${(h + 11) % 12 + 1}${h < 12 ? 'a' : 'p'}';

/// Small circled "i" with a 44pt touch target.
class InfoDot extends StatelessWidget {
  const InfoDot({super.key, required this.onTap});
  final VoidCallback onTap;
  @override
  Widget build(BuildContext context) {
    final t = Tok.of(context);
    return Semantics(
      button: true,
      label: 'Which forecast to trust',
      child: InkResponse(
        key: const Key('models-info'),
        onTap: onTap,
        radius: 22,
        child: SizedBox(
          width: 22,
          height: 28,
          child: Center(
            child: Container(
              width: 15,
              height: 15,
              alignment: Alignment.center,
              decoration: BoxDecoration(shape: BoxShape.circle, border: Border.all(color: t.muted)),
              child: Text('i', style: TextStyle(fontSize: 9.5, fontWeight: FontWeight.w700, color: t.muted, height: 1)),
            ),
          ),
        ),
      ),
    );
  }
}
