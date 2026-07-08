#!/usr/bin/env python3
"""Generic protobuf wire-format decoder (no .proto needed).
Recursively decodes; heuristically shows nested messages, strings, ints, floats.
"""
import sys, struct

def read_varint(b, i):
    shift=0; result=0
    while True:
        byte=b[i]; i+=1
        result |= (byte & 0x7f) << shift
        if not (byte & 0x80): break
        shift+=7
    return result, i

def try_decode(b, indent=0, maxdepth=8):
    out=[]
    i=0; n=len(b)
    pad="  "*indent
    while i < n:
        try:
            tag, i = read_varint(b, i)
        except Exception:
            out.append(f"{pad}<trailing {n-i} bytes>")
            break
        field = tag >> 3
        wt = tag & 7
        if wt==0:
            try: val,i=read_varint(b,i)
            except: out.append(f"{pad}<bad varint>"); break
            sval=struct.unpack('<q',struct.pack('<Q',val))[0] if val>=(1<<63) else val
            out.append(f"{pad}#{field} varint={val}"+(f" (signed {sval})" if val>0x7fffffff else ""))
        elif wt==5:
            if i+4>n: break
            raw=b[i:i+4]; i+=4
            f=struct.unpack('<f',raw)[0]; u=struct.unpack('<I',raw)[0]
            out.append(f"{pad}#{field} i32 float={f:.6g} u32={u}")
        elif wt==1:
            if i+8>n: break
            raw=b[i:i+8]; i+=8
            d=struct.unpack('<d',raw)[0]; u=struct.unpack('<Q',raw)[0]
            out.append(f"{pad}#{field} i64 double={d:.8g} u64={u}")
        elif wt==2:
            try: ln,i=read_varint(b,i)
            except: break
            if i+ln>n: out.append(f"{pad}#{field} <len {ln} overflow>"); break
            sub=b[i:i+ln]; i+=ln
            # decide: nested message, string, or packed floats
            printable=all(32<=c<127 or c in (9,10,13) for c in sub) and len(sub)>0
            nested=None
            if indent<maxdepth and len(sub)>=2:
                nested=recurse_ok(sub)
            if nested and not (printable and len(sub)<40):
                out.append(f"{pad}#{field} msg[{ln}]:")
                out.append(try_decode(sub,indent+1,maxdepth))
            elif printable:
                out.append(f"{pad}#{field} str[{ln}]={sub.decode('utf-8','replace')!r}")
            elif ln%4==0 and ln<=64:
                fl=struct.unpack('<%df'%(ln//4),sub)
                out.append(f"{pad}#{field} floats[{ln//4}]="+",".join(f"{x:.5g}" for x in fl))
            else:
                out.append(f"{pad}#{field} bytes[{ln}]={sub[:32].hex()}"+("..." if ln>32 else ""))
        else:
            out.append(f"{pad}<unknown wiretype {wt} at field {field}>")
            break
    return "\n".join(x for x in out if x)

def recurse_ok(b):
    """Check if bytes plausibly decode as protobuf (all fields consume cleanly)."""
    i=0;n=len(b);fields=0
    while i<n:
        try: tag,i=read_varint(b,i)
        except: return False
        wt=tag&7; field=tag>>3
        if field==0: return False
        if wt==0:
            try: _,i=read_varint(b,i)
            except: return False
        elif wt==5: i+=4
        elif wt==1: i+=8
        elif wt==2:
            try: ln,i=read_varint(b,i)
            except: return False
            i+=ln
            if i>n: return False
        else: return False
        fields+=1
    return i==n and fields>0

if __name__=='__main__':
    data=open(sys.argv[1],'rb').read()
    print(try_decode(data))
