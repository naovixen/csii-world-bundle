"""Static locator for BeamNG engine internals (Phase 10 §8.9).

No disassembler: uses the exe's RTTI (vtables), assert/log strings, RIP-relative
LEA xrefs and .pdata function bounds. Anchors are strings and class names, so it
re-runs unchanged after a game update. Read-only; touches nothing in the install.
"""
import pefile, re, struct, bisect, sys
EXE = "G:/SteamLibrary/steamapps/common/BeamNG.drive/Bin64/BeamNG.drive.x64.exe"
p = pefile.PE(EXE, fast_load=True)
base = p.OPTIONAL_HEADER.ImageBase
data = open(EXE, "rb").read()
secs = {s.Name.decode().strip('\0'): s for s in p.sections}
def rva2off(rva):
    return p.get_offset_from_rva(rva)
def off2rva(off):
    return p.get_rva_from_offset(off)
text = secs['.text']; rdata = secs['.rdata']; pdata = secs['.pdata']
# --- function table from .pdata
pd = data[pdata.PointerToRawData:pdata.PointerToRawData+pdata.Misc_VirtualSize]
funcs = []
for i in range(0, len(pd)-11, 12):
    s,e,u = struct.unpack_from('<III', pd, i)
    if s == 0: break
    funcs.append((s,e))
funcs.sort()
starts = [f[0] for f in funcs]
def func_of(rva):
    i = bisect.bisect_right(starts, rva)-1
    if i >= 0 and funcs[i][0] <= rva < funcs[i][1]: return funcs[i]
    return None
print("functions in .pdata:", len(funcs))
# --- RIP-relative LEA/MOV xref index: (target rva) -> [insn rva]
tstart, tend = text.PointerToRawData, text.PointerToRawData + text.Misc_VirtualSize
xrefs = {}
tb = data[tstart:tend]
for m in re.finditer(rb'[\x48\x4c]\x8d[\x05\x0d\x15\x1d\x25\x2d\x35\x3d]', tb):
    o = m.start()
    disp = struct.unpack_from('<i', tb, o+3)[0]
    insn_rva = text.VirtualAddress + o
    tgt = insn_rva + 7 + disp
    xrefs.setdefault(tgt, []).append(insn_rva)
print("lea xrefs indexed:", len(xrefs))
def find_str(s):
    out = []
    i = data.find(s)
    while i != -1:
        out.append(off2rva(i)); i = data.find(s, i+1)
    return out
def report_string(label, s):
    print(f"\n[{label}] {s!r}")
    for rva in find_str(s):
        refs = xrefs.get(rva, [])
        print(f"  str @ {base+rva:x}  refs: " + ", ".join(f"{base+r:x} (fn {base+func_of(r)[0]:x}-{base+func_of(r)[1]:x})" if func_of(r) else f"{base+r:x}" for r in refs))
for lbl, s in [("collision reload", b"Physics collision reloading..."), ("collision done", b"Physics collision reloaded in"),
               ("shape resource create", b"Resource2<TSShape>::create"), ("Failed TSShape", b"Failed to create TSShape"),
               ("cdae ext", b".dts;.cdae"), ("cdae glob", b"*.cdae"), ("prefab load", b"Engine not initialized when calling Prefab::load"),
               ("prefab _loadFile", b"Prefab::_loadFile - failed loading prefab file"),
               ("tsShapeOldRead.cpp", b"gameengine\engine\graphics\ts\tsShapeOldRead.cpp"),
               ("assimp parity", b"assimp vs .cdae parity")]:
    report_string(lbl, s)
# --- RTTI: TypeDescriptor -> COL -> vtable
def vtable_for(name):
    s = b".?AV"+name+b"@engine@@\0"
    i = data.find(s)
    if i == -1: return None
    td_rva = off2rva(i) - 16           # TypeDescriptor starts 16 bytes before the name
    out = []
    # COL: sig(4) offset(4) cdOffset(4) pTypeDescriptor(4, image-relative) pClassDescriptor(4) pSelf(4)
    rb = data[rdata.PointerToRawData:rdata.PointerToRawData+rdata.Misc_VirtualSize]
    key = struct.pack('<I', td_rva)
    j = rb.find(key)
    while j != -1:
        col_off = j-12
        if col_off >= 0 and struct.unpack_from('<I', rb, col_off)[0] == 1:
            off_in_class = struct.unpack_from('<I', rb, col_off+4)[0]
            col_rva = rdata.VirtualAddress + col_off
            # vtable = address after a pointer to COL (8-byte VA)
            k = rb.find(struct.pack('<Q', base+col_rva))
            while k != -1:
                vt_rva = rdata.VirtualAddress + k + 8
                out.append((off_in_class, vt_rva))
                k = rb.find(struct.pack('<Q', base+col_rva), k+1)
        j = rb.find(key, j+1)
    return out
for cls in [b"TSStatic", b"Prefab", b"TSShapeLoader", b"ColladaShapeLoader", b"BeamNGCollision", b"PhysicsCollision", b"TSShapeInstance", b"SceneObject", b"SimObject"]:
    vts = vtable_for(cls)
    print(f"\n[vtable] {cls.decode()}: ", end="")
    if not vts: print("not found"); continue
    for off_in_class, vt in vts:
        # count slots until a non-.text pointer
        n = 0; slots = []
        while True:
            q = struct.unpack_from('<Q', data, rva2off(vt)+n*8)[0]
            r = q - base
            if not (text.VirtualAddress <= r < text.VirtualAddress+text.Misc_VirtualSize): break
            slots.append(r); n += 1
            if n > 400: break
        print(f"@+{off_in_class} vtable {base+vt:x} ({n} slots)", end="  ")
        if cls == b"TSStatic" and off_in_class == 0:
            print("\n   first 40 slots:", " ".join(f"{base+s:x}" for s in slots[:40]))
    print()

