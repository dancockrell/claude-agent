"""
Agent runner — the bridge between Claude and this machine.

Claude cannot reach this machine's network, and cannot type into a terminal.
So instead: this script sits in a loop watching a folder that Claude *can*
write to (Downloads, which is shared with the session). Claude drops a job
file in; this runs it against the local services (ComfyUI on 8188, edge-tts)
and writes the result back into the same folder for Claude to collect.

Start it once by double-clicking RUN-AGENT.bat. Leave it running.
Close the window to stop it. It touches nothing outside its own folder.
"""
import os, sys, json, time, glob, base64, traceback, subprocess, urllib.request, urllib.error
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE     = os.path.dirname(os.path.abspath(__file__))
JOBS     = os.path.join(HERE, "jobs")       # Claude writes here
RESULTS  = os.path.join(HERE, "results")    # Claude reads here
DONE     = os.path.join(HERE, "done")
for d in (JOBS, RESULTS, DONE): os.makedirs(d, exist_ok=True)

COMFY_PORTS = [8188, 8189, 8000, 7860]
HEARTBEAT   = os.path.join(RESULTS, "_heartbeat.json")

def log(*a):
    msg = " ".join(str(x) for x in a)
    print(time.strftime("[%H:%M:%S]"), msg, flush=True)

# ---------------------------------------------------------------- comfy
def comfy_port():
    for p in COMFY_PORTS:
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/system_stats" % p, timeout=3)
            return p
        except Exception:
            pass
    return None

def comfy_post(port, path, payload):
    data = json.dumps(payload).encode()
    req  = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), data=data,
                                  headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=120).read())

def comfy_get(port, path):
    return json.loads(urllib.request.urlopen(
        "http://127.0.0.1:%d%s" % (port, path), timeout=60).read())

def comfy_image(port, fn, sub, typ):
    from urllib.parse import urlencode
    q = urlencode({"filename": fn, "subfolder": sub, "type": typ})
    return urllib.request.urlopen(
        "http://127.0.0.1:%d/view?%s" % (port, q), timeout=120).read()

def run_comfy(job):
    """job: {kind:'comfy', graphs:[{name, prompt(graph)}], timeout}"""
    port = comfy_port()
    if not port:
        return {"ok": False, "error": "ComfyUI is not answering on any of %s" % COMFY_PORTS}
    out = {"ok": True, "port": port, "images": {}}
    for g in job["graphs"]:
        name = g["name"]
        try:
            log("queue", name)
            r  = comfy_post(port, "/prompt", {"prompt": g["prompt"]})
            pid = r["prompt_id"]
            t0  = time.time()
            limit = job.get("timeout", 900)
            hist = None
            while time.time() - t0 < limit:
                time.sleep(2.0)
                h = comfy_get(port, "/history/%s" % pid)
                if pid in h and h[pid].get("outputs"):
                    hist = h[pid]; break
            if not hist:
                out["images"][name] = {"error": "timed out after %ss" % limit}
                log("TIMEOUT", name); continue
            got = []
            for node in hist["outputs"].values():
                for im in node.get("images", []):
                    b = comfy_image(port, im["filename"], im.get("subfolder", ""), im.get("type", "output"))
                    got.append(base64.b64encode(b).decode())
            out["images"][name] = {"b64": got, "seconds": round(time.time() - t0, 1)}
            log("done", name, len(got), "image(s)", round(time.time() - t0, 1), "s")
        except Exception as e:
            out["images"][name] = {"error": "%s: %s" % (type(e).__name__, e)}
            log("ERROR", name, e)
    return out

