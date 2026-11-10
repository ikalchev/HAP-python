// Generates tests/data/sframe_scrypted_vectors.json with Scrypted's SFrame code.
//
// pyhap.media matches the SFrame byte layout of Scrypted's HomeKit WebRTC
// plugin; these vectors pin that. Scrypted's plugins/webrtc has no license
// file, so its sources are not copied into HAP-python: fetch them yourself.
//
// Layout: from https://github.com/koush/scrypted at SCRYPTED_COMMIT, copy
// plugins/webrtc/src/{sframe,sframe-repacketizer,h265-packetizer}.ts and
// plugins/homekit/src/types/camera/{h264-packetizer,jitter-buffer}.ts into
// <scratch>/scrypted/ keeping their paths, give the relative import specifiers
// a .ts suffix and mark the RtpPacket imports as type-only (node cannot run
// them otherwise). Put this file in <scratch>/tools/ and run:
//
//   node --experimental-transform-types gen_sframe_scrypted_vectors.ts out.json
//
// Byte strings over 64 bytes are stored as length and SHA-256. Plaintexts and
// the HEVC input are stored as seeds; tests/webrtc_helpers.py rebuilds them.
import { createHash } from 'crypto';
import { writeFileSync } from 'fs';
import { SFrameAudioReceiver, SFrameStreamCryptor } from '../scrypted/plugins/webrtc/src/sframe.ts';
import { SFrameRepacketizer, SFrameRtpPacketizer } from '../scrypted/plugins/webrtc/src/sframe-repacketizer.ts';
import { H265Repacketizer } from '../scrypted/plugins/webrtc/src/h265-packetizer.ts';

const SCRYPTED_COMMIT = '0482ac4b0bb1bf1d17a5da847f6ad55df84a680e';

// Deterministic byte source so the vectors are reproducible.
function bytes(seed: string, length: number): Buffer {
    const out: Buffer[] = [];
    let counter = 0;
    let total = 0;
    while (total < length) {
        const block = createHash('sha256').update(`${seed}:${counter++}`).digest();
        out.push(block);
        total += block.length;
    }
    return Buffer.concat(out).subarray(0, length);
}

class Packet {
    header: { padding: boolean; marker: boolean; timestamp: number; sequenceNumber: number; payloadType: number; ssrc: number };
    payload: Buffer;
    constructor(header: Packet['header'], payload: Buffer) {
        this.header = { ...header };
        this.payload = payload;
    }
    clone() {
        return new Packet(this.header, Buffer.from(this.payload));
    }
    serialize(): Buffer {
        throw new Error('unused');
    }
}

const hex = (b: Buffer) => b.toString('hex');
const blob = (b: Buffer) => b.length <= 64 ? hex(b)
    : { len: b.length, sha256: createHash('sha256').update(b).digest('hex') };
const quiet = { log() { }, warn() { }, error() { } } as unknown as Console;

// Per-stream key derivation and frame protection, three frames per stream.
const streams: object[] = [];
const streamCases: [string, bigint, number, 6 | 8, bigint][] = [
    ['video-a', 1n, 0x11223344, 6, 0n],
    ['video-b', 0xffffffffffffffffn, 0, 6, 0x1234n],
    ['audio-a', 1n, 0x11223344, 8, 0n],
    ['audio-b', 0x8000000000000000n, 0xffffffff, 8, 0xfffffffen],
];
const plaintextSizes = [0, 160, 4000];
for (const [seed, kid, ssrc, suite, startCounter] of streamCases) {
    const key = bytes(`key-${seed}`, 32);
    const cryptor = new SFrameStreamCryptor({ kid: kid.toString(), key: key.toString('base64') }, ssrc, suite);
    (cryptor as any).counter = startCounter;
    const keys = (cryptor as any).keys as { key: Buffer; salt: Buffer };
    const frames = plaintextSizes.map((size, index) => {
        const pt = bytes(`pt-${seed}-${index}`, size);
        return { counter: (startCounter + BigInt(index)).toString(), pt: { seed: `pt-${seed}-${index}`, len: size }, ct: blob(cryptor.protectFrame(pt)) };
    });
    streams.push({
        name: seed, suite, kid: kid.toString(), ssrc, key: hex(key),
        sframe_key: hex(keys.key), sframe_salt: hex(keys.salt), frames,
    });
}

// SFrame RTP packetization with the 1200-byte payload budget.
const packetized: object[] = [];
{
    const key = bytes('key-packetize', 32);
    const cryptor = new SFrameStreamCryptor({ kid: '42', key: key.toString('base64') }, 0x01020304, 6);
    const packetizer = new SFrameRtpPacketizer(cryptor, 1200);
    const frames = [10, 1188, 1189, 2378].map((size, index) => {
        const pt = bytes(`frame-${index}`, size);
        const input = new Packet({ padding: true, marker: true, timestamp: 90000 + index * 3000, sequenceNumber: 65530, payloadType: 96, ssrc: 0x01020304 }, Buffer.alloc(0));
        const out = packetizer.packetize(input as any, pt) as unknown as Packet[];
        return {
            pt: { seed: `frame-${index}`, len: size },
            packets: out.map(p => ({ sequence: p.header.sequenceNumber, marker: p.header.marker, payload: blob(p.payload) })),
        };
    });
    packetized.push({ suite: 6, kid: '42', ssrc: 0x01020304, key: hex(key), max_payload: 1200, frames });
}

