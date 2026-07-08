#!/usr/bin/env python3
"""
Extract IMU (orientation quaternion + accelerometer), exposure metadata and
optical calibration from a DJI Osmo 360 .OSV/.MP4 file (djmd metadata track,
protobuf dvtm_oq101.proto).

Outputs:
  imu_perframe.csv    : one row per video frame - timestamp, quaternion, accel, ISO, shutter, colortemp
  imu_highrate.csv    : ~1 kHz orientation quaternion stream
  calibration.json    : per-lens intrinsics/distortion/extrinsics + global header

Usage: python3 extract_djmd.py <file.OSV> [outdir]

Protobuf field map (reverse-engineered, Osmo 360 fw 10.00.25.29 / proto 2.0.8):
  Per-frame metadata sample (djmd track, handler 'CAM meta', ~1 kB):
    top #3 -> payload
       .#1  header: (#1 type, #2 timestamp_us_like)
       .#2  frame metadata:
            #3.#1  ISO (float)
            #4.#1  shutter (2 bytes, e.g. 01 64 -> 1/356? exiftool reads 1/100)
            #6.#1  color temperature (varint, Kelvin)
            #9     orientation quaternion (4x float32, normalized) [w,x,y,z order TBD]
            #10    accelerometer (3x float32, units of g)  fields #2,#3,#4
            #15    exposure/AE block (7 floats: EV, ?, gains..., + flag)
            #16.#1 sensor temperature (float, deg C)
       .#3  high-rate IMU block:
            ...#1.#1 block start timestamp
            ...#1 repeated #3 = orientation quaternion (4x float32) ~40/frame (~1 kHz)
  First sample of djmd track (~7.7 kB) additionally carries:
    top #1  device header (proto name, fw, serial, model 'Osmo 360', boot ts)
        #1.#3 initial orientation quaternion (4 floats)
    top #2 .#6  optical calibration: up to 16 lens blocks, first 2 populated:
       #1  fx (float, px)          #2  fy (float, px)
       #3  cx (float, px)          #4  cy (float, px)
       #5..#8 distortion coeffs (k1,k2,p1,p2-like)
       #10 img width  #11 img height
       #12 lens yaw(deg) #13 lens pitch(deg)
       #21 lens extrinsic quaternion (4 floats)  (== #28)
       #22 radial-distortion LUT (14 floats, field->radius)
       #23 radial-distortion LUT (14 floats)
"""
import sys, os, struct, json, csv
from mp4parse import walk, parse_trak, sample_offsets

def read_varint(b,i):
    shift=0;r=0
    while True:
        c=b[i];i+=1;r|=(c&0x7f)<<shift
        if not c&0x80:break
        shift+=7
    return r,i

def parse_fields(b):
    """Return dict field->list of (wiretype,value_bytes/number)."""
    i=0;n=len(b);out={}
    while i<n:
        tag,i=read_varint(b,i)
        f=tag>>3;wt=tag&7
        if wt==0: v,i=read_varint(b,i)
        elif wt==5: v=b[i:i+4];i+=4
        elif wt==1: v=b[i:i+8];i+=8
        elif wt==2:
            ln,i=read_varint(b,i);v=b[i:i+ln];i+=ln
        else: break
        out.setdefault(f,[]).append((wt,v))
    return out

