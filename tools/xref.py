"""List instructions with rip-relative refs into [lo,hi). usage: xref.py file lo hi"""
import sys, re, pefile, capstone
pe = pefile.PE(sys.argv[1]); base = pe.OPTIONAL_HEADER.ImageBase
lo, hi = int(sys.argv[2],16), int(sys.argv[3],16)
data = pe.get_memory_mapped_image()
md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64); md.skipdata = True
pat = re.compile(r'rip ([+-]) (0x[0-9a-f]+)')
for s in pe.sections:
    if not s.Characteristics & 0x20000000: continue
    a = s.VirtualAddress
    for ins in md.disasm(data[a:a+s.Misc_VirtualSize], base+a):
        m = pat.search(ins.op_str)
        if not m: continue
        t = ins.address + ins.size + (int(m.group(2),16) if m.group(1)=='+' else -int(m.group(2),16)) - base
        if lo <= t < hi:
            print(f"{ins.address-base:#08x}  +{t-lo:#04x}  {ins.mnemonic} {ins.op_str}")
