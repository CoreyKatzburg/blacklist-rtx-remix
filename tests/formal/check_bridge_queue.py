"""Model check of the Remix bridge data queue (BridgeDataQueue.tla) with TLC. No game, no GPU, about 15 s.

  python check_bridge_queue.py            run every check; exit 0 only if each result is the expected one
                                          and the covered source code is unchanged since the last sync
  python check_bridge_queue.py --resync   record the covered code as it is now (only after you confirmed
                                          that the model still describes it)

Each run's full TLC output (with the step-by-step counterexample when a property is violated) is saved
in out\\<run name>.txt. See D:\\BlacklistOverhaul\\.agents\\skills\\blacklist-graphics-verification\\features\\bridge-data-queue.md.
"""
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
BRIDGE = HERE.parents[1] / "src" / "dxvk-remix" / "bridge" / "src"
JAR = HERE / "tools" / "tla2tools.jar"
JAR_URL = "https://github.com/tlaplus/tlaplus/releases/download/v1.7.4/tla2tools.jar"
JAR_SHA256 = "936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88"
COVERED = HERE / "covered.json"
OUT = HERE / "out"

# name, config, invariants, expected result, text that must appear in the last state of the counterexample, note
RUNS = [
    ("reply", "reply.cfg", "NoFreeze NoOverwrite InSync NoFatal", "holds", None,
     "server -> game replies and both module channels"),
    ("broken-reply-writer-waits", "broken_reply_wait.cfg", "NoFreeze", "violated", "sdp = -1",
     "must fail: the 2026-11-12 freeze put back"),
    ("broken-reply-no-request-response", "broken_reply_async.cfg", "NoOverwrite", "violated", None,
     "must fail: replies written faster than they are read"),
    ("device-F1", "device.cfg", "NoOverwrite", "violated", None,
     "OPEN FINDING F1: a blob that ends exactly on the last slot overwrites unread data at slot 0"),
    ("device-F2", "device.cfg", "NoFreeze", "violated", "batchStart = 0",
     "OPEN FINDING F2: a command that starts at slot 0 waits for a wake-up that never comes"),
    ("device-F3", "device_f1fixed.cfg", "NoOverwrite", "violated", None,
     "OPEN FINDING F3: an exactly full queue is treated as empty (shown with F1 fixed in the model)"),
]

# The code each part of the model describes: file, first line of the region, first line after it (None = whole file).
REGIONS = [
    ("util/util_bridgecommand.cpp", "DECL_BRIDGE_FUNC(void, syncDataQueue", "DECL_BRIDGE_FUNC(Header, pop_front)",
     "Send, SendDoubleOverflow, SendOverwrite, Wake1, Wake2, EnterWait, Overwrites, TooBig; constant WriterMayWait"),
    ("util/util_bridgecommand.cpp", "DECL_COMMAND_FUNC(,Command,", "template class Bridge<BridgeId::Module>;",
     "BeginCmd, EndCmd, EndRetry, PushHeader; constants UidFirst, StageSetters"),
    ("util/util_bridgecommand.h", None, None, "Need (send_data memUsed), RPull (get_data clears serverResetPosRequired)"),
    ("util/util_circularqueue.h", None, None, "Inc"),
    ("util/util_circularbuffer.h", None, None, "BlobStart, ItemSlots, ItemEnd"),
    ("util/util_atomiccircularqueue.h", None, None, "stg / pub, MaxStaged, the queue-full branch of EndCmd"),
    ("util/util_ipcchannel.h", None, None, "Init (serverDataPos = -1, clientDataExpectedPos = -1, semaphore 0 of 1)"),
    ("server/main.cpp", "// Ensure the data position between client and server is in sync after processing the command",
     "const auto count = DeviceBridge::end_read_data();", "RPublish; constant ReaderPublishes"),
    ("client/d3d9_device_base.cpp", "ClientMessage c(m_ex ? Commands::IDirect3D9Ex_CreateDeviceEx",
     "const auto header = DeviceBridge::pop_front();", "constant HandshakeFirst"),
]


def region_hash(rel, start, end):
    lines = (BRIDGE / rel).read_text(encoding="utf-8", errors="replace").splitlines()
    if start is not None:
        first = next((i for i, l in enumerate(lines) if start in l), None)
        last = next((i for i, l in enumerate(lines) if first is not None and i > first and end in l), None)
        if first is None or last is None:
            return None
        lines = lines[first:last]
    text = "\n".join(l.rstrip() for l in lines)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def current_regions():
    return [{"file": rel, "from": start, "to": end, "model": model, "sha256": region_hash(rel, start, end)}
            for rel, start, end, model in REGIONS]


