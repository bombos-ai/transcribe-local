// audiocap.swift — the macOS capture helper for meeting_recorder.py.
//
// macOS has no WASAPI-style loopback device, so system audio comes from a Core
// Audio process tap (macOS 14.2+): a private, global tap on everything the Mac
// plays, wrapped in a private aggregate device. No BlackHole, no Screen
// Recording permission; only "System Audio Recording".
//
//   audiocap system              raw float32 LE, 48 kHz, stereo interleaved -> stdout
//   audiocap mic [--device S]    raw float32 LE, 48 kHz, mono               -> stdout
//   audiocap list                print input/output devices
//
// Diagnostics go to stderr. Both capture modes resample to 48 kHz whatever the
// hardware runs at (AirPods in a call drop to 16/24 kHz), follow the default
// device when it changes mid-meeting, and pad silence over any gap longer than
// a second so the mic and system streams stay paired in time.
//
// Privacy prompts: the helper re-launches itself "disclaimed", so macOS asks
// about (and remembers) *this* binary, whatever started it: Terminal, the menu
// bar app, or launchd. Its Info.plist is embedded at build time (build.sh).
//
// Build: macos/build.sh (swiftc -O + embedded Info.plist + ad-hoc signature).

import AVFoundation
import AudioToolbox
import CoreAudio
import Foundation

let OUT_RATE = 48000.0
let GAP_PAD_SECS = 1.0          // a stall longer than this is filled with silence

func log(_ s: String) {
    FileHandle.standardError.write(("audiocap: " + s + "\n").data(using: .utf8)!)
}

func die(_ s: String, code: Int32 = 1) -> Never {
    log(s)
    exit(code)
}

// ------------------------------------------------------------ disclaim + relaunch
// posix_spawnattr flag that makes the child its own "responsible process" for
// TCC (privacy) purposes. Private but stable API, used by Qt Creator, LLDB, etc.
@_silgen_name("responsibility_spawnattrs_setdisclaim")
func responsibility_spawnattrs_setdisclaim(_ attrs: UnsafeMutablePointer<posix_spawnattr_t?>, _ disclaim: Int32) -> Int32

func relaunchDisclaimed() -> Never {
    var attr: posix_spawnattr_t? = nil
    posix_spawnattr_init(&attr)
    _ = responsibility_spawnattrs_setdisclaim(&attr, 1)
    setenv("AUDIOCAP_DISCLAIMED", "1", 1)

    var size: UInt32 = 4096
    var buf = [CChar](repeating: 0, count: Int(size))
    guard _NSGetExecutablePath(&buf, &size) == 0 else { die("cannot resolve own path") }
    let path = String(cString: buf)

    let args = [path] + CommandLine.arguments.dropFirst()
    var argv: [UnsafeMutablePointer<CChar>?] = args.map { strdup($0) }
    argv.append(nil)
    var pid: pid_t = 0
    let rc = posix_spawn(&pid, path, nil, &attr, argv, environ)
    guard rc == 0 else { die("relaunch failed: \(String(cString: strerror(rc)))") }

    // Forward termination requests to the child, then mirror its exit status.
    for sig in [SIGTERM, SIGINT, SIGHUP] {
        signal(sig, SIG_IGN)
        let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
        src.setEventHandler { kill(pid, sig) }
        src.resume()
        _sources.append(src)
    }
    DispatchQueue.global().async {
        var status: Int32 = 0
        while waitpid(pid, &status, 0) < 0 && errno == EINTR {}
        let code: Int32 = (status & 0x7f) == 0 ? (status >> 8) & 0xff : 128 + (status & 0x7f)
        exit(code)
    }
    dispatchMain()
}
var _sources: [DispatchSourceSignal] = []

