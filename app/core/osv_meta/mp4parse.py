#!/usr/bin/env python3
"""Minimal MP4 box parser: extract per-sample bytes for each trak.
Reads stsd(codec tag), stsz(sizes), stco/co64(chunk offsets), stsc(sample->chunk).
Usage: python3 mp4parse.py <file>
"""
import struct, sys

def read_boxes(data, start, end, depth=0, path=""):
    boxes = []
    off = start
    while off + 8 <= end:
        size = struct.unpack(">I", data[off:off+4])[0]
        typ = data[off+4:off+8]
        hdr = 8
        if size == 1:
            size = struct.unpack(">Q", data[off+8:off+16])[0]
            hdr = 16
        elif size == 0:
            size = end - off
        boxes.append((typ, off, size, hdr))
        off += size
    return boxes

CONTAINERS = {b'moov', b'trak', b'mdia', b'minf', b'stbl', b'edts', b'udta', b'meta'}

def walk(data, start, end, cb, depth=0):
    for typ, off, size, hdr in read_boxes(data, start, end):
        cb(typ, off, size, hdr, depth)
        if typ in CONTAINERS:
            s = off+hdr
            if typ == b'meta':
                s = off+hdr+4  # meta has version/flags
            walk(data, s, off+size, cb, depth+1)

def parse_trak(data, trak_off, trak_size):
    info = {'codec': None, 'handler': None, 'sizes': [], 'chunk_offsets': [], 'stsc': [], 'timescale': None, 'stts':[]}
    def cb(typ, off, size, hdr, depth):
        body = off+hdr
        if typ == b'hdlr':
            info['handler'] = data[body+8:body+12]
        elif typ == b'stsd':
            # version/flags(4) entrycount(4) then entry: size(4) format(4)
            info['codec'] = data[body+12:body+16]
        elif typ == b'stsz':
            ver_sample_size = struct.unpack(">I", data[body+4:body+8])[0]
            count = struct.unpack(">I", data[body+8:body+12])[0]
            if ver_sample_size != 0:
                info['sizes'] = [ver_sample_size]*count
            else:
                arr = struct.unpack(">%dI"%count, data[body+12:body+12+4*count])
                info['sizes'] = list(arr)
        elif typ == b'stco':
            count = struct.unpack(">I", data[body+4:body+8])[0]
            info['chunk_offsets'] = list(struct.unpack(">%dI"%count, data[body+8:body+8+4*count]))
        elif typ == b'co64':
            count = struct.unpack(">I", data[body+4:body+8])[0]
            info['chunk_offsets'] = list(struct.unpack(">%dQ"%count, data[body+8:body+8+8*count]))
        elif typ == b'stsc':
            count = struct.unpack(">I", data[body+4:body+8])[0]
            for i in range(count):
                fc, spc, sdi = struct.unpack(">III", data[body+8+12*i:body+8+12*i+12])
                info['stsc'].append((fc, spc, sdi))
        elif typ == b'mdhd':
            ver = data[body]
            if ver==1:
                info['timescale']=struct.unpack(">I",data[body+20:body+24])[0]
            else:
                info['timescale']=struct.unpack(">I",data[body+12:body+16])[0]
        elif typ == b'stts':
            count = struct.unpack(">I", data[body+4:body+8])[0]
            for i in range(count):
                cnt,dur=struct.unpack(">II",data[body+8+8*i:body+8+8*i+8])
                info['stts'].append((cnt,dur))
    walk(data, trak_off+8, trak_off+trak_size, cb)
    return info

def sample_offsets(info):
    """Return list of (offset, size) per sample using stsc/stco/stsz."""
    sizes = info['sizes']
    chunks = info['chunk_offsets']
    stsc = info['stsc']
    # expand stsc to per-chunk samples-per-chunk
    n_chunks = len(chunks)
    spc_per_chunk = [0]*n_chunks
    for i,(first,spc,sdi) in enumerate(stsc):
        last = stsc[i+1][0]-1 if i+1<len(stsc) else n_chunks
        for c in range(first, last+1):
            if 1<=c<=n_chunks:
                spc_per_chunk[c-1]=spc
    result=[]
    s_idx=0
    for c in range(n_chunks):
        off=chunks[c]
        for _ in range(spc_per_chunk[c]):
            if s_idx>=len(sizes): break
            result.append((off, sizes[s_idx]))
            off+=sizes[s_idx]
            s_idx+=1
    return result

if __name__=='__main__':
    fn=sys.argv[1]
    data=open(fn,'rb').read()
    traks=[]
    def top_cb(typ, off, size, hdr, depth):
        pass
    # find all trak boxes
    trak_list=[]
    def find_cb(typ,off,size,hdr,depth):
        if typ==b'trak':
            trak_list.append((off,size))
    walk(data,0,len(data),find_cb)
    for i,(toff,tsize) in enumerate(trak_list):
        info=parse_trak(data,toff,tsize)
        so=sample_offsets(info)
        print(f"trak#{i}: codec={info['codec']} handler={info['handler']} timescale={info['timescale']} nsamples={len(info['sizes'])} sizes_uniq={sorted(set(info['sizes']))[:10]} first5offs={so[:3]}")