def git_head():
    try:
        return subprocess.run(["git", "-C", str(BRIDGE), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    except OSError:
        return "unknown"


def sync_problems():
    if not COVERED.exists():
        return ["covered.json is missing: run with --resync after confirming the model describes the code"]
    recorded = {(r["file"], r["from"]): r for r in json.loads(COVERED.read_text(encoding="utf-8"))["regions"]}
    problems = []
    for r in current_regions():
        old = recorded.get((r["file"], r["from"]))
        where = f"{r['file']}" + (f" (from \"{r['from']}\")" if r["from"] else "")
        if r["sha256"] is None:
            problems.append(f"{where}: region not found (the code was moved or renamed). Model parts: {r['model']}")
        elif old is None or old["sha256"] != r["sha256"]:
            problems.append(f"{where}: changed since the last sync. Model parts to review: {r['model']}")
    return problems


def ensure_jar():
    if not JAR.exists():
        JAR.parent.mkdir(parents=True, exist_ok=True)
        print(f"downloading {JAR_URL}")
        urllib.request.urlretrieve(JAR_URL, JAR)
    digest = hashlib.sha256(JAR.read_bytes()).hexdigest()
    if digest != JAR_SHA256:
        raise SystemExit(f"{JAR} has sha256 {digest}, expected {JAR_SHA256}. Delete it and run again.")


def run_tlc(name, cfg, invariants, work):
    text = re.sub(r"(?m)^INVARIANTS.*$", "INVARIANTS " + invariants, (HERE / cfg).read_text(encoding="utf-8"))
    one = work / f"{name}.cfg"
    one.write_text(text, encoding="utf-8")
    cmd = ["java", "-XX:+UseParallelGC", "-cp", str(JAR), "tlc2.TLC", "-config", str(one), "-workers", "4",
           "-metadir", str(work / f"{name}-states"), "BridgeDataQueue.tla"]
    out = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True).stdout
    (OUT / f"{name}.txt").write_text(out, encoding="utf-8")
    violated = re.search(r"Error: Invariant (\w+) is violated", out)
    if violated:
        result = "violated"
    elif "Model checking completed. No error has been found." in out:
        result = "holds"
    else:
        result = "error"          # parse error, deadlock without an invariant violation, TLC crash
    last_state = out.split("\nState ")[-1] if violated else ""
    states = re.search(r"(\d+) distinct states found", out)
    return result, violated.group(1) if violated else "", last_state, states.group(1) if states else "?"


def main():
    if "--resync" in sys.argv:
        regions = current_regions()
        missing = [r["file"] for r in regions if r["sha256"] is None]
        if missing:
            raise SystemExit(f"region not found in: {missing}. Fix REGIONS in this script first.")
        COVERED.write_text(json.dumps({"dxvk_remix_head": git_head(), "regions": regions}, indent=2) + "\n", encoding="utf-8")
        print(f"recorded {len(regions)} code regions in {COVERED} (dxvk-remix HEAD {git_head()[:10]}, plus uncommitted changes)")
        return 0

    ensure_jar()
    OUT.mkdir(exist_ok=True)
    failures = []
    for problem in sync_problems():
        failures.append("code sync: " + problem)
    work = Path(tempfile.mkdtemp(prefix="bridge-queue-tlc-"))
    try:
        for name, cfg, invariants, expected, final_text, note in RUNS:
            result, which, last_state, states = run_tlc(name, cfg, invariants, work)
            ok = result == expected and (not final_text or final_text in last_state)
            shown = f"{result}{' ' + which if which else ''}"
            print(f"{'ok  ' if ok else 'FAIL'} {name:34} {shown:22} expected {expected:9} {states:>7} states  {note}")
            if not ok:
                why = f"{name}: got '{shown}', expected '{expected}'"
                if result == expected and final_text:
                    why = f"{name}: violated as expected but not in the known way (last state lacks '{final_text}')"
                failures.append(why + f". See {OUT / (name + '.txt')}")
    finally:
        shutil.rmtree(work, ignore_errors=True)

    open_findings = [note for *_, expected, _f, note in RUNS if note.startswith("OPEN FINDING")]
    if open_findings:
        print("\nOpen findings in the code (expected failures above, not yet fixed):")
        for note in open_findings:
            print("  " + note)
    if failures:
        print("\ncheck_bridge_queue: FAIL")
        for f in failures:
            print("  " + f)
        return 1
    print("\ncheck_bridge_queue: PASS (every run gave its expected result; covered code unchanged since the last sync)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
