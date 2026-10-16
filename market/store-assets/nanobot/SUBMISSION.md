# Submission: nanobot for reCamera App Center

## Overview

nanobot for reCamera is a third-party, fully open-source application package
for the reCamera Pro (RV1126B) App Center. It deploys the nanobot personal AI
assistant with two user-facing surfaces: a browser-based chat console, and a
hands-free voice channel with wake-word activation. The voice pipeline runs
either fully on-device (local wake-word detection, speech recognition and
speech synthesis) or through cloud realtime services (OpenAI, Gemini, Qwen).
A bundled recamera-local skill allows the assistant to operate the camera
itself: capturing snapshots, configuring AI detection, recording video clips
and managing storage.

- Positioning: non-commercial, open source, third-party plugin
- Source repository: https://github.com/iChizer0/nanobot-app (MIT)
- Upstream components: nanobot (MIT), nanobot-channel-voice (MIT),
  reCamera-Intellisense (MIT)

## Files in this submission

| File | Purpose |
|---|---|
| `nanobot-1.0.0-arm64.tar.gz` | Application package (94.8 MB) |
| `nanobot-1.0.0-arm64.tar.gz.sig` | Detached ECDSA-P256-SHA256 signature (base64 DER), produced with our owner key; intended for integrity verification of this transfer. Please re-sign with the vendor key for catalog listing. |
| `icon.png` | Store icon (PNG; conforms to the `icons/` naming rules) |
| `nanobot-banner.png` | Card banner artwork, 1600x892 (2x of the card's 800/446 aspect ratio) |
| `catalog-entry.json` | Draft catalog entry. URLs assume the standard `packages/` and `icons/` layout; sha256, size and signature are computed from the submitted files. The `image` field (card banner) is supplied as `nanobot-banner.png` (1600x892, exactly 2x the card's 800x446 aspect); host it at `/appcenter/apps/nanobot.png`. |
| `screenshots/` | Captures from a production test unit: App Center card, chat console, and voice-channel configuration panels |

## Validation status

- `validate_app.py --mode publish`: passes with zero errors
- Device testing on reCamera Pro firmware V1.2.1 covers fresh install,
  upgrade, stop/start, configuration restart and rollback-relevant states;
  all passing
- End-user documentation with device screenshots is maintained at
  `docs/CONFIG_GUIDE.md` in the source repository

## Licensing

Full details are in THIRD_PARTY_NOTICES.md, which ships inside the package:

- All first-party code is MIT-licensed
- The default wake-word model (openWakeWord "hey mycroft") is CC-BY-NC-SA 4.0.
  This plugin is therefore positioned for non-commercial distribution;
  commercial integrators are expected to configure a self-trained wake phrase
  through the panel's Custom option
- The Matcha TTS zh-en weights bundle espeak-ng-data (GPL-3.0); the license
  reference and attribution are included in THIRD_PARTY_NOTICES.md
- Model weights are not part of the package. They are downloaded after
  installation from a HuggingFace index, and license-bearing models require
  explicit acceptance in the configuration layer
  (`channels.voice.models.acceptNotices`)

## Contact

iChizer0 — via the GitHub repositories listed above.