// ------------------------------------------------------------ Core Audio helpers
func getProp<T>(_ obj: AudioObjectID, _ sel: AudioObjectPropertySelector,
                scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal, _ value: inout T) -> OSStatus {
    var addr = AudioObjectPropertyAddress(mSelector: sel, mScope: scope, mElement: kAudioObjectPropertyElementMain)
    var size = UInt32(MemoryLayout<T>.size)
    return withUnsafeMutablePointer(to: &value) { AudioObjectGetPropertyData(obj, &addr, 0, nil, &size, $0) }
}

func getString(_ obj: AudioObjectID, _ sel: AudioObjectPropertySelector) -> String? {
    var addr = AudioObjectPropertyAddress(mSelector: sel, mScope: kAudioObjectPropertyScopeGlobal,
                                          mElement: kAudioObjectPropertyElementMain)
    var size = UInt32(MemoryLayout<CFString?>.size)
    var value: Unmanaged<CFString>? = nil
    let err = withUnsafeMutablePointer(to: &value) { AudioObjectGetPropertyData(obj, &addr, 0, nil, &size, $0) }
    guard err == noErr, let v = value else { return nil }
    return v.takeRetainedValue() as String
}

func allDevices() -> [AudioObjectID] {
    var addr = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDevices,
                                          mScope: kAudioObjectPropertyScopeGlobal,
                                          mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size) == noErr else { return [] }
    var ids = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
    guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &ids) == noErr else { return [] }
    return ids
}

func channelCount(_ dev: AudioObjectID, input: Bool) -> Int {
    var addr = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyStreamConfiguration,
                                          mScope: input ? kAudioObjectPropertyScopeInput : kAudioObjectPropertyScopeOutput,
                                          mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(dev, &addr, 0, nil, &size) == noErr, size > 0 else { return 0 }
    let raw = UnsafeMutableRawPointer.allocate(byteCount: Int(size), alignment: 16)
    defer { raw.deallocate() }
    guard AudioObjectGetPropertyData(dev, &addr, 0, nil, &size, raw) == noErr else { return 0 }
    let abl = UnsafeMutableAudioBufferListPointer(raw.assumingMemoryBound(to: AudioBufferList.self))
    return abl.reduce(0) { $0 + Int($1.mNumberChannels) }
}

func defaultDevice(input: Bool) -> AudioObjectID {
    var id = AudioObjectID(kAudioObjectUnknown)
    _ = getProp(AudioObjectID(kAudioObjectSystemObject),
                input ? kAudioHardwarePropertyDefaultInputDevice : kAudioHardwarePropertyDefaultOutputDevice, &id)
    return id
}

func deviceName(_ dev: AudioObjectID) -> String { getString(dev, kAudioObjectPropertyName) ?? "device \(dev)" }