// HEVC RTP (RFC 7798) in, SFrame RTP out: the full video send path. The
// input is described by NAL unit specs; tests rebuild the same payloads.
type NalSpec = [number, number, string];
function nal([type, size, seed]: NalSpec): Buffer {
    return Buffer.concat([Buffer.from([type << 1, 1]), bytes(seed, size - 2)]);
}
function ap(nals: Buffer[]): Buffer {
    const parts: Buffer[] = [Buffer.from([48 << 1, 1])];
    for (const n of nals) {
        const length = Buffer.alloc(2);
        length.writeUInt16BE(n.length);
        parts.push(length, n);
    }
    return Buffer.concat(parts);
}
function fu(n: Buffer, chunk: number): Buffer[] {
    const type = (n[0] >> 1) & 0x3f;
    const out: Buffer[] = [];
    for (let offset = 2; offset < n.length; offset += chunk) {
        const start = offset === 2 ? 0x80 : 0;
        const end = offset + chunk >= n.length ? 0x40 : 0;
        out.push(Buffer.concat([Buffer.from([49 << 1, 1, start | end | type]), n.subarray(offset, offset + chunk)]));
    }
    return out;
}
// Each access unit is one of: ['single', nal], ['ap', nals], ['fu', nal].
type UnitSpec = ['single' | 'ap' | 'fu', NalSpec[]];
const FU_CHUNK = 1400;
const units: UnitSpec[] = [
    ['ap', [[32, 24, 'vps'], [33, 40, 'sps'], [34, 8, 'pps']]],
    ['fu', [[19, 3000, 'idr']]],
    ['single', [[1, 900, 'trail1']]],
    ['ap', [[1, 300, 'slice-a'], [1, 200, 'slice-b']]],
    // IDR without in-band parameter sets: the cached ones are inserted.
    ['fu', [[19, 1600, 'idr2']]],
];
// The first two specs form one access unit (parameter sets plus IDR).
const accessUnits: UnitSpec[][] = [[units[0], units[1]], [units[2]], [units[3]], [units[4]]];
const payloadsOf = ([kind, nals]: UnitSpec): Buffer[] =>
    kind === 'ap' ? [ap(nals.map(nal))] : kind === 'fu' ? fu(nal(nals[0]), FU_CHUNK) : [nal(nals[0])];
const inputPackets: Packet[] = [];
let sequenceNumber = 1000;
accessUnits.forEach((specs, index) => {
    const payloads = specs.flatMap(payloadsOf);
    payloads.forEach((payload, i) => {
        inputPackets.push(new Packet({
            padding: false, marker: i === payloads.length - 1, timestamp: 3000 * (index + 1),
            sequenceNumber: sequenceNumber++ & 0xffff, payloadType: 96, ssrc: 0x55667788,
        }, payload));
    });
});
const repacketized: object[] = [];
{
    const key = bytes('key-hevc', 32);
    const cryptor = new SFrameStreamCryptor({ kid: '3', key: key.toString('base64') }, 0x0a0b0c0d, 6);
    const repacketizer = new SFrameRepacketizer(new H265Repacketizer(quiet, Infinity, {} as any), new SFrameRtpPacketizer(cryptor, 1200));
    const output: object[] = [];
    for (const packet of inputPackets) {
        for (const p of repacketizer.repacketize(packet as any) as unknown as Packet[])
            output.push({ timestamp: p.header.timestamp, marker: p.header.marker, payload: blob(p.payload) });
    }
    repacketized.push({
        suite: 6, kid: '3', ssrc: 0x0a0b0c0d, key: hex(key), max_payload: 1200, fu_chunk: FU_CHUNK,
        access_units: accessUnits,
        input: inputPackets.map(p => ({ sequence: p.header.sequenceNumber, timestamp: p.header.timestamp, marker: p.header.marker, payload: blob(p.payload) })),
        output,
    });
}

// Audio receive path with replay protection.
const receive: object[] = [];
{
    const key = bytes('key-talkback', 32);
    const kid = 9n;
    const ssrc = 0x31323334;
    const sender = new SFrameStreamCryptor({ kid: kid.toString(), key: key.toString('base64') }, ssrc, 8);
    const keys = new Map([[kid.toString(), key.toString('base64')]]);
    const receiver = new SFrameAudioReceiver(keys);
    const protectedFrames = new Map<number, Buffer>();
    const at = (counter: number) => {
        if (!protectedFrames.has(counter)) {
            (sender as any).counter = BigInt(counter);
            protectedFrames.set(counter, sender.protectFrame(bytes(`opus-${counter}`, 60)));
        }
        return protectedFrames.get(counter)!;
    };
    const order = [0, 1, 1, 3, 2, 2, 70, 6, 7, 69, 5, 140, 76, 77, 77];
    const steps = order.map(counter => {
        const payload = Buffer.concat([Buffer.from([0xc0]), at(counter)]);
        try {
            receiver.receive(payload, ssrc);
            return { counter, payload: blob(payload), accepted: true };
        }
        catch {
            return { counter, payload: blob(payload), accepted: false };
        }
    });
    receive.push({ suite: 8, kid: kid.toString(), ssrc, key: hex(key), steps });
}

writeFileSync(process.argv[2], JSON.stringify({
    source: {
        repository: 'https://github.com/koush/scrypted',
        commit: SCRYPTED_COMMIT,
        files: ['plugins/webrtc/src/sframe.ts', 'plugins/webrtc/src/sframe-repacketizer.ts', 'plugins/webrtc/src/h265-packetizer.ts'],
    },
    streams, packetized, repacketized, receive,
}, null, 1) + '\n');
console.log('streams', streams.length, 'repacketized out', (repacketized[0] as any).output.length);
