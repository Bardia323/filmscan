"""Disassemble an exported function (or RVA) of a PE file. usage: dis.py file name|0xRVA [ninsn]"""
import sys, pefile, capstone
pe = pefile.PE(sys.argv[1]); base = pe.OPTIONAL_HEADER.ImageBase
tgt = sys.argv[2]; n = int(sys.argv[3]) if len(sys.argv) > 3 else 80
exp = {e.name.decode(): e.address for e in pe.DIRECTORY_ENTRY_EXPORT.symbols if e.name}
rva = int(tgt, 16) if tgt.startswith('0x') else exp[tgt]
imps = {}
for d in getattr(pe, 'DIRECTORY_ENTRY_IMPORT', []):
    for i in d.imports:
        imps[i.address] = f"{d.dll.decode()}!{i.name.decode() if i.name else i.ordinal}"
data = pe.get_memory_mapped_image()
md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
def cstr(r):
    try:
        s = data[r:r+60].split(b'\0')[0]
        if len(s) >= 4 and all(32 <= c < 127 for c in s): return s.decode()
    except Exception: pass
k = 0
for ins in md.disasm(data[rva:rva+n*16], base + rva):
    note = ''
    if 'rip' in ins.op_str:
        import re
        m = re.search(r'rip ([+-]) (0x[0-9a-f]+)', ins.op_str)
        if m:
            t = ins.address + ins.size + (int(m.group(2),16) if m.group(1)=='+' else -int(m.group(2),16))
            note = f"  ; -> rva {t-base:#x}"
            if t in imps: note += ' ' + imps[t]
            s = cstr(t - base)
            if s: note += f' "{s}"'
    print(f"{ins.address-base:#08x}  {ins.mnemonic:8s}{ins.op_str}{note}")
    k += 1
    if k >= n: break