/// Call `handler` on `queue` whenever the default input/output device changes.
func onDefaultDeviceChange(input: Bool, queue: DispatchQueue, _ handler: @escaping () -> Void) {
    var addr = AudioObjectPropertyAddress(
        mSelector: input ? kAudioHardwarePropertyDefaultInputDevice : kAudioHardwarePropertyDefaultOutputDevice,
        mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
    AudioObjectAddPropertyListenerBlock(AudioObjectID(kAudioObjectSystemObject), &addr, queue) { _, _ in handler() }
}

// ------------------------------------------------------------ output writer
/// Converts whatever the hardware delivers to 48 kHz float32 interleaved and
/// writes it to stdout. Everything runs on one serial queue, so a converter
/// swap after a device change can't race a write.
final class Writer {
    let queue = DispatchQueue(label: "audiocap.writer")
    let outFormat: AVAudioFormat
    private var converter: AVAudioConverter?
    private var t0: Double? = nil
    private var written: Int64 = 0
    private let out = FileHandle.standardOutput

    init(channels: AVAudioChannelCount) {
        outFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: OUT_RATE,
                                  channels: channels, interleaved: true)!
        // A stalled device (unplugged, switching, asleep) would otherwise let the
        // other stream run ahead. Fill gaps with silence on a timer.
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now() + 0.25, repeating: 0.25)
        timer.setEventHandler { [weak self] in self?.padGap() }
        timer.resume()
        _timer = timer
    }
    private var _timer: DispatchSourceTimer?

    /// Called on `queue` with a buffer the caller owns (already copied).
    func push(_ buf: AVAudioPCMBuffer) {
        if converter == nil || converter!.inputFormat != buf.format {
            converter = AVAudioConverter(from: buf.format, to: outFormat)
            converter?.downmix = true
            if converter == nil { log("no converter for \(buf.format)"); return }
        }
        let cap = AVAudioFrameCount(Double(buf.frameLength) * OUT_RATE / buf.format.sampleRate) + 64
        guard let outBuf = AVAudioPCMBuffer(pcmFormat: outFormat, frameCapacity: cap) else { return }
        var fed = false
        var err: NSError?
        let st = converter!.convert(to: outBuf, error: &err) { _, status in
            if fed { status.pointee = .noDataNow; return nil }
            fed = true
            status.pointee = .haveData
            return buf
        }
        if st == .error { log("convert failed: \(err?.localizedDescription ?? "?")"); converter = nil; return }
        if t0 == nil { t0 = now() }
        emit(outBuf)
    }

    private func now() -> Double { Double(DispatchTime.now().uptimeNanoseconds) / 1e9 }

    private func padGap() {
        guard let t0 = t0 else { return }
        let behind = Int64((now() - t0) * OUT_RATE) - written
        guard Double(behind) > GAP_PAD_SECS * OUT_RATE else { return }
        let n = Int(behind)
        out.write(Data(count: n * Int(outFormat.channelCount) * 4))
        written += Int64(n)
    }

    private func emit(_ b: AVAudioPCMBuffer) {
        let n = Int(b.frameLength)
        guard n > 0, let p = b.audioBufferList.pointee.mBuffers.mData else { return }
        out.write(Data(bytes: p, count: n * Int(outFormat.channelCount) * 4))
        written += Int64(n)
    }

    /// Restart the gap clock after a deliberate rebuild (the pad timer already
    /// covered the hole); keeps `written` monotonic.
    func resetConverter() { converter = nil }
}

/// Copy a Core Audio buffer list into an AVAudioPCMBuffer we own, so the IO
/// thread can return immediately and conversion happens off the real-time path.
func copyBuffer(_ abl: UnsafePointer<AudioBufferList>, format: AVAudioFormat) -> AVAudioPCMBuffer? {
    guard let src = AVAudioPCMBuffer(pcmFormat: format, bufferListNoCopy: abl, deallocator: nil),
          src.frameLength > 0,
          let dst = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: src.frameLength) else { return nil }
    dst.frameLength = src.frameLength
    let s = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: src.audioBufferList))
    let d = UnsafeMutableAudioBufferListPointer(dst.mutableAudioBufferList)
    for i in 0..<min(s.count, d.count) {
        let bytes = min(Int(s[i].mDataByteSize), Int(d[i].mDataByteSize))
        if let sp = s[i].mData, let dp = d[i].mData { memcpy(dp, sp, bytes) }
        d[i].mDataByteSize = UInt32(bytes)
    }
    return dst
}

// ------------------------------------------------------------ system audio (process tap)
final class SystemCapture {
    let writer = Writer(channels: 2)
    let ioQueue = DispatchQueue(label: "audiocap.io", qos: .userInteractive)
    var tapID = AudioObjectID(kAudioObjectUnknown)
    var aggID = AudioObjectID(kAudioObjectUnknown)
    var procID: AudioDeviceIOProcID?
    var tapASBD = AudioStreamBasicDescription()
    var format: AVAudioFormat!         // what buffers really arrive as (see deviceFormat); ioQueue only
    var callbacks = 0                  // touched on ioQueue / the IO thread only as a liveness hint
    var style = 0                      // which aggregate recipe worked (see build)

