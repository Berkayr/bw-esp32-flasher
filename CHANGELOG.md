# Changelog

Changes follow [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.1.0] - 2026-11-17

### Added

- Streaming Brightway and LEQI MicroPython ports with upstream protocol comparison tests.
- ESP32 keep-alive, wire probe and Xiaomi 5 telemetry with private serial display controls.
- Offline phone Wi-Fi UI with bounded uploads, preflight, progress and battery risk guards.
- Desktop Web Serial UI and a classic 4 MiB ESP32 installer image builder.
- Installer licence notices, attribution and community reporting templates.

### Fixed

- Keep-alive restoration after every probe, including failed classifications and callbacks.
- Arduino bridge instructions require 3.3 V level conversion; hardware remains untested.

### Security

- Random per-board AP password generated on first boot and persisted locally.
- USB setup displays phone credentials without sending them to a server.
