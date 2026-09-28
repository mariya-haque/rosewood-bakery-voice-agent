"""Simulated callers for the Rosewood agent, over the real Voice Agent API.

Windows' built-in speech synthesis voices the caller, the audio streams to the
agent in real time with silence between lines like a live microphone, and a
few rules answer the agent's questions (number, name, size, pickup time,
confirmations). Everything the agent does is real: transcription, tools,
the orders board, and the webhook review after hang-up.

    cd backend
    .venv/Scripts/python -m pip install websockets
    .venv/Scripts/python ../scripts/sim_call.py                  # every scenario
    .venv/Scripts/python ../scripts/sim_call.py regular allergy  # some of them

Needs the backend running on localhost:8000 and the agent published against
its public URL. Windows only, for System.Speech. Each call is billed as a
normal session. Results land in scripts/sim_results.json."""
import asyncio
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
import wave

import websockets

BACKEND = "http://localhost:8000"
RATE = 24000
CHUNK = 960  # 20 ms of 16-bit mono at 24 kHz

SCENARIOS = {
    "new_customer": {
        "voice": "Microsoft Zira Desktop",
        "open": "Hi, do you have black forest cake?",
        "agenda": ["Okay, then I'd like a red velvet cake instead.",
                   "No, that's all, thank you."],
        "name": "Maya Chen", "phone": "4 1 5, 5 5 5, 0 1 8 8",
        "time": "Tomorrow at 4 pm.", "size": "Two kilos, please.",
        "message": "Please write Happy Birthday Mira.",
    },
    "regular": {
        "voice": "Microsoft Zira Desktop",
        "open": "Hi, I'd like to place an order please.",
        "agenda": ["Yes, my usual please. Six butter croissants.", "No, that's everything."],
        "name": "Dana Whitfield", "phone": "4 1 5, 5 5 5, 0 1 4 2",
        "time": "Saturday at 10 am.", "size": "Six please.", "message": "No message.",
    },
    "allergy": {
        "voice": "Microsoft David Desktop",
        "open": "Hi. Are your almond croissants safe for someone with a tree nut allergy?",
        "agenda": ["Yes, please have the owner call me.", "No, that's all. Thanks."],
        "name": "Marcus Lee", "phone": "4 1 5, 5 5 5, 0 1 7 7",
        "time": "Tomorrow at noon.", "size": "One.", "message": "No message.",
    },
    "gluten_free": {
        "voice": "Microsoft David Desktop",
        "open": "Hey, do you have any gluten free cupcakes?",
        "agenda": ["Yes, please have someone call me back about gluten free options.",
                   "No, that's it."],
        "name": "Tom Reyes", "phone": "4 1 5, 5 5 5, 0 1 9 3",
        "time": "Friday at 3 pm.", "size": "A dozen.", "message": "No message.",
    },
}


def tts(text: str, voice: str) -> bytes:
    path = os.path.join(tempfile.gettempdir(), f"sim_{abs(hash(text))}.wav")
    ps = (
        "Add-Type -AssemblyName System.Speech;"
        "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        f"$s.SelectVoice('{voice}'); $s.Rate=0;"
        "$f=New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(24000,"
        "[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,"
        "[System.Speech.AudioFormat.AudioChannel]::Mono);"
        f"$s.SetOutputToWaveFile('{path}',$f); $s.Speak({json.dumps(text)}); $s.Dispose()"
    )
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, capture_output=True)
    with wave.open(path) as w:
        return w.readframes(w.getnframes())


def respond(agent_text: str, p: dict, state: dict):
    t = agent_text.lower()
    if re.search(r"order (number )?is|order number", t) and re.search(r"\d", t) and "?" not in t[-3:]:
        state["done"] = True
        return "Great, thank you. Bye."
    if state.get("done_soon") and ("bye" in t or "have a" in t or "see you" in t):
        return None
    if any(k in t for k in ("is that right", "is that correct", "did i get", "shall i", "should i",
                            "go ahead", "sound right", "sound good", "confirm", "is that ok",
                            "does that work", "that work for you", "all correct", "want me to")):
        return "Yes, that's right."
    if "anything else" in t:
        state["done_soon"] = True
        return p["agenda"][-1]
    if "number" in t and "order number" not in t:
        return p["phone"]
    if "name" in t:
        return p["name"]
    if any(k in t for k in ("kilo", "size", "how big", "how many")):
        return p["size"]
    if any(k in t for k in ("message", "write", "inscription")):
        return p["message"]
    if any(k in t for k in ("when", "pick up", "pickup", "collect", "what time", "what day")):
        return p["time"]
    if state["agenda"]:
        return state["agenda"].pop(0)
    if "bye" in t or "day" in t[-20:]:
        return None
    return "Yes."