    func start() {
        guard #available(macOS 14.2, *) else { die("system audio capture needs macOS 14.2 or later") }
        build()
        onDefaultDeviceChange(input: false, queue: writer.queue) { [weak self] in
            guard let self = self else { return }
            log("default output changed to \(deviceName(defaultDevice(input: false))); rebuilding tap")
            self.teardown()
            self.build()
        }
    }

    /// Two recipes, because they behave differently across macOS releases:
    /// 0) the output device as main sub-device, tap listed at creation (Apple's
    ///    sample / AudioCap); 1) an empty aggregate with the tap added after
    ///    creation (audiotee). If the first delivers nothing in 2 s, use the second.
    @available(macOS 14.2, *)
    func build() {
        for attempt in [style, 1 - style] {
            createTap()
            createAggregate(style: attempt)
            startIO()
            let deadline = Date().addingTimeInterval(2)
            while callbacks == 0 && Date() < deadline { usleep(20_000) }
            if callbacks > 0 {
                style = attempt
                writer.resetConverter()
                log("system: tapping all output via \(deviceName(defaultDevice(input: false))) " +
                    "(\(Int(format.sampleRate)) Hz, \(format.channelCount) ch, recipe \(attempt))")
                return
            }
            log("system: recipe \(attempt) delivered no audio; trying the other one")
            teardown()
        }
        die("system audio tap delivered no audio. Check System Settings › Privacy & Security › " +
            "Screen & System Audio Recording (\"audiocap\" must be allowed).", code: 4)
    }

    @available(macOS 14.2, *)
    func createTap() {
        let desc = CATapDescription(stereoGlobalTapButExcludeProcesses: [])
        desc.uuid = UUID()
        desc.name = "transcribe-local"
        desc.isPrivate = true
        desc.muteBehavior = .unmuted
        var err = AudioHardwareCreateProcessTap(desc, &tapID)
        guard err == noErr else { die("AudioHardwareCreateProcessTap failed (\(err))") }
        err = getProp(tapID, kAudioTapPropertyFormat, &tapASBD)
        guard err == noErr, AVAudioFormat(streamDescription: &tapASBD) != nil else { die("cannot read tap format (\(err))") }
    }

    /// The tap's channel layout at the rate the aggregate actually runs at. That's
    /// its main sub-device's rate, not the tap's: AirPods in a call run at 24 kHz
    /// while the tap still says 48 kHz, and taking the tap's word for it made the
    /// other side come out twice as fast with every other second padded silence.
    func deviceFormat() -> AVAudioFormat {
        var asbd = tapASBD
        var rate = Float64(0)
        if getProp(aggID, kAudioDevicePropertyNominalSampleRate, &rate) == noErr, rate > 0 {
            asbd.mSampleRate = rate
        }
        return AVAudioFormat(streamDescription: &asbd)!
    }

    func createAggregate(style: Int) {
        guard let tapUID = getString(tapID, kAudioTapPropertyUID) else { die("cannot read tap UID") }
        var desc: [String: Any] = [
            kAudioAggregateDeviceNameKey: "transcribe-local tap",
            kAudioAggregateDeviceUIDKey: UUID().uuidString,
            kAudioAggregateDeviceIsPrivateKey: true,
            kAudioAggregateDeviceIsStackedKey: false,
        ]
        if style == 0 {
            var out = AudioObjectID(kAudioObjectUnknown)
            _ = getProp(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyDefaultSystemOutputDevice, &out)
            guard let outUID = getString(out, kAudioDevicePropertyDeviceUID) else { die("no default output device") }
            desc[kAudioAggregateDeviceMainSubDeviceKey] = outUID
            desc[kAudioAggregateDeviceTapAutoStartKey] = true
            desc[kAudioAggregateDeviceSubDeviceListKey] = [[kAudioSubDeviceUIDKey: outUID]]
            desc[kAudioAggregateDeviceTapListKey] = [[kAudioSubTapDriftCompensationKey: true, kAudioSubTapUIDKey: tapUID]]
        } else {
            desc[kAudioAggregateDeviceSubDeviceListKey] = [] as [Any]
        }
        var err = AudioHardwareCreateAggregateDevice(desc as CFDictionary, &aggID)
        guard err == noErr else { die("AudioHardwareCreateAggregateDevice failed (\(err))") }
        if style == 1 {
            var addr = AudioObjectPropertyAddress(mSelector: kAudioAggregateDevicePropertyTapList,
                                                  mScope: kAudioObjectPropertyScopeGlobal,
                                                  mElement: kAudioObjectPropertyElementMain)
            let list = [tapUID] as CFArray
            err = withUnsafePointer(to: list) {
                AudioObjectSetPropertyData(aggID, &addr, 0, nil, UInt32(MemoryLayout<CFArray>.size), $0)
            }
            guard err == noErr else { die("adding the tap to the aggregate failed (\(err))") }
        }
    }

