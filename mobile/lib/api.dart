import 'dart:convert';

import 'package:http/http.dart' as http;

/// Windcheck server. Override for local testing:
/// flutter run --dart-define=API=http://127.0.0.1:5050
const apiBase = String.fromEnvironment('API', defaultValue: 'https://windcheck.net');

typedef Json = Map<String, dynamic>;

class ApiError implements Exception {
  ApiError(this.message);
  final String message;
  @override
  String toString() => message;
}

/// History refresh is reCAPTCHA-gated on the public site; local servers skip it.
final canRefreshHistory = !Uri.parse(apiBase).host.endsWith('windcheck.net');

// One client so requests reuse the TLS connection (saves a handshake each).
http.Client? _shared;
http.Client get _client => _shared ??= http.Client();

Uri _uri(String path, Map<String, String> query) =>
    Uri.parse(apiBase).replace(path: path, queryParameters: query);

Future<Json> getJson(String path, Map<String, String> query) async {
  final uri = _uri(path, query);
  final http.Response res;
  try {
    res = await _client.get(uri).timeout(const Duration(seconds: 90));
  } catch (_) {
    throw ApiError('Can’t reach ${Uri.parse(apiBase).host}. Check your connection.');
  }
  final Object? body;
  try {
    body = jsonDecode(res.body);
  } catch (_) {
    throw ApiError('Unexpected reply (HTTP ${res.statusCode}).');
  }
  if (res.statusCode >= 400 || body is! Map<String, dynamic>) {
    final msg = body is Map ? body['error'] : null;
    throw ApiError(msg?.toString() ?? 'HTTP ${res.statusCode}');
  }
  return body;
}

/// POSTs JSON and yields each line of the NDJSON reply (history stream).
Stream<Json> postLines(String path, Map<String, String> query, Json body) async* {
  final req = http.Request('POST', _uri(path, query))
    ..headers['content-type'] = 'application/json'
    ..body = jsonEncode(body);
  final res = await _client.send(req);
  if (res.statusCode >= 400) {
    final text = await res.stream.bytesToString();
    Object? err;
    try {
      err = (jsonDecode(text) as Map)['error'];
    } catch (_) {}
    throw ApiError('${err ?? 'HTTP ${res.statusCode}'}');
  }
  yield* res.stream
      .transform(utf8.decoder)
      .transform(const LineSplitter())
      .where((l) => l.trim().isNotEmpty)
      .map((l) => jsonDecode(l) as Json);
}

/// Numbers in payloads arrive as int or double.
double? num_(Object? v) => v is num ? v.toDouble() : null;
