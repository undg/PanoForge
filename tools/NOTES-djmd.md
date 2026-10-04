# DJI Osmo 360 .OSV — structure of the `djmd` metadata track

Analyzed file: CAM_20260707202137_0001_D.OSV (Osmo 360, fw 10.00.25.29,
serial 95SXNAB0425CFN, proto `dvtm_oq101.proto` v2.0.8, 3840x3840 dual-fisheye,
98 frames @ 25 fps = 3.92 s).

## Tracks (ffprobe / mp4parse.py)
- 0:0, 0:1  video hvc1 (2 fisheye hemispheres)
- 0:2       audio AAC
- 0:3  djmd "CAM meta"  ~1 kB/sample, 98 samples  -> IMU + expo + CALIBRATION (1st sample = 7775 bytes)
- 0:4  djmd "CAM meta"  ~140 bytes/sample         -> expo only (no IMU)
- 0:5, 0:6 dbgi "CAM dbgi" ~9 kB/sample       -> debug telemetry (sensor stats, AE), opaque
- root box `camd` (128 KB) = standalone embedded ISOM mini-MP4 (proxy) that
  reduplicates the same djmd metadata. No additional calibration.

## Format: protobuf (no .proto needed, decoding by wire-type)
Each djmd sample = a protobuf message. See field mapping at the top
of `extract_djmd.py`.

### Per-frame sample (track 0:3)
- top#3.#1.#2 : timestamp in microseconds (delta ~40002 µs = 25 fps)
- top#3.#2.#3.#1 : ISO (float)
- top#3.#2.#4.#1 : shutter speed (2 bytes, 01 64)
- top#3.#2.#6.#1 : color temperature (K)
- top#3.#2.#9   : orientation QUATERNION, subfields #1..#4 = [w,x,y,z] float32, ||q||=1.000
- top#3.#2.#10  : ACCELEROMETER, subfields #2,#3,#4 = [x,y,z] float32 in g (||a||≈1.0-1.16)
- top#3.#2.#15  : AE block (EV, gains...), #16.#1 = sensor temperature °C
- top#3.#3      : HIGH-FREQUENCY block = ~40 quaternions/frame (~1 kHz), each sub-msg #3 fields #1..#4

Verified values: ||quaternion||=1.0000, accelerometer matches exactly
`exiftool -ee` (DocN Accelerometer X/Y/Z).

### 1st sample of 0:3 (7775 bytes) — HEADER + OPTICAL CALIBRATION
- top#1.#1 : proto/fw/serial/model/boot_ts
- top#1.#3.#1 : initial orientation quaternion (4 float)
- top#2.#6 : up to 16 lens blocks (`msg[264]`). 2 distinct physical lenses
  repeated (8 variants each, very close calibrations — probably per
  resolution/mode). Per block:
    #1 fx, #2 fy, #3 cx, #4 cy (pixels)
    #5..#8 distortion coeffs (k1,k2,p1,p2)
    #10 width, #11 height (3840)
    #12 yaw°, #13 pitch° (lens A yaw≈-180°, lens B yaw≈0° -> front/rear)
    #21 extrinsic quaternion of the lens (== #28)
    #22, #23 : radial distortion LUT (14 float, angle->radius curve)

Lens A : fx≈1048, cx≈1908, cy≈1921, dist≈[0.075,-0.022,0.017,-0.009]
Lens B : fx≈1050, cx≈1917, cy≈1916, dist≈[0.067,-0.014,0.012,-0.008]

## GPS: ABSENT. No lat/lon/gnss field (Osmo 360 without internal GPS). The rare
~7.31 doubles found by raw scan are false positives (misaligned float32).

## Scripts
- mp4parse.py      : MP4 box parser (stsd/stsz/stsc/stco) -> sample offsets
- dumpsamples.py   : extracts each sample of a trak into separate files
- pbdecode.py      : generic protobuf decoder (hex/tree inspection)
- extract_djmd.py  : PRODUCTION -> calibration.json + imu_perframe.csv + imu_highrate.csv
  Usage: python3 extract_djmd.py <file.OSV> <outdir>
