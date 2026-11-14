# Contributing

Bug fixes and hardware test reports are welcome. Read the disclaimer and code of conduct.
Use English and keep changes focused. The port remains CC BY-NC-SA 4.0; preserve attribution.

Follow the clean-machine test commands in [README.md](README.md#how-it-works):
Python 3.12, pytest==8.4.2, Node.js >= 20 and the exact upstream commit used by CI.
Run `python -m compileall -q esp32 tools tests`, `python -m pytest -q tests` with
`BW_FLASHER_SRC` set, and `node --test web/test/*.mjs`. For installer changes, also install
`tools/requirements-build.txt` and run `python tools/build_image.py`; check read-back output.
Use fake hardware in tests. Never flash hardware or access serial ports without its owner's permission.

Hardware reports should state scooter model, ESC version, board, wiring/logic levels,
setup A–E, tested steps and outcome. Clearly distinguish simulated from physical tests.
Do not attach firmware binaries, serial numbers, passwords or unredacted logs to issues or PRs.
Report security vulnerabilities privately as described in [SECURITY.md](SECURITY.md).
