#!/bin/bash
# Build the audio capture helper: macos/build/audiocap.
# Needs the Xcode command line tools (xcode-select --install).
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p build
swiftc -O -swift-version 5 -target "$(uname -m)-apple-macos14.2" \
    -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker Info.plist \
    audiocap.swift -o build/audiocap
# A stable identifier keeps the privacy grants tied to "audiocap", not a hash.
codesign --force --sign - --identifier local.transcribe-local.audiocap build/audiocap
echo "built $(pwd)/build/audiocap"
