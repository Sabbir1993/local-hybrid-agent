---
name: mobile-testing
description: Build a mobile app, run it on an Android emulator / phone or iOS simulator, drive the UI, read logs and fix crashes.
triggers: android, emulator, apk, mobile app, flutter, react native, expo, kotlin app, ios simulator, test on phone, adb, logcat
---

# Mobile App Testing Skill

Use this when building or fixing an Android / iOS app. Verify on a real emulator or device before calling it done.

1. **Find a target**: `mobile_devices`. If nothing is running, `mobile_boot` an AVD from the list (Android) or a simulator udid with `platform: "ios"` (only on a Mac companion). For a physical Android phone: USB debugging needs nothing; for Wi-Fi use `mobile_connect` (pair once with the 6-digit code shown on the phone).
2. **Build** with `run_shell` in the project:
   - Android (Gradle): `gradlew assembleDebug` → `app/build/outputs/apk/debug/app-debug.apk`
   - Flutter: `flutter build apk --debug` → `build/app/outputs/flutter-apk/app-debug.apk` (or `flutter run -d <serial>`)
   - React Native: `npx react-native run-android` (installs itself) or `cd android && gradlew assembleDebug`
   - Expo: `npx expo run:android`
   - iOS simulator: `xcodebuild -scheme <App> -sdk iphonesimulator -derivedDataPath build` → the `.app` under `build/Build/Products/`
3. **Install and launch**: `mobile_install` with the APK/.app path, then `mobile_launch` with the package / bundle id (from `applicationId` in build.gradle or `app.json`). Use `restart: true` after reinstalling.
4. **See the screen**: `mobile_ui` lists visible elements with `[nN]` refs and positions; `mobile_screenshot` with a `question` shows layout problems.
5. **Interact**: `mobile_tap` (ref or x,y), `mobile_type`, `mobile_swipe` (scroll, or `key: "BACK"`). Re-run `mobile_ui` after each change, because refs are only valid for the dump they came from.
6. **When it crashes or misbehaves**: `mobile_logs` with `app_id` (and `crash: true` for Android crash traces). Fix the code, rebuild, reinstall, retest.
7. **Report** what you ran it on (device/emulator + OS), what you tested, what you fixed.

Rules:
- Never type real card numbers or passwords. For payment/login flows use the gateway sandbox with [PLACEHOLDER] test values the user provides, or ask the user to enter them on the device.
- Installing, booting and pairing ask the user on their machine; if they deny, stop and ask them how to proceed.
- iOS needs the companion on a Mac with Xcode; say so instead of trying on Windows.
