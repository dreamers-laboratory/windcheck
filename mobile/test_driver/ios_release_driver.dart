import 'dart:convert';
import 'dart:io';

import 'package:integration_test/integration_test_driver_extended.dart';

Future<void> main() async {
  final path = Platform.environment['WINDCHECK_SCREENSHOTS'];
  if (path == null || !path.startsWith('/')) {
    throw StateError(
      'Set WINDCHECK_SCREENSHOTS to an absolute output directory.',
    );
  }
  final output = Directory(path);
  await output.create(recursive: true);
  final captured = <String>{};
  await integrationDriver(
    onScreenshot: (name, bytes, [args]) async {
      if (!RegExp(r'^[a-zA-Z0-9-]+$').hasMatch(name)) return false;
      if (bytes.length < 1024) throw StateError('Empty screenshot: $name');
      // iOS captures each UIKit window at takeScreenshot, not at driver completion.
      await File('${output.path}/$name.png').writeAsBytes(bytes);
      captured.add(name);
      return true;
    },
    responseDataCallback: (data) async {
      final summary = Map<String, dynamic>.from(data ?? {})
        ..remove('screenshots');
      summary['screenshots'] = captured.toList()..sort();
      await File('${output.path}/results.json')
          .writeAsString(const JsonEncoder.withIndent('  ').convert(summary));
      final expected = int.parse(Platform.environment['WINDCHECK_EXPECT_SCREENSHOTS'] ?? '9');
      if (captured.length != expected) {
        throw StateError('Expected $expected screenshots, got ${captured.length}.');
      }
    },
    writeResponseOnFailure: true,
  );
}