print("\n\n===== PART 2: call graph =====")
startset = set(starts)
calls = {}
for m in re.finditer(rb'\xe8', tb):
    o = m.start()
    if o+5 > len(tb): break
    disp = struct.unpack_from('<i', tb, o+1)[0]
    insn = text.VirtualAddress + o
    tgt = insn + 5 + disp
    if tgt in startset:
        calls.setdefault(tgt, []).append(insn)
def callers(rva):
    return sorted({func_of(c)[0] for c in calls.get(rva, []) if func_of(c)})
def slots(vt_rva, n):
    return [struct.unpack_from('<Q', data, rva2off(vt_rva)+i*8)[0]-base for i in range(n)]
ts = slots(0x141f6f3a0-base, 78); so = slots(0x141f25718-base, 78); pf = slots(0x141f64c28-base, 79)
print("TSStatic slots overriding SceneObject:")
for i,(a,b) in enumerate(zip(ts,so)):
    if a != b:
        f = func_of(a); size = f[1]-f[0] if f else 0
        c = calls.get(0x1402fc3a0-base, [])
        calls_create = any(f and f[0] <= ci < f[1] for ci in c)
        calls_coll = any(f and f[0] <= ci < f[1] for ci in calls.get(0x14019b3a0-base, []))
        print(f"  slot {i:2d}: {base+a:x} size {size:5d}" + ("  <-- calls Resource2<TSShape>::create" if calls_create else "") + ("  <-- calls collision rebuild" if calls_coll else ""))
print("\nPrefab slots overriding SceneObject:")
for i,(a,b) in enumerate(zip(pf,so)):
    if a != b:
        f = func_of(a); print(f"  slot {i:2d}: {base+a:x} size {f[1]-f[0] if f else 0}")
print("\ncallers of Resource2<TSShape>::create (1402fc3a0):", [f"{base+c:x}" for c in callers(0x1402fc3a0-base)])
print("callers of collision rebuild (14019b3a0):", [f"{base+c:x}" for c in callers(0x14019b3a0-base)])
print("callers of Prefab::_loadFile (1409c1660):", [f"{base+c:x}" for c in callers(0x1409c1660-base)])
# what does the collision rebuild's function reference (strings)?
def strings_in(fn):
    out=[]
    for tgt, insns in xrefs.items():
        if any(fn[0] <= i < fn[1] for i in insns):
            off = rva2off(tgt) if rdata.VirtualAddress <= tgt < rdata.VirtualAddress+rdata.Misc_VirtualSize else None
            if off:
                s = data[off:off+80].split(b'\0')[0]
                if len(s) >= 4 and all(0x20 <= ch < 0x7f for ch in s): out.append(s.decode())
    return sorted(set(out))
print("\nstrings referenced by collision rebuild fn:", strings_in((0x14019b3a0-base, 0x14019ba98-base)))
for c in callers(0x14019b3a0-base):
    f = func_of(c); print(f"  caller {base+c:x} strings:", strings_in(f)[:15])
print("\nstrings referenced by TSShape create fn:", strings_in((0x1402fc3a0-base, 0x1402fe6c4-base))[:40])
# callees of the collision rebuild function: what does it call (sizes) -- to see if it is one monolith
callees = sorted({(text.VirtualAddress + m.start() + 5 + struct.unpack_from('<i', tb, m.start()+1)[0]) for m in re.finditer(rb'\xe8', tb[0x14019b3a0-base-text.VirtualAddress:0x14019ba98-base-text.VirtualAddress])})
print("\ncollision rebuild callees:", [f"{base+c:x}({func_of(c)[1]-func_of(c)[0] if func_of(c) else '?'})" for c in callees if func_of(c)])

print("\n\n===== PART 3 =====")
wrappers = [0x1401bdcd0-base, 0x1401d04a0-base, 0x140963780-base]
for w in wrappers:
    f = func_of(w); print(f"wrapper {base+w:x} size {f[1]-f[0]} strings {strings_in(f)[:8]} callers:", [f"{base+c:x}" for c in callers(w)][:20])
# which TSStatic override reaches a wrapper within 2 hops
def callees_of(fn):
    seg = tb[fn[0]-text.VirtualAddress: fn[1]-text.VirtualAddress]
    out=set()
    for m in re.finditer(rb'\xe8', seg):
        o=m.start()
        if o+5>len(seg): break
        t = fn[0]+o+5+struct.unpack_from('<i', seg, o+1)[0]
        if t in startset: out.add(t)
    return out
for i,(a,b) in enumerate(zip(ts,so)):
    if a==b: continue
    f=func_of(a)
    if not f: continue
    c1 = callees_of(f); c2=set()
    for c in c1: c2 |= callees_of(func_of(c))
    hit1=[f"{base+x:x}" for x in c1 if x in wrappers]; hit2=[f"{base+x:x}" for x in c2 if x in wrappers]
    print(f"TSStatic slot {i:2d} {base+a:x}: strings {strings_in(f)[:6]} wrapper-1hop {hit1} 2hop {hit2}")
report_string("reloadCollision", b"reloadCollision")
report_string("generateCollision", b"generateCollision")
report_string("createShape", b"_createShape")
report_string("shapeName field", b"shapeName")