    func startIO() {
        callbacks = 0
        format = deviceFormat()
        let writer = self.writer
        var lastCheck = DispatchTime.now()
        var err = AudioDeviceCreateIOProcIDWithBlock(&procID, aggID, ioQueue) { [weak self] _, inData, _, _, _ in
            guard let self = self else { return }
            self.callbacks += 1
            // The rate changes under us when a headset enters or leaves call mode.
            let now = DispatchTime.now()
            if now.uptimeNanoseconds - lastCheck.uptimeNanoseconds > 250_000_000 {
                lastCheck = now
                let fmt = self.deviceFormat()
                if fmt.sampleRate != self.format.sampleRate {
                    log("system: device rate changed \(Int(self.format.sampleRate)) -> \(Int(fmt.sampleRate)) Hz")
                    self.format = fmt
                }
            }
            if let copy = copyBuffer(inData, format: self.format) {
                writer.queue.async { writer.push(copy) }
            }
        }
        guard err == noErr else { die("AudioDeviceCreateIOProcIDWithBlock failed (\(err))") }
        err = AudioDeviceStart(aggID, procID)
        guard err == noErr else { die("AudioDeviceStart failed (\(err))") }
    }

    func teardown() {
        if aggID != kAudioObjectUnknown {
            AudioDeviceStop(aggID, procID)
            if let p = procID { AudioDeviceDestroyIOProcID(aggID, p) }
            AudioHardwareDestroyAggregateDevice(aggID)
        }
        if tapID != kAudioObjectUnknown, #available(macOS 14.2, *) { AudioHardwareDestroyProcessTap(tapID) }
        procID = nil
        aggID = AudioObjectID(kAudioObjectUnknown)
        tapID = AudioObjectID(kAudioObjectUnknown)
    }
}

// ------------------------------------------------------------ microphone
final class MicCapture {
    let writer = Writer(channels: 1)
    let match: String?
    var engine = AVAudioEngine()

    init(match: String?) { self.match = match }