# ---------------------------------------------------------------- tts
def run_tts(job):
    """job: {kind:'tts', clips:[{id,text,voice,rate,pitch}]}"""
    try:
        import edge_tts  # noqa
    except ImportError:
        log("installing edge-tts…")
        subprocess.run([sys.executable, "-m", "pip", "install", "edge-tts", "--quiet"],
                       capture_output=True)
        try:
            import edge_tts  # noqa
        except ImportError:
            return {"ok": False, "error": "could not install edge-tts"}
    import asyncio, edge_tts

    async def one(c):
        words = []
        comm = edge_tts.Communicate(c["text"], c["voice"],
                                    rate=c.get("rate", "+0%"), pitch=c.get("pitch", "+0Hz"))
        buf = bytearray()
        async for ch in comm.stream():
            if ch["type"] == "audio":
                buf.extend(ch["data"])
            elif ch["type"] == "WordBoundary":
                words.append([ch["offset"] // 10000, ch["text"]])
        return c["id"], base64.b64encode(bytes(buf)).decode(), words

    async def all_of(clips):
        res = {}
        for c in clips:
            try:
                cid, b64, w = await one(c)
                res[cid] = {"mp3": b64, "words": w}
                log("spoke", cid, len(b64) // 1024, "KB")
            except Exception as e:
                res[c["id"]] = {"error": str(e)}
                log("ERROR", c["id"], e)
        return res

    return {"ok": True, "clips": asyncio.run(all_of(job["clips"]))}

def run_voices(job):
    """job: {kind:'voices'} — list every installed neural voice"""
    try:
        import edge_tts
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", "edge-tts", "--quiet"],
                       capture_output=True)
        import edge_tts
    import asyncio
    vs = asyncio.run(edge_tts.list_voices())
    return {"ok": True, "voices": [
        {"n": v["ShortName"], "g": v.get("Gender"), "loc": v.get("Locale"),
         "tags": (v.get("VoiceTag") or {})} for v in vs]}

def run_probe(job):
    port = comfy_port()
    info = {"ok": True, "comfy_port": port, "python": sys.version.split()[0], "cwd": HERE}
    if port:
        try:
            info["stats"] = comfy_get(port, "/system_stats")
            info["models"] = comfy_get(port, "/object_info/CheckpointLoaderSimple") \
                ["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0]
        except Exception as e:
            info["stats_error"] = str(e)
    return info


# ---------------------------------------------------------------- chatterbox
# A local neural TTS that is markedly better than edge-tts on long passages.
# It lives in its own virtual environment inside this folder so nothing is
# installed into the machine's Anaconda, and deleting claude-agent/ removes it
# completely.
VENV = os.path.join(HERE, ".venv-tts")
def venv_py():
    p = os.path.join(VENV, "Scripts", "python.exe")
    return p if os.path.exists(p) else os.path.join(VENV, "bin", "python")

def run_setup(job):
    """job: {kind:'setup'} — build the TTS environment. Slow, once.

    Rebuilt from scratch each time it is asked for: a venv interrupted
    half way through has no pip in it, and every later step then fails
    with a confusing 'No module named pip'."""
    steps = []
    def sh(args, label, timeout=7200):
        log(label, "...")
        kw = {}
        if os.name == "nt":
            kw["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        try:
            r = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                               encoding="utf-8", errors="replace", **kw)
            code, tail = r.returncode, ((r.stdout or "")[-1200:] + (r.stderr or "")[-2500:])
        except Exception as e:
            code, tail = -1, "%s: %s" % (type(e).__name__, e)
        steps.append({"step": label, "code": code, "tail": tail})
        log(label, "->", code)
        return code == 0

    # a fresh venv every time — --clear wipes a half-built one
    if not sh([sys.executable, "-m", "venv", "--clear", VENV], "create venv", 900):
        return {"ok": False, "steps": steps}
    py = venv_py()
    if not os.path.exists(py):
        steps.append({"step": "locate venv python", "code": -1, "tail": "missing " + py})
        return {"ok": False, "steps": steps}

    # a venv can come up without pip; bootstrap it before anything else
    if not sh([py, "-m", "pip", "--version"], "check pip", 180):
        sh([py, "-m", "ensurepip", "--upgrade", "--default-pip"], "bootstrap pip", 600)
    sh([py, "-m", "pip", "install", "--upgrade", "pip", "wheel", "setuptools"], "upgrade pip")

    sh([py, "-c", "import sys;print(sys.version)"], "venv python version", 120)
    # CUDA build of torch first, so pip does not resolve to the CPU wheel
    sh([py, "-m", "pip", "install", "torch", "torchaudio",
        "--index-url", "https://download.pytorch.org/whl/cu124"], "install torch (cuda)")
    ok = sh([py, "-m", "pip", "install", "chatterbox-tts"], "install chatterbox-tts")
    if not ok:
        # some ML wheels lag the newest Python; say so plainly rather than
        # leaving a wall of pip output to read
        sh([py, "-m", "pip", "install", "chatterbox-tts", "--no-deps"],
           "install chatterbox-tts (no deps)")
    r = subprocess.run([py, "-c",
        "import torch;print('torch',torch.__version__,'cuda',torch.cuda.is_available());"
        "import chatterbox;print('chatterbox ok')"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    steps.append({"step": "verify", "code": r.returncode,
                  "tail": (r.stdout or "") + (r.stderr or "")[-1200:]})
    return {"ok": r.returncode == 0, "steps": steps, "python": py}

CHATTER_WORKER = r"""
import sys, json, base64, io, os, wave, struct
payload = json.load(open(sys.argv[1], encoding='utf-8'))
import torch, torchaudio
from chatterbox.tts import ChatterboxTTS
dev = 'cuda' if torch.cuda.is_available() else 'cpu'
model = ChatterboxTTS.from_pretrained(device=dev)
out = {}
for c in payload['clips']:
    try:
        kw = {}
        if c.get('ref') and os.path.exists(c['ref']): kw['audio_prompt_path'] = c['ref']
        if c.get('exaggeration') is not None: kw['exaggeration'] = c['exaggeration']
        if c.get('cfg_weight')  is not None: kw['cfg_weight']  = c['cfg_weight']
        if c.get('temperature') is not None: kw['temperature'] = c['temperature']
        wav = model.generate(c['text'], **kw)
        buf = io.BytesIO()
        torchaudio.save(buf, wav.cpu(), model.sr, format='wav')
        raw = buf.getvalue()
        # crude but useful word timings: find speech runs by energy, then hand
        # the words out across them in proportion to how long each word is
        import numpy as np
        w = wave.open(io.BytesIO(raw)); n = w.getnframes(); sr = w.getframerate()
        a = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float32)/32768.0
        if w.getnchannels() > 1: a = a.reshape(-1, w.getnchannels()).mean(axis=1)
        hop = max(1, sr//100)
        env = np.sqrt(np.convolve(a*a, np.ones(hop)/hop, mode='same'))[::hop]
        thr = max(env.max()*0.06, 1e-4)
        voiced = env > thr
        runs, i = [], 0
        while i < len(voiced):
            if voiced[i]:
                j = i
                while j < len(voiced) and voiced[j]: j += 1
                if (j-i) >= 4: runs.append((i*10, j*10))     # ms
                i = j
            else: i += 1
        if not runs: runs = [(0, int(len(a)/sr*1000))]
        words = c['text'].split()
        total = sum(len(x) for x in words) or 1
        span = sum(e-s for s,e in runs)
        times, acc, ri, within = [], 0, 0, 0
        for x in words:
            share = (len(x)/total)*span
            while ri < len(runs)-1 and within >= (runs[ri][1]-runs[ri][0]):
                within = 0; ri += 1
            times.append([int(runs[ri][0]+within), x])
            within += share
        out[c['id']] = {'wav': base64.b64encode(raw).decode(), 'words': times,
                        'sr': model.sr}
        print('ok', c['id'], len(raw)//1024, 'KB', flush=True)
    except Exception as e:
        out[c['id']] = {'error': '%s: %s' % (type(e).__name__, e)}
        print('ERR', c['id'], e, flush=True)
json.dump({'clips': out}, open(sys.argv[2], 'w'))
"""

def run_chatter(job):
    """job: {kind:'chatter', clips:[{id,text,ref?,exaggeration?,cfg_weight?}]}"""
    py = venv_py()
    if not os.path.exists(py):
        return {"ok": False, "error": "TTS environment not built — send a 'setup' job first"}
    work = os.path.join(HERE, "_chatter_worker.py")
    open(work, "w", encoding="utf-8").write(CHATTER_WORKER)
    inp = os.path.join(HERE, "_chatter_in.json")
    outp = os.path.join(HERE, "_chatter_out.json")
    json.dump(job, open(inp, "w", encoding="utf-8"))
    log("chatterbox: %d clips" % len(job.get("clips", [])))
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    r = subprocess.run([py, "-u", work, inp, outp], capture_output=True, text=True,
                       timeout=job.get("timeout", 7200),
                       encoding="utf-8", errors="replace", **kw)
    if not os.path.exists(outp):
        return {"ok": False, "error": ((r.stdout or "") + (r.stderr or ""))[-4000:]}
    res = json.load(open(outp, encoding="utf-8"))
    res["ok"] = True
    res["log"] = (r.stdout or "")[-2000:]
    try: os.remove(outp)
    except Exception: pass
    return res

def run_writeref(job):
    """job: {kind:'writeref', name, b64} — drop a reference wav for cloning."""
    d = os.path.join(HERE, "refs"); os.makedirs(d, exist_ok=True)
    p = os.path.join(d, job["name"])
    open(p, "wb").write(base64.b64decode(job["b64"]))
    return {"ok": True, "path": p, "bytes": os.path.getsize(p)}

KINDS = {"comfy": run_comfy, "setup": run_setup,
         "chatter": run_chatter, "writeref": run_writeref, "tts": run_tts, "voices": run_voices, "probe": run_probe}

def main():
    log("agent runner up. watching", JOBS)
    log("ComfyUI on port:", comfy_port())
    beat = 0
    while True:
        try:
            beat += 1
            if beat % 10 == 0 or beat == 1:
                json.dump({"t": time.time(), "iso": time.strftime("%Y-%m-%d %H:%M:%S"),
                           "comfy": comfy_port()}, open(HEARTBEAT, "w"))
            for path in sorted(glob.glob(os.path.join(JOBS, "*.json"))):
                name = os.path.basename(path)
                # wait until the file has stopped growing (it is being copied in)
                s1 = os.path.getsize(path); time.sleep(0.7)
                if os.path.getsize(path) != s1: continue
                try:
                    job = json.load(open(path, encoding="utf-8"))
                except Exception:
                    time.sleep(1.0)
                    continue
                log("JOB", name, job.get("kind"))
                t0 = time.time()
                try:
                    res = KINDS.get(job.get("kind"), lambda j: {"ok": False,
                          "error": "unknown kind %r" % j.get("kind")})(job)
                except Exception:
                    res = {"ok": False, "error": traceback.format_exc()[-3000:]}
                res["seconds"] = round(time.time() - t0, 1)
                tmp = os.path.join(RESULTS, name + ".part")
                json.dump(res, open(tmp, "w", encoding="utf-8"))
                os.replace(tmp, os.path.join(RESULTS, name))
                try: os.replace(path, os.path.join(DONE, name))
                except Exception: os.remove(path)
                log("RESULT", name, res.get("ok"), res["seconds"], "s")
            time.sleep(1.5)
        except KeyboardInterrupt:
            log("stopped."); return
        except Exception:
            log("loop error:", traceback.format_exc()[-800:]); time.sleep(3)

if __name__ == "__main__":
    main()
