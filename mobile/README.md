# Windcheck mobile

The Flutter client provides forecast guidance, hourly historical summaries,
and individual airport weather observations. It reads the Windcheck API and
supports automatic device time zones and manual IANA zone selection.

## Run and test

Use a Flutter SDK with Dart 3.13.4 or later within Dart 3.x. Install Xcode
and an iOS Simulator for iOS, or the Android SDK and an emulator for Android.
From this directory:

```bash
flutter pub get
flutter analyze
flutter test
flutter run -d <device-id>
```

The default backend is `https://windcheck.net`. To use the local Flask
server from the iOS Simulator, pass `--dart-define=API=http://127.0.0.1:5050`.
On the standard Android emulator, use `http://10.0.2.2:5050` instead.
For Android, if the Gradle wrapper is missing, initialize the generated
platform files with `flutter create --platforms=android .` before building.

For a physical device or store release, set your own signing credentials and
application identifiers. This source snapshot does not include production
signing, store, or over-the-air update configuration.

The native app reads cached observations from the server. To fetch more
history, use the web app or the [backend CLI](../README.md#quick-start).
Widget tests use synthetic weather fixtures. The layout suite writes local
screenshots to the ignored `test/goldens/` directory.
