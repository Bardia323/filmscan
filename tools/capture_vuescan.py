"""Record how VueScan drives the Plustek driver, to learn the exposure / multi-exposure parameters.

Starts VueScan itself, hooks
Scndrvu_x64.drv with Frida and appends every driver call to out/capture.jsonl until VueScan exits.

If a vuescan.ini is given, a COPY is placed next to vuescan.exe for the session and removed afterwards,
so saved settings are never modified.
"""
import json
import os
import shutil
import sys
import time

import frida

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# usage: capture_vuescan.py <path to vuescan.exe> [vuescan.ini to copy in for the session]
EXE = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join("C:" + os.sep, "Program Files", "VueScan", "vuescan.exe")
EXE_DIR = os.path.dirname(EXE)
INI_SRC = os.path.abspath(sys.argv[2]) if len(sys.argv) > 2 else ""
INI_TMP = os.path.join(EXE_DIR, "vuescan.ini")
OUT = os.path.join(HERE, "out", "capture.jsonl")

JS = r"""
const NAMES = {0:'Register',1:'Unregister',2:'Lock',3:'GetDeviceCapability',4:'GetDeviceStatus',5:'GetImageLayout',
  6:'StartScan',7:'Read',8:'StopScan',9:'GetButtonStatus',10:'TestUnitReady',11:'Precalibration',12:'LedControl',
  14:'LampControl',17:'ReadCalibration',19:'WarmUpTime',20:'LampOffTime',21:'ReadOneImageLine',22:'Reset',
  24:'DoCalibration',25:'GetCalibrationInfo',27:'GetExtCapability',30:'ScannerEventCallback',33:'ReadDuplex',
  34:'GetNewExtCapability',39:'EndScan',40:'CalibratInterval'};
function hx(p, n) {
  try { if (p.isNull()) return null;
    const b = new Uint8Array(p.readByteArray(n)); let s = '';
    for (let i = 0; i < b.length; i++) s += ('0' + b[i].toString(16)).slice(-2);
    return s;
  } catch (e) { return null; }
}
function install(drv) {
  const base = drv.base;
  send({t: 'module', base: base.toString(), path: drv.path});
  const G_PARAM = base.add(0x1e7e50), G_PROP = base.add(0x1e7ea8);
  let lastG = '';
  function globals(where) {
    const p = hx(G_PARAM, 0x51), q = hx(G_PROP, 0x3f);
    if (p + q !== lastG) { lastG = p + q; send({t: 'globals', where: where, param: p, prop: q}); }
  }
  // ---- old command interface ----
  let lastCmd = -1, rep = 0;
  Interceptor.attach(drv.getExportByName('ScannerDriverEntry'), {
    onEnter(args) {
      this.c = args[0];
      this.cmd = this.c.readU16();
      this.quiet = (this.cmd === lastCmd && ++rep > 3);
      if (this.cmd !== lastCmd) { if (rep > 3) send({t: 'sde_repeat', cmd: lastCmd, times: rep}); rep = 0; lastCmd = this.cmd; }
      let dp = ptr(0);
      try { dp = this.c.add(6).readPointer(); } catch (e) {}
      this.dp = dp;
      if (!this.quiet) send({t: 'sde_in', cmd: this.cmd, head: hx(this.c, 0x20), data: hx(dp, 0xc0)});
    },
    onLeave(ret) {
      if (!this.quiet) send({t: 'sde_out', cmd: this.cmd, head: hx(this.c, 0x20), data: hx(this.dp, 0xc0)});
      globals('sde ' + this.cmd);
    }
  });
  // ---- new ScanAPI table ----
  const getTable = new NativeFunction(drv.getExportByName('SCANAPI_GetFunctionTable'), 'pointer', []);
  const tbl = getTable();
  const seen = {};
  let lastSlot = -1, srep = 0;
  for (let i = 0; i < 46; i++) {
    let f; try { f = tbl.add(i * 8).readPointer(); } catch (e) { continue; }
    if (f.isNull() || f.compare(base) < 0 || f.compare(base.add(drv.size)) >= 0) continue;
    const key = f.toString(); if (seen[key]) continue; seen[key] = 1;
    const slot = i, name = NAMES[i] || ('slot' + i);
    Interceptor.attach(f, {
      onEnter(args) {
        this.quiet = (slot === lastSlot && ++srep > 3);
        if (slot !== lastSlot) { if (srep > 3) send({t: 'api_repeat', slot: lastSlot, times: srep}); srep = 0; lastSlot = slot; }
        if (this.quiet) return;
        const m = {t: 'api_in', slot: slot, name: name, a0: args[0].toString(), a1: args[1].toString(), a2: args[2].toString()};
        if (slot === 6) { m.param = hx(args[0], 0x51); try { m.prop = hx(args[0].readPointer(), 0x3f); } catch (e) {} }
        if (slot === 5) m.prop = hx(args[0], 0x3f);
        if (slot === 0) m.ver = hx(args[0], 8);
        if (slot === 4) m.status = hx(args[1], 16);
        send(m);
      },
      onLeave(ret) { if (!this.quiet) send({t: 'api_out', slot: slot, name: name, ret: ret.toInt32()}); if (slot === 6) globals('api StartScan'); }
    });
  }
  // ---- internal "set scan parameters" (rdx = param block, r8 = property block) ----
  Interceptor.attach(base.add(0x2c8c0), {
    onEnter(args) { send({t: 'setparam', param: hx(args[1], 0x51), prop: hx(args[2], 0x3f)}); }
  });
  send({t: 'ready'});
}
const timer = setInterval(function () {
  const m = Process.findModuleByName('Scndrvu_x64.drv');
  if (m) { clearInterval(timer); try { install(m); } catch (e) { send({t: 'error', msg: '' + e + ' ' + e.stack}); } }
}, 200);
"""


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    placed_ini = False
    if INI_SRC and os.path.exists(INI_SRC) and not os.path.exists(INI_TMP):
        shutil.copy2(INI_SRC, INI_TMP)
        placed_ini = True
    out = open(OUT, "a", encoding="utf-8")
    out.write(json.dumps({"t": "session", "time": time.strftime("%Y-%m-%d %H:%M:%S")}) + "\n")
    done = {"v": False}

    def on_message(msg, data):
        if msg["type"] == "send":
            p = msg["payload"]
            p["ts"] = round(time.time(), 2)
            out.write(json.dumps(p) + "\n")
            out.flush()
            if p.get("t") in ("module", "ready", "error"):
                print(p, flush=True)
        else:
            out.write(json.dumps({"t": "frida_error", "msg": msg}) + "\n")
            out.flush()
            print("frida error:", msg.get("description"), flush=True)

    try:
        pid = frida.spawn([EXE], cwd=EXE_DIR)
        session = frida.attach(pid)
        session.on("detached", lambda *a: done.update(v=True))
        script = session.create_script(JS)
        script.on("message", on_message)
        script.load()
        frida.resume(pid)
        print("VueScan started, pid", pid, "- recording to", OUT, flush=True)
        while not done["v"]:
            time.sleep(0.5)
        print("VueScan closed, capture finished", flush=True)
    finally:
        out.close()
        if placed_ini:
            for _ in range(20):                      # VueScan rewrites the ini on exit; wait, then remove our copy
                try:
                    os.remove(INI_TMP)
                    break
                except OSError:
                    time.sleep(0.5)
        for junk in ("vuescan.log",):
            try:
                os.remove(os.path.join(EXE_DIR, junk))
            except OSError:
                pass


if __name__ == "__main__":
    sys.exit(main())