async def call(name: str) -> dict:
    p = SCENARIOS[name]
    token = json.loads(urllib.request.urlopen(BACKEND + "/api/token").read())["token"]
    agent = json.loads(urllib.request.urlopen(BACKEND + "/api/agent").read())
    log = {"scenario": name, "events": []}
    outgoing = bytearray()
    st = {"speaking": False, "play_until": 0.0, "agent_lines": [], "session": None,
          "ready": False, "ended": False, "last_activity": time.time()}

    def note(kind, **kw):
        kw["t"] = round(time.time() - t0, 2)
        log["events"].append({"kind": kind, **kw})
        if kind in ("caller", "agent", "tool"):
            who = {"caller": "CALLER", "agent": "AGENT ", "tool": "  tool"}[kind]
            print(f"[{kw['t']:6.1f}] {who}: {kw.get('text') or kw.get('name') + ' ' + json.dumps(kw.get('args'))}", flush=True)

    t0 = time.time()
    async with websockets.connect(f"wss://agents.assemblyai.com/v1/ws?token={token}", max_size=None, open_timeout=45, ping_interval=20, ping_timeout=90) as ws:
        await ws.send(json.dumps({"type": "session.update", "session": {"agent_id": agent["id"]}}))

        async def sender():
            next_at = time.time()
            while not st["ended"]:
                if st["ready"]:
                    if outgoing:
                        chunk = bytes(outgoing[:CHUNK]); del outgoing[:CHUNK]
                        chunk = chunk.ljust(CHUNK, b"\0")
                    else:
                        chunk = b"\0" * CHUNK
                    await ws.send(json.dumps({"type": "input.audio", "audio": base64.b64encode(chunk).decode()}))
                next_at += CHUNK / 2 / RATE
                await asyncio.sleep(max(0, next_at - time.time()))

        async def receiver():
            async for raw in ws:
                m = json.loads(raw)
                k = m.get("type")
                if k == "session.ready":
                    st["ready"] = True; st["session"] = m.get("session_id"); log["session_id"] = st["session"]
                elif k == "reply.started":
                    st["speaking"] = True; st["last_activity"] = time.time()
                elif k == "reply.audio":
                    n = len(base64.b64decode(m["data"])) / 2 / RATE
                    st["play_until"] = max(st["play_until"], time.time()) + n
                    st["last_activity"] = time.time()
                elif k == "reply.done":
                    st["speaking"] = False; st["last_activity"] = time.time()
                elif k == "transcript.agent":
                    st["agent_lines"].append(m.get("text", "")); note("agent", text=m.get("text", ""))
                elif k == "transcript.user":
                    note("heard", text=m.get("text", ""))
                    print(f"         (heard: {m.get('text','')})", flush=True)
                elif k == "tool.call":
                    note("tool", name=m.get("name"), args=m.get("arguments"))
                elif k == "session.error":
                    note("error", text=m.get("message")); print("ERROR", m, flush=True)
                elif k == "session.ended":
                    st["ended"] = True
                    return

        send_task = asyncio.create_task(sender())
        recv_task = asyncio.create_task(receiver())
        state = {"agenda": list(p["agenda"])}
        seen = 0
        turns = 0
        # Wait for the greeting to finish.
        while not st["ended"] and turns < 18:
            deadline = time.time() + 30
            while time.time() < deadline:
                await asyncio.sleep(0.2)
                quiet = (not st["speaking"] and not outgoing and time.time() > st["play_until"] + 0.8
                         and time.time() > st["last_activity"] + 1.2)
                if quiet and len(st["agent_lines"]) > seen:
                    break
            if len(st["agent_lines"]) == seen and turns > 0:
                note("stall", text="agent said nothing for 30s")
                print("  (agent silent for 30s)", flush=True)
            last = st["agent_lines"][-1] if len(st["agent_lines"]) > seen else ""
            seen = len(st["agent_lines"])
            say = p["open"] if turns == 0 else respond(last, p, state)
            if say is None:
                break
            note("caller", text=say)
            outgoing.extend(tts(say, p["voice"]))
            turns += 1
            if state.get("done") and turns > 1:
                await asyncio.sleep(6)
                break
        await asyncio.sleep(2)
        await ws.send(json.dumps({"type": "session.end"}))
        await asyncio.sleep(2)
        st["ended"] = True
        send_task.cancel()
        recv_task.cancel()
    log["duration"] = round(time.time() - t0, 1)
    return log


async def main(names):
    results = []
    for n in names:
        print(f"\n===== {n} =====", flush=True)
        try:
            results.append(await asyncio.wait_for(call(n), 300))
        except Exception as e:  # noqa: BLE001
            print("FAILED", n, repr(e), flush=True)
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sim_results.json")
    json.dump(results, open(out, "w"), indent=1)
    print("\nsaved", out)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:] or list(SCENARIOS)))
