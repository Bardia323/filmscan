"""For xref lines (addr at col 1) matching a +off tag, show the following N instructions compactly."""
import sys, pefile, capstone
pe = pefile.PE(sys.argv[1]); base = pe.OPTIONAL_HEADER.ImageBase; data = pe.get_memory_mapped_image()
md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
tag = sys.argv[3]; n = int(sys.argv[4])
for line in open(sys.argv[2]):
    p = line.split()
    if len(p) < 2 or p[1] != tag: continue
    a = int(p[0], 16); out = []
    for i, ins in enumerate(md.disasm(data[a:a+80], base+a)):
        if i > n: break
        out.append(f"{ins.mnemonic} {ins.op_str}".replace('dword ptr ', '').replace('qword ptr ', ''))
    print(f"{a:#08x}: " + " ; ".join(out))