    func start() {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: break
        case .notDetermined:
            let sem = DispatchSemaphore(value: 0)
            var ok = false
            AVCaptureDevice.requestAccess(for: .audio) { ok = $0; sem.signal() }
            sem.wait()
            if !ok { die("microphone access denied", code: 3) }
        default:
            die("microphone access denied — allow \"audiocap\" in System Settings › Privacy & Security › Microphone", code: 3)
        }
        build()
        // Follow the default input (headset plugged in, AirPods connected) unless
        // a specific device was asked for; AVAudioEngine also posts this when
        // the current device's format changes or it disappears.
        NotificationCenter.default.addObserver(forName: .AVAudioEngineConfigurationChange, object: nil,
                                               queue: nil) { [weak self] _ in
            self?.writer.queue.async { self?.restart("audio configuration changed") }
        }
        if match == nil {
            onDefaultDeviceChange(input: true, queue: writer.queue) { [weak self] in
                self?.restart("default input changed")
            }
        }
    }

    func restart(_ why: String) {
        log("mic: \(why); restarting")
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        engine = AVAudioEngine()
        build()
    }

    func pickDevice() -> AudioObjectID? {
        guard let m = match?.lowercased(), !m.isEmpty else { return nil }
        let found = allDevices().first { channelCount($0, input: true) > 0 && deviceName($0).lowercased().contains(m) }
        if found == nil { die("no input device matching \"\(match!)\"") }
        return found
    }

    func build() {
        let input = engine.inputNode
        if var id = pickDevice(), let au = input.audioUnit {
            let err = AudioUnitSetProperty(au, kAudioOutputUnitProperty_CurrentDevice, kAudioUnitScope_Global, 0,
                                           &id, UInt32(MemoryLayout<AudioObjectID>.size))
            if err != noErr { die("cannot select input device (\(err))") }
        }
        let fmt = input.outputFormat(forBus: 0)
        guard fmt.sampleRate > 0, fmt.channelCount > 0 else {
            log("mic: no usable input right now; retrying in 1s")
            writer.queue.asyncAfter(deadline: .now() + 1) { [weak self] in self?.restart("retry") }
            return
        }
        let writer = self.writer
        input.installTap(onBus: 0, bufferSize: 4096, format: fmt) { buf, _ in
            // The tap's buffer is reused by AVAudioEngine; copy before hopping queues.
            if let copy = copyBuffer(buf.audioBufferList, format: buf.format) {
                writer.queue.async { writer.push(copy) }
            }
        }
        do { try engine.start() } catch {
            log("mic: engine start failed (\(error.localizedDescription)); retrying in 1s")
            input.removeTap(onBus: 0)
            writer.queue.asyncAfter(deadline: .now() + 1) { [weak self] in self?.restart("retry") }
            return
        }
        writer.resetConverter()
        let devID = pickDevice() ?? defaultDevice(input: true)
        log("mic: \(deviceName(devID)) (\(Int(fmt.sampleRate)) Hz, \(fmt.channelCount) ch)")
    }
}

// ------------------------------------------------------------ list
func listDevices() {
    let defIn = defaultDevice(input: true), defOut = defaultDevice(input: false)
    print("=== Microphones (inputs) ===")
    for d in allDevices() where channelCount(d, input: true) > 0 {
        let name = deviceName(d)
        if name.hasPrefix("transcribe-local") { continue }
        print("  \(name)\(d == defIn ? "  [default]" : "")")
    }
    print("\n=== Outputs (system audio is tapped from all of them) ===")
    for d in allDevices() where channelCount(d, input: false) > 0 {
        let name = deviceName(d)
        if name.hasPrefix("transcribe-local") { continue }
        print("  \(name)\(d == defOut ? "  [default]" : "")")
    }
    print("\nDefault mic:     \(deviceName(defIn))")
    print("Default speaker: \(deviceName(defOut))")
}

// ------------------------------------------------------------ main
let args = Array(CommandLine.arguments.dropFirst())
let mode = args.first ?? ""
signal(SIGPIPE, SIG_DFL)            // reader gone -> just exit

switch mode {
case "list":
    listDevices()
    exit(0)
case "system", "mic":
    if getenv("AUDIOCAP_DISCLAIMED") == nil { relaunchDisclaimed() }
    var device: String? = nil
    if let i = args.firstIndex(of: "--device"), i + 1 < args.count { device = args[i + 1] }
    if mode == "system" {
        let cap = SystemCapture()
        cap.start()
        for sig in [SIGTERM, SIGINT, SIGHUP] {
            signal(sig, SIG_IGN)
            let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
            src.setEventHandler { cap.teardown(); exit(0) }
            src.resume()
            _sources.append(src)
        }
        withExtendedLifetime(cap) { dispatchMain() }
    } else {
        let cap = MicCapture(match: device)
        cap.start()
        withExtendedLifetime(cap) { dispatchMain() }
    }
default:
    FileHandle.standardError.write("usage: audiocap system | mic [--device NAME] | list\n".data(using: .utf8)!)
    exit(2)
}
