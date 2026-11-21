"""Map SCANAPI_GetFunctionTable slots -> function RVA -> debug strings referenced."""
import sys, re, pefile, capstone
pe = pefile.PE(sys.argv[1]); base = pe.OPTIONAL_HEADER.ImageBase
data = pe.get_memory_mapped_image()
exp = {e.name.decode(): e.address for e in pe.DIRECTORY_ENTRY_EXPORT.symbols if e.name}
md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
def cstr(r):
    s = data[r:r+120].split(b'\0')[0]
    if len(s) >= 4 and all(32 <= c < 127 for c in s): return s.decode()
def rip_target(ins):
    m = re.search(r'rip ([+-]) (0x[0-9a-f]+)', ins.op_str)
    if m: return ins.address + ins.size + (int(m.group(2),16) if m.group(1)=='+' else -int(m.group(2),16)) - base
rva = exp['SCANAPI_GetFunctionTable']; slots = {}; last = None; tbl0 = None
for ins in md.disasm(data[rva:rva+0x400], base+rva):
    t = rip_target(ins)
    if ins.mnemonic == 'lea' and t is not None: last = t
    elif ins.mnemonic == 'mov' and ins.op_str.startswith('qword ptr [rip') and last is not None:
        slots[t] = last; last = None
    elif ins.mnemonic == 'ret': break
tbl0 = min(slots)
for addr in sorted(slots):
    f = slots[addr]; strs = []; n = 0
    for ins in md.disasm(data[f:f+0x3000], base+f):
        n += 1
        t = rip_target(ins)
        if t is not None and ins.mnemonic == 'lea':
            s = cstr(t)
            if s and s not in strs: strs.append(s)
        if ins.mnemonic == 'ret' or n > 1500: break
    print(f"slot {(addr-tbl0)//8:2d} @+{addr-tbl0:#05x} fn {f:#x} insns {n:4d} :: {' | '.join(strs[:4])}")