def f32(v): return struct.unpack('<f',v)[0]
def floats(b): return list(struct.unpack('<%df'%(len(b)//4),b))
def subfloats(b, keys):
    """Parse a sub-message and return [f32(field k) for k in keys], None if absent."""
    fl=parse_fields(b)
    return [ (f32(fl[k][0][1]) if k in fl else None) for k in keys ]

def get_traks(data):
    tl=[]
    walk(data,0,len(data),lambda t,o,s,h,d: tl.append((o,s)) if t==b'trak' else None)
    res=[]
    for o,s in tl:
        info=parse_trak(data,o,s)
        info['samples']=sample_offsets(info)
        res.append(info)
    return res

def find_djmd_full(traks):
    """The djmd track whose first sample is large (contains calibration+IMU)."""
    cand=[]
    for t in traks:
        if t['codec']==b'djmd' and t['samples']:
            cand.append((t['samples'][0][1],t))  # size of first sample
    cand.sort(key=lambda x:x[0],reverse=True)
    return cand[0][1] if cand else None

def decode_perframe(payload):
    """payload = bytes of the per-frame protobuf sample."""
    top=parse_fields(payload)
    row={}
    # frame metadata lives under top #3 -> .#2 ; high-rate under #3
    if 3 not in top: return None,[]
    inner=parse_fields(top[3][0][1])
    # header timestamp
    if 1 in inner:
        h=parse_fields(inner[1][0][1])
        if 2 in h and h[2][0][0]==0: row['timestamp']=h[2][0][1]
    hr=[]
    if 2 in inner:
        m=parse_fields(inner[2][0][1])
        if 3 in m: row['iso']=f32(m[3][0][1][ -4:]) if False else f32(parse_fields(m[3][0][1])[1][0][1])
        if 6 in m: row['color_temp']=parse_fields(m[6][0][1])[1][0][1]
        if 4 in m:
            sb=parse_fields(m[4][0][1])[1][0][1]
            row['shutter_raw']=sb.hex()
        if 9 in m:  # quaternion: sub-fields #1..#4
            row['quat']=subfloats(m[9][0][1],(1,2,3,4))
        if 10 in m: # accel: sub-fields #2,#3,#4 (units of g)
            row['accel']=subfloats(m[10][0][1],(2,3,4))
        if 16 in m:
            row['sensor_temp']=f32(parse_fields(m[16][0][1])[1][0][1])
    # high-rate quaternions under inner #3
    if 3 in inner:
        b=inner[3][0][1]
        lvl=parse_fields(b)          # #2
        if 2 in lvl:
            b2=parse_fields(lvl[2][0][1])  # #1
            if 1 in b2:
                blk=parse_fields(b2[1][0][1])
                ts=blk[1][0][1] if 1 in blk and blk[1][0][0]==0 else None
                for wt,v in blk.get(3,[]):
                    if wt==2:
                        q=subfloats(v,(1,2,3,4))
                        if all(x is not None for x in q): hr.append(q)
    return row,hr

def decode_calibration(first_sample):
    top=parse_fields(first_sample)
    out={'lenses':[]}
    if 1 in top:
        hdr=parse_fields(top[1][0][1])
        dev=parse_fields(hdr[1][0][1]) if 1 in hdr else {}
        def s(k):
            return dev[k][0][1].decode('utf-8','replace') if k in dev and dev[k][0][0]==2 else None
        out['proto']=s(1); out['fw_a']=s(2); out['proto_ver']=s(3)
        out['serial']=s(5); out['fw_b']=s(6); out['model']=s(10)
        if 9 in dev and dev[9][0][0]==0: out['boot_ts']=dev[9][0][1]
        if 3 in hdr:
            q=parse_fields(hdr[3][0][1])
            if 1 in q: out['initial_quat']=floats(q[1][0][1])
    if 2 in top:
        blk=parse_fields(top[2][0][1])
        if 6 in blk:
            cal=parse_fields(blk[6][0][1])
            for f in sorted(cal):
                for wt,v in cal[f]:
                    lp=parse_fields(v)
                    if 1 not in lp:  # empty/zero lens
                        continue
                    def g(k): return f32(lp[k][0][1]) if k in lp else None
                    lens={
                        'fx':g(1),'fy':g(2),'cx':g(3),'cy':g(4),
                        'dist':[g(k) for k in (5,6,7,8)],
                        'width':g(10),'height':g(11),
                        'yaw_deg':g(12),'pitch_deg':g(13),
                    }
                    if 21 in lp: lens['extrinsic_quat']=floats(lp[21][0][1])
                    if 22 in lp: lens['radial_lut_1']=floats(lp[22][0][1])
                    if 23 in lp: lens['radial_lut_2']=floats(lp[23][0][1])
                    out['lenses'].append(lens)
    return out

def main():
    fn=sys.argv[1]
    outdir=sys.argv[2] if len(sys.argv)>2 else 'djmd_out'
    os.makedirs(outdir,exist_ok=True)
    data=open(fn,'rb').read()
    traks=get_traks(data)
    trak=find_djmd_full(traks)
    if not trak:
        print("no djmd track found");return
    samples=trak['samples']
    # calibration from first sample
    cal=decode_calibration(data[samples[0][0]:samples[0][0]+samples[0][1]])
    json.dump(cal,open(os.path.join(outdir,'calibration.json'),'w'),indent=2)
    # per-frame + high-rate from remaining samples
    pf_rows=[];hr_rows=[]
    for idx,(off,sz) in enumerate(samples):
        row,hr=decode_perframe(data[off:off+sz])
        if row is None: continue
        q=row.get('quat',[None]*4); a=row.get('accel',[None]*3)
        pf_rows.append([idx,row.get('timestamp'),*q,*a,
                        row.get('iso'),row.get('shutter_raw'),
                        row.get('color_temp'),row.get('sensor_temp')])
        for j,hq in enumerate(hr):
            hr_rows.append([idx,j,*hq])
    with open(os.path.join(outdir,'imu_perframe.csv'),'w',newline='') as fp:
        w=csv.writer(fp)
        w.writerow(['frame','timestamp','qw','qx','qy','qz','ax_g','ay_g','az_g','iso','shutter_raw','color_temp_K','sensor_temp_C'])
        w.writerows(pf_rows)
    with open(os.path.join(outdir,'imu_highrate.csv'),'w',newline='') as fp:
        w=csv.writer(fp)
        w.writerow(['frame','subidx','qw','qx','qy','qz'])
        w.writerows(hr_rows)
    print(f"frames={len(pf_rows)}  highrate_samples={len(hr_rows)}  (~{len(hr_rows)/max(1,len(pf_rows)):.1f}/frame)")
    print(f"lenses with data: {len(cal['lenses'])}")
    print("wrote:",outdir,"/ {calibration.json, imu_perframe.csv, imu_highrate.csv}")

if __name__=='__main__':
    main()
