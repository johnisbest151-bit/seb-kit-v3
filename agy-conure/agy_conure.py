#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agy-conure - a lightweight, single-file re-implementation of the Conure overlay
that uses your Antigravity CLI (`agy`) as the AI brain. No Electron, no exe:
just this script + a few pip packages.

Run:   pythonw agy_conure.py        (or: python agy_conure.py  to see logs)

Features (parity with Conure 1.1.0):
  * frameless always-on-top dark overlay, draggable, resizable, opacity, font scale
  * Private mode (hidden from screen-share / recordings via SetWindowDisplayAffinity)
  * click-through mode, hide / show, move / resize via hotkeys
  * live transcription of call audio (WASAPI loopback) + your microphone
    using the local sherpa-onnx Zipformer model Conure already shipped (+ punctuation)
  * question detection + auto-answer, manual Answer, click-a-line-to-answer
  * Screen: screenshot -> agy looks at it and solves / summarises
  * free-form ask box, regenerate, answer history as context, copy answer
  * interview / meeting / sales / support modes, concise / detailed / STAR styles,
    resume / job description / notes / documents (pdf, docx, txt) as context
  * session transcript + answers saved as Markdown, automatic post-session notes
  * global hotkeys, tray icon, launch-at-login toggle, settings window
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

APP_NAME = "agy-conure"
APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, "data")
WORK_DIR = os.path.join(DATA_DIR, "work")
IMG_DIR = os.path.join(WORK_DIR, "images")
SESSIONS_DIR = os.path.join(DATA_DIR, "sessions")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
LOG_FILE = os.path.join(DATA_DIR, "agy-conure.log")
for _d in (DATA_DIR, WORK_DIR, IMG_DIR, SESSIONS_DIR):
    os.makedirs(_d, exist_ok=True)

LOCALAPPDATA = os.environ.get("LOCALAPPDATA", os.path.expanduser("~\\AppData\\Local"))
AGY_EXE = os.environ.get("AGY_EXE") or os.path.join(LOCALAPPDATA, "agy", "bin", "agy.exe")
MODELS_DIR_CANDIDATES = [
    os.path.join(APP_DIR, "models"),
    os.path.join(LOCALAPPDATA, "Programs", "Conure", "resources", "models"),
]

# Give AGY a hidden console instead of no console: its updater children inherit it,
# so Windows does not flash a visible console window during background checks.
AGY_STARTUPINFO = None
AGY_CREATION_FLAGS = 0
if os.name == "nt":
    AGY_STARTUPINFO = subprocess.STARTUPINFO()
    AGY_STARTUPINFO.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    AGY_STARTUPINFO.wShowWindow = subprocess.SW_HIDE
    AGY_CREATION_FLAGS = subprocess.CREATE_NEW_CONSOLE


def log(*a):
    line = "[%s] %s" % (datetime.now().strftime("%H:%M:%S"), " ".join(str(x) for x in a))
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ----------------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------------
DEFAULTS = {
    "overlay": {"width": 540, "height": 720, "x": None, "y": None, "opacity": 0.97, "privateMode": True,
                "clickThrough": False, "fontScale": 1.0, "showTranscript": True, "transcriptHeight": 190},
    "hotkeys": {
        "toggleVisibility": "ctrl+alt+h", "answer": "ctrl+alt+enter", "screenshotAnswer": "ctrl+alt+s",
        "clear": "ctrl+alt+c", "regenerate": "ctrl+alt+r", "toggleClickThrough": "ctrl+alt+m",
        "togglePrivate": "ctrl+alt+p", "toggleListening": "ctrl+alt+space", "toggleAutoAnswer": "ctrl+alt+a",
        "moveLeft": "ctrl+alt+left", "moveRight": "ctrl+alt+right", "moveUp": "ctrl+alt+up", "moveDown": "ctrl+alt+down",
        "growOverlay": "ctrl+alt+=", "shrinkOverlay": "ctrl+alt+-", "scrollUp": "ctrl+alt+page up", "scrollDown": "ctrl+alt+page down",
    },
    "audio": {"captureSystem": True, "captureMic": True, "micDevice": "default"},
    "stt": {"punctuation": True, "numThreads": 2, "endpointSilenceMs": 800},
    "llm": {"model": "gemini-3.8-flash-low", "warm": True},
    "assistant": {"autoAnswer": True, "answerStyle": "concise", "mode": "interview", "responseLanguage": "auto",
                  "contextWindowUtterances": 8, "historyTurns": 2, "customInstructions": "",
                  "includeTranscriptContext": True, "minQuestionWords": 3},
    "context": {"resumeText": "", "jobDescription": "", "notes": "", "documents": []},
    "session": {"saveTranscripts": True, "saveDir": "", "autoNotes": True},
    "general": {"launchAtLogin": False, "showTray": True},
}


def deep_merge(a, b):
    out = json.loads(json.dumps(a))
    for k, v in (b or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Settings:
    def __init__(self):
        data = {}
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            pass
        self.d = deep_merge(DEFAULTS, data)

    def save(self):
        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.d, f, indent=2)
        os.replace(tmp, SETTINGS_FILE)

    def __getitem__(self, k):
        return self.d[k]


# ----------------------------------------------------------------------------
# Antigravity CLI driver (persistent stream-json processes, pre-warmed)
# ----------------------------------------------------------------------------
class AgyProc:
    def __init__(self, model):
        self.model = model
        self.created = time.time()
        self.ready = threading.Event()
        self.dead = False
        self.error = None
        self.handlers = None
        self.lock = threading.Lock()
        args = [AGY_EXE, "--input-format", "stream-json", "--output-format", "stream-json", "--model", model, "-p="]
        self.p = subprocess.Popen(args, cwd=WORK_DIR, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, creationflags=AGY_CREATION_FLAGS, startupinfo=AGY_STARTUPINFO, bufsize=0)
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stderr(self):
        try:
            for line in iter(self.p.stderr.readline, b""):
                s = line.decode("utf-8", "replace").strip()
                if s:
                    log("agy[%s] stderr: %s" % (self.p.pid, s[:300]))
        except Exception:
            pass

    def _read_stdout(self):
        try:
            for raw in iter(self.p.stdout.readline, b""):
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    j = json.loads(line)
                except Exception:
                    continue
                ev = j.get("event")
                if ev == "init":
                    self.ready.set()
                    continue
                with self.lock:
                    h = self.handlers
                if not h:
                    continue
                if ev == "step_update":
                    su = j.get("step_update") or {}
                    if su.get("step_type") == "agent_response" and su.get("text_delta"):
                        h["on_delta"](su["text_delta"])
                elif ev == "result":
                    r = j.get("result") or {}
                    with self.lock:
                        self.handlers = None
                    if r.get("status") == "SUCCESS":
                        h["on_done"](r)
                    else:
                        h["on_error"](r.get("error") or ("agy status %s" % r.get("status")))
        except Exception as e:
            log("agy reader error", e)
        finally:
            self.dead = True
            if not self.ready.is_set():
                self.error = "agy exited during start-up (is agy logged in? run `agy` once in a terminal)"
                self.ready.set()
            with self.lock:
                h = self.handlers
                self.handlers = None
            if h:
                h["on_error"]("agy process exited unexpectedly")

    def ask(self, prompt, on_delta, on_done, on_error):
        with self.lock:
            self.handlers = {"on_delta": on_delta, "on_done": on_done, "on_error": on_error}
        msg = json.dumps({"event": "user", "message": {"role": "user", "content": prompt}}) + "\n"
        try:
            self.p.stdin.write(msg.encode("utf-8"))
            self.p.stdin.flush()
        except Exception as e:
            with self.lock:
                self.handlers = None
            on_error("could not talk to agy: %s" % e)

    def kill(self):
        if self.dead:
            return
        try:
            self.p.stdin.close()
        except Exception:
            pass

        def _k():
            time.sleep(1.5)
            try:
                self.p.kill()
            except Exception:
                pass
        threading.Thread(target=_k, daemon=True).start()


class AgyClient:
    """Keeps one warm agy process for the active model so answers start in ~3 s."""
    MAX_AGE = 15 * 60

    def __init__(self, settings):
        self.settings = settings
        self.pool = {}
        self.lock = threading.Lock()
        self.models = []
        threading.Thread(target=self._janitor, daemon=True).start()

    def list_models(self, force=False):
        if self.models and not force:
            return self.models
        try:
            out = subprocess.run([AGY_EXE, "models"], cwd=WORK_DIR, capture_output=True, text=True, timeout=40,
                                 creationflags=AGY_CREATION_FLAGS, startupinfo=AGY_STARTUPINFO).stdout
            ms = []
            for line in out.splitlines():
                m = re.match(r"^([\w.\-]+)\t(.*)$", line)
                if m:
                    ms.append((m.group(1), m.group(2)))
            if ms:
                self.models = ms
        except Exception as e:
            log("agy models failed", e)
        return self.models

    def warm(self, model=None):
        model = model or self.settings["llm"]["model"]
        if not self.settings["llm"].get("warm", True):
            return
        with self.lock:
            arr = [p for p in self.pool.get(model, []) if not p.dead]
            self.pool[model] = arr
            if arr:
                return
            try:
                arr.append(AgyProc(model))
                log("warming agy", model)
            except Exception as e:
                log("agy spawn failed", e)

    def take(self, model):
        with self.lock:
            arr = self.pool.get(model, [])
            proc = None
            while arr and proc is None:
                c = arr.pop(0)
                if not c.dead:
                    proc = c
        if proc is None:
            proc = AgyProc(model)
        threading.Thread(target=lambda: (time.sleep(0.2), self.warm(model)), daemon=True).start()
        return proc

    def _janitor(self):
        while True:
            time.sleep(60)
            with self.lock:
                for model, arr in list(self.pool.items()):
                    for c in list(arr):
                        if c.dead or time.time() - c.created > self.MAX_AGE:
                            arr.remove(c)
                            c.kill()
            self.warm()

    def shutdown(self):
        with self.lock:
            for arr in self.pool.values():
                for c in arr:
                    c.kill()
            self.pool.clear()


# ----------------------------------------------------------------------------
# Prompts (ported from Conure's llm/prompts.js) + flattening for agy
# ----------------------------------------------------------------------------
MODE_ROLE = {
    "interview": "You are the candidate's silent real-time interview copilot. The candidate is in a live job interview and reads your text while speaking. Answer AS the candidate, in first person, so the text can be spoken aloud naturally.",
    "meeting": "You are a real-time meeting copilot. The user is in a live meeting and reads your text while speaking. Give the user the most useful thing to say or know right now.",
    "sales": "You are a real-time sales-call copilot. The user is on a live customer call. Suggest what to say next: handle objections, clarify value, ask good discovery questions.",
    "support": "You are a real-time customer-support copilot. The user is on a live support call. Suggest accurate, calm, step-by-step guidance the user can relay.",
}
STYLE_RULES = {
    "concise": "Format: start with one direct sentence in bold, then up to 4 short bullets; add a closing line only if useful. Keep under ~100 words unless technical detail or code is required.",
    "detailed": "Format: a direct answer first, then well-structured bullets with specifics, examples and trade-offs. Aim for 150-300 words; use code blocks for code.",
    "star": "For behavioral questions use the STAR structure with bold labels (Situation, Task, Action, Result), one or two sentences each, with concrete metrics where plausible from the provided context. For non-behavioral questions answer directly in 3-6 bullets.",
}


def truncate(s, n):
    s = s or ""
    return s if len(s) <= n else s[:n] + "..."


def build_system_prompt(st):
    a = st["assistant"]
    parts = [MODE_ROLE.get(a["mode"], MODE_ROLE["interview"]), STYLE_RULES.get(a["answerStyle"], STYLE_RULES["concise"])]
    parts.append("\n".join([
        "Rules:",
        "- Be fast and specific. Lead with the answer; never restate the question.",
        "- You receive the conversation so far as context and then ONE latest message. Answer only the latest message; use the context (and your earlier answers in this chat) to understand follow-ups, fragments and references like \"that\" or \"the second one\".",
        "- Transcripts come from speech recognition and may contain errors or missing punctuation; infer the intended question. If a question is ambiguous, answer the most likely interpretation and add one short note on the alternative.",
        "- For coding or system-design questions: give the approach in 2-3 bullets, then clean, idiomatic code in a fenced block with the language tag, then complexity/trade-offs in one line.",
        "- Use the candidate context (resume, job description, notes) to personalize with real details; never invent employers, degrees or metrics that contradict the context. If context is missing, keep claims generic and plausible and mark them as placeholders in [brackets].",
        "- Do not mention that you are an AI, do not add disclaimers, do not apologize.",
        "- Plain Markdown only (bold, bullets, code). No tables.",
    ]))
    if a.get("responseLanguage") and a["responseLanguage"] != "auto":
        parts.append("Always answer in %s." % a["responseLanguage"])
    else:
        parts.append("Answer in the same language as the question.")
    if (a.get("customInstructions") or "").strip():
        parts.append("Additional user instructions:\n" + truncate(a["customInstructions"].strip(), 3000))
    ctx = st["context"]
    cp = []
    if (ctx.get("resumeText") or "").strip():
        cp.append("## Candidate resume / background\n" + truncate(ctx["resumeText"].strip(), 12000))
    if (ctx.get("jobDescription") or "").strip():
        cp.append("## Job description / meeting goal\n" + truncate(ctx["jobDescription"].strip(), 8000))
    if (ctx.get("notes") or "").strip():
        cp.append("## User notes / talking points\n" + truncate(ctx["notes"].strip(), 6000))
    for d in ctx.get("documents") or []:
        if d.get("text", "").strip():
            cp.append("## Document: %s\n%s" % (d.get("name"), truncate(d["text"].strip(), 10000)))
    if cp:
        parts.append("# Context\n" + truncate("\n\n".join(cp), 40000))
    return "\n\n".join(parts)


def fmt_transcript(utts, max_items=None):
    items = utts[-max_items:] if max_items else utts
    return "\n".join("[%s] %s" % ("Me" if u["source"] == "you" else "Other party", u["text"].strip()) for u in items)


def history_turns(history):
    msgs = []
    for h in history or []:
        if not (h.get("text") or "").strip():
            continue
        q = (h.get("question") or "").strip()
        if h.get("kind") == "screenshot":
            label = "[Earlier, about a screenshot of my screen] " + (q or "What is on my screen?")
        elif h.get("kind") == "auto":
            label = "[Earlier question from the other party] " + q
        else:
            label = "[Earlier question / request] " + q
        msgs.append(("user", label))
        msgs.append(("assistant", truncate(h["text"].strip(), 1500)))
    return msgs


def context_block(ctx_utts, st):
    a = st["assistant"]
    if not a.get("includeTranscriptContext", True) or not ctx_utts:
        return []
    return ["Conversation so far - context only, do not answer these lines (oldest first):",
            fmt_transcript(ctx_utts, a.get("contextWindowUtterances", 12)), ""]


def build_answer_messages(question, ctx_utts, history, st, manual_prompt="", regenerate=False):
    lines = context_block(ctx_utts, st)
    if manual_prompt.strip():
        lines.append("My request now: " + manual_prompt.strip())
        if question.strip():
            lines.append('(For reference, the latest message from the other party was: "%s")' % question.strip())
    else:
        lines.append("Latest message from the other party - THIS is the question to answer now:")
        lines.append('"%s"' % question.strip())
        lines.append("If it is a fragment or a follow-up, interpret it using the conversation context above.")
    if regenerate:
        lines.append("Give a different, better answer than your previous one; do not repeat its wording.")
    lines += ["", "Write what I should say, in the required format."]
    return history_turns(history) + [("user", "\n".join(lines))]


def build_screenshot_messages(ctx_utts, history, st, extra=""):
    lines = context_block(ctx_utts, st)
    lines.append(extra.strip() or "This is a screenshot of my screen right now. If it shows a coding problem, quiz, or technical question: solve it with the full solution (code in a fenced block with language tag) and a short explanation. If it shows a slide or document: summarize the key point and suggest what I should say. Ignore my own notes/overlay if visible.")
    return history_turns(history) + [("user", "\n".join(lines))]


def build_summary_messages(utts, answers, mode, duration):
    transcript = fmt_transcript(utts)
    qa = "\n\n".join("Q: %s\nSuggested: %s" % (x.get("question"), truncate(x.get("text"), 600)) for x in answers if x.get("text"))
    content = "\n\n".join([
        "Create concise post-session notes in Markdown for a %s that lasted %s." % (mode, duration),
        "Sections: **Summary** (3-5 bullets), **Questions asked** (bullet list), **Key points & commitments**, **Action items / next steps**, **Follow-up suggestions** (how I could have answered better, 2-3 bullets).",
        "Transcript:", truncate(transcript, 60000),
        ("\nSuggested answers during the session:\n" + truncate(qa, 20000)) if qa else "",
    ])
    return [("user", content)]


def flatten_for_agy(system, messages, image_path=None):
    out = ["You are answering through a chat bridge. IMPORTANT RULES:",
           "- Reply ONLY with the final answer text for the user (no preamble, no tool narration).",
           "- Do NOT run commands, edit files, browse the web, or ask permission. Do not ask clarifying questions."]
    if image_path:
        out.append("- An image is attached as a file at: %s - use the view_file tool to look at it FIRST, then answer." % image_path)
    out.append("")
    if system:
        out += ["=== SYSTEM INSTRUCTIONS ===", system, ""]
    if len(messages) > 1:
        out.append("=== CONVERSATION SO FAR ===")
        for role, text in messages[:-1]:
            out.append("%s: %s\n" % ("Assistant" if role == "assistant" else "User", text))
    out += ["=== LATEST USER MESSAGE (respond to this) ===", messages[-1][1]]
    return "\n".join(out)


# ----------------------------------------------------------------------------
# Question detection (ported from question-detector.js)
# ----------------------------------------------------------------------------
WH_START = re.compile(r"^(what|what's|whats|why|how|how's|when|where|which|who|who's|whom|whose)\b")
AUX_START = re.compile(r"^(can|could|would|will|shall|should|do|does|did|is|are|was|were|have|has|had|may|might|am|isn't|aren't|don't|doesn't|didn't|won't|wouldn't|couldn't|shouldn't)\b")
IMPERATIVE_START = re.compile(r"^(tell me|tell us|walk me|walk us|take me|describe|explain|elaborate|give me|give us|share|talk about|talk me|say more|name|list|define|compare|outline|summarize|summarise|show me|help me understand|let's talk about|lets talk about|so tell|please tell|please explain|please describe)\b")
QUESTION_PHRASES = ["tell me about", "walk me through", "can you", "could you", "would you", "have you ever", "have you worked", "what do you think", "how would you", "how do you", "how did you", "why do you", "why did you", "what is your", "what are your", "what's your", "what was", "what were", "do you have", "do you know", "are you familiar", "experience with", "give me an example", "give an example", "describe a time", "tell me a time", "a time when", "any questions", "your thoughts on", "your experience", "your approach", "how about", "what about", "explain to me", "difference between", "is it possible", "would it be", "what if", "how many", "how much", "how long", "how often", "biggest", "strength", "weakness", "why should we", "why do you want", "where do you see", "introduce yourself", "about yourself", "your background"]
LEADING_FILLERS = re.compile(r"^(so|okay|ok|alright|all right|well|um|uh|hmm|right|great|cool|now|and|but|then|yeah|yes|no|like|also|next|first|second|finally|perfect|awesome|good)[,.\s]+", re.I)
QUESTION_THRESHOLD = 0.45


def question_score(text):
    raw = re.sub(r"\s+", " ", str(text or "").lower().replace('"', "")).strip()
    if not raw:
        return 0.0
    words = len(raw.split())
    score = 0.0
    if raw.endswith("?"):
        score += 0.75
    t = re.sub(r"[?!.]+$", "", raw)
    for _ in range(4):
        t2 = LEADING_FILLERS.sub("", t)
        if t2 == t:
            break
        t = t2
    if WH_START.search(t):
        score += 0.6
    elif AUX_START.search(t):
        score += 0.45
    elif IMPERATIVE_START.search(t):
        score += 0.55
    for p in QUESTION_PHRASES:
        if p in t:
            score += 0.35
            break
    if score == 0 and re.search(r"\b(what|why|how|when|where|which|who)\b", t) and re.search(r"\b(you|your)\b", t):
        score += 0.3
    if words < 2:
        score *= 0.3
    elif words < 4:
        score *= 0.75
    if words > 80:
        score *= 0.85
    return min(1.0, score)


# ----------------------------------------------------------------------------
# Local speech recognition (sherpa-onnx) + audio capture (WASAPI loopback / mic)
# ----------------------------------------------------------------------------
def find_models_dir():
    for d in MODELS_DIR_CANDIDATES:
        if os.path.isfile(os.path.join(d, "zipformer-en-20M-int8", "tokens.txt")):
            return d
    return None


class LocalSTT:
    def __init__(self, settings, on_partial, on_final, on_status):
        import numpy as np
        import sherpa_onnx
        self.np = np
        self.on_partial, self.on_final, self.on_status = on_partial, on_final, on_status
        base = find_models_dir()
        if not base:
            raise RuntimeError("speech models not found (expected Conure's resources\\models or .\\models)")
        z = os.path.join(base, "zipformer-en-20M-int8")
        ep = max(0.5, (settings["stt"].get("endpointSilenceMs") or 1000) / 1000.0)
        self.rec = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=os.path.join(z, "tokens.txt"), encoder=os.path.join(z, "encoder-epoch-99-avg-1.int8.onnx"),
            decoder=os.path.join(z, "decoder-epoch-99-avg-1.onnx"), joiner=os.path.join(z, "joiner-epoch-99-avg-1.int8.onnx"),
            num_threads=max(1, min(8, int(settings["stt"].get("numThreads") or 2))), sample_rate=16000, feature_dim=80,
            enable_endpoint_detection=True, rule1_min_trailing_silence=2.5, rule2_min_trailing_silence=ep,
            rule3_min_utterance_length=30, decoding_method="greedy_search", provider="cpu")
        self.punct = None
        if settings["stt"].get("punctuation", True):
            try:
                pdir = os.path.join(base, "punct-en")
                cfg = sherpa_onnx.OnlinePunctuationConfig(model_config=sherpa_onnx.OnlinePunctuationModelConfig(
                    cnn_bilstm=os.path.join(pdir, "model.int8.onnx"), bpe_vocab=os.path.join(pdir, "bpe.vocab"), num_threads=1, provider="cpu"))
                self.punct = sherpa_onnx.OnlinePunctuation(cfg)
            except Exception as e:
                on_status("punctuation model unavailable: %s" % e)
        self.streams = {}
        self.last_partial = {}
        self.q = queue.Queue(maxsize=400)
        self.running = True
        threading.Thread(target=self._worker, daemon=True).start()

    def feed(self, source, samples):
        try:
            self.q.put_nowait((source, samples))
        except queue.Full:
            pass

    def flush(self):
        self.q.put(("__flush__", None))

    def stop(self):
        self.running = False
        self.q.put(("__stop__", None))

    @staticmethod
    def _restore_case(t):
        t = t.lower().strip()
        t = re.sub(r"(^|[.?!]\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), t)
        t = re.sub(r"\bi\b", "I", t)
        t = re.sub(r"\bi'(m|ve|ll|d)\b", lambda m: "I'" + m.group(1), t)
        return t

    def _post(self, text):
        t = re.sub(r"\s+", " ", text).strip().lower()
        if not t:
            return ""
        if self.punct:
            try:
                t = self.punct.add_punctuation_with_case(t)
            except Exception as e:
                log("punct failed", e)
        t = self._restore_case(t)
        if not re.search(r"[.?!]$", t):
            t += "."
        return t

    def _stream(self, source):
        s = self.streams.get(source)
        if s is None:
            s = self.rec.create_stream()
            self.streams[source] = s
        return s

    def _worker(self):
        while self.running:
            source, samples = self.q.get()
            if source == "__stop__":
                break
            try:
                if source == "__flush__":
                    for src in list(self.streams):
                        self._finish(src, force=True)
                    continue
                st = self._stream(source)
                st.accept_waveform(16000, samples)
                while self.rec.is_ready(st):
                    self.rec.decode_stream(st)
                text = self.rec.get_result(st).strip()
                if self.rec.is_endpoint(st):
                    self._finish(source, text=text)
                elif text != self.last_partial.get(source, ""):
                    self.last_partial[source] = text
                    self.on_partial(source, self._restore_case(text))
            except Exception as e:
                log("stt error", e)

    def _finish(self, source, text=None, force=False):
        st = self._stream(source)
        if force:
            st.accept_waveform(16000, self.np.zeros(int(16000 * 0.8), dtype=self.np.float32))
            while self.rec.is_ready(st):
                self.rec.decode_stream(st)
            text = self.rec.get_result(st).strip()
        if text:
            self.on_final(source, self._post(text))
        self.rec.reset(st)
        self.last_partial[source] = ""
        self.on_partial(source, "")


class AudioCapture:
    """Callback-based WASAPI capture: loopback of the default speakers ("them") + microphone ("you")."""

    def __init__(self, on_samples, on_level):
        import numpy as np
        import pyaudiowpatch as pyaudio
        self.np, self.pa = np, pyaudio
        self.on_samples, self.on_level = on_samples, on_level
        self.p = None
        self.streams = []

    def list_mics(self):
        p = self.pa.PyAudio()
        try:
            out = []
            for i in range(p.get_device_count()):
                d = p.get_device_info_by_index(i)
                if d["maxInputChannels"] > 0 and not d.get("isLoopbackDevice") and p.get_host_api_info_by_index(d["hostApi"])["type"] == self.pa.paWASAPI:
                    out.append((i, d["name"]))
            return out
        finally:
            p.terminate()

    def start(self, capture_system=True, capture_mic=True, mic_device="default"):
        self.p = self.pa.PyAudio()
        opened = {"sys": False, "mic": False}
        if capture_system:
            try:
                was = self.p.get_host_api_info_by_type(self.pa.paWASAPI)
                out = self.p.get_device_info_by_index(was["defaultOutputDevice"])
                lb = None
                for d in self.p.get_loopback_device_info_generator():
                    if out["name"] in d["name"]:
                        lb = d
                        break
                if lb is None:
                    lb = next(self.p.get_loopback_device_info_generator(), None)
                if lb is not None:
                    self._open(lb, "them")
                    opened["sys"] = True
            except Exception as e:
                log("loopback open failed", e)
        if capture_mic:
            try:
                if mic_device not in (None, "", "default"):
                    dev = self.p.get_device_info_by_index(int(mic_device))
                else:
                    was = self.p.get_host_api_info_by_type(self.pa.paWASAPI)
                    dev = self.p.get_device_info_by_index(was["defaultInputDevice"])
                self._open(dev, "you")
                opened["mic"] = True
            except Exception as e:
                log("mic open failed", e)
        return opened

    def _open(self, dev, source):
        np = self.np
        rate = int(dev["defaultSampleRate"])
        ch = max(1, int(dev["maxInputChannels"]))
        frames = max(256, int(rate * 0.04))

        def cb(in_data, frame_count, time_info, status):
            try:
                a = np.frombuffer(in_data, dtype=np.int16).astype(np.float32) / 32768.0
                if ch > 1:
                    a = a.reshape(-1, ch).mean(axis=1)
                if rate != 16000:
                    n = int(len(a) * 16000 / rate)
                    a = np.interp(np.linspace(0, len(a), n, endpoint=False), np.arange(len(a)), a).astype(np.float32)
                rms = float(np.sqrt(np.mean(a * a))) if len(a) else 0.0
                self.on_level(source, rms)
                self.on_samples(source, a)
            except Exception as e:
                log("audio cb error", e)
            return (None, self.pa.paContinue)

        s = self.p.open(format=self.pa.paInt16, channels=ch, rate=rate, input=True, input_device_index=dev["index"],
                        frames_per_buffer=frames, stream_callback=cb)
        s.start_stream()
        self.streams.append(s)
        log("audio open", source, dev["name"], rate, ch)

    def stop(self):
        for s in self.streams:
            try:
                s.stop_stream()
                s.close()
            except Exception:
                pass
        self.streams = []
        if self.p:
            try:
                self.p.terminate()
            except Exception:
                pass
            self.p = None


# ----------------------------------------------------------------------------
# Session
# ----------------------------------------------------------------------------
class Session:
    def __init__(self):
        self.active = False
        self.started = 0
        self.utterances = []
        self.answers = []

    def start(self):
        self.active = True
        self.started = time.time()

    def stop(self):
        self.active = False
        return {"utterances": list(self.utterances), "answers": list(self.answers), "durationMs": int((time.time() - self.started) * 1000) if self.started else 0}

    def add_final(self, source, text):
        u = {"id": uuid.uuid4().hex[:8], "source": source, "text": text, "ts": time.time()}
        self.utterances.append(u)
        return u

    def add_answer(self, **kw):
        a = {"id": uuid.uuid4().hex[:8], "text": "", "status": "pending", "ts": time.time()}
        a.update(kw)
        self.answers.append(a)
        return a

    def clear(self):
        self.utterances.clear()
        self.answers.clear()

    def save(self, d, title, notes=""):
        os.makedirs(d, exist_ok=True)
        name = datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + ".md"
        path = os.path.join(d, name)
        lines = ["# %s" % title, "", "_%s_" % datetime.now().strftime("%Y-%m-%d %H:%M"), ""]
        if notes:
            lines += ["## Notes", "", notes, ""]
        lines += ["## Transcript", ""]
        for u in self.utterances:
            lines.append("- **%s** (%s): %s" % ("Me" if u["source"] == "you" else "Other", datetime.fromtimestamp(u["ts"]).strftime("%H:%M:%S"), u["text"]))
        lines += ["", "## Suggested answers", ""]
        for a in self.answers:
            if a.get("text") and a.get("kind") != "summary":
                lines += ["### %s" % (a.get("question") or a.get("kind")), "", a["text"], ""]
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return path


# ----------------------------------------------------------------------------
# Win32 helpers
# ----------------------------------------------------------------------------
user32 = ctypes.windll.user32 if os.name == "nt" else None
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x80000
WS_EX_TRANSPARENT = 0x20
WS_EX_TOOLWINDOW = 0x80
WDA_NONE = 0x0
WDA_EXCLUDEFROMCAPTURE = 0x11
GA_ROOT = 2


def root_hwnd(widget):
    h = widget.winfo_id()
    try:
        return user32.GetAncestor(h, GA_ROOT) or h
    except Exception:
        return h


def set_private(widget, on):
    try:
        h = root_hwnd(widget)
        ok = user32.SetWindowDisplayAffinity(wt.HWND(h), wt.DWORD(WDA_EXCLUDEFROMCAPTURE if on else WDA_NONE))
        return bool(ok)
    except Exception as e:
        log("display affinity failed", e)
        return False


def set_click_through(widget, on):
    try:
        h = root_hwnd(widget)
        st = user32.GetWindowLongW(h, GWL_EXSTYLE)
        st = (st | WS_EX_LAYERED | WS_EX_TRANSPARENT) if on else (st & ~WS_EX_TRANSPARENT)
        user32.SetWindowLongW(h, GWL_EXSTYLE, st)
    except Exception as e:
        log("click-through failed", e)


def hide_from_taskbar(widget):
    try:
        h = root_hwnd(widget)
        st = user32.GetWindowLongW(h, GWL_EXSTYLE)
        user32.SetWindowLongW(h, GWL_EXSTYLE, st | WS_EX_TOOLWINDOW)
    except Exception:
        pass


# ----------------------------------------------------------------------------
# Theme
# ----------------------------------------------------------------------------
C = {
    "bg": "#0e1117", "panel": "#151a22", "panel2": "#1b212b", "border": "#262d3a", "text": "#e7eaf0", "muted": "#8a93a6",
    "accent": "#7c9cff", "accent2": "#5b7cfa", "green": "#34d399", "amber": "#fbbf24", "red": "#f87171",
    "them": "#ffb86b", "you": "#6ee7b7", "code": "#0b0e14", "btn": "#222a36", "btn_hover": "#2b3442", "on": "#2f3f6b",
}


# ----------------------------------------------------------------------------
# Main application
# ----------------------------------------------------------------------------
class App:
    def __init__(self):
        self.st = Settings()
        self.ui_q = queue.Queue()
        self.session = Session()
        self.agy = AgyClient(self.st)
        self.stt = None
        self.audio = None
        self.listening = False
        self.inflight = None
        self.auto_answered = set()
        self.auto_timer = None
        self.last_ask = None
        self.levels = {"them": 0.0, "you": 0.0}
        self.partials = {"them": "", "you": ""}
        self.answer_blocks = {}
        self.visible = True
        self.tray = None
        self.notice_timer = None
        self.settings_win = None
        self.stealth = False

        self.root = tk.Tk()
        self.root.withdraw()
        try:
            self.dpi = max(1.0, user32.GetDpiForSystem() / 96.0)
        except Exception:
            self.dpi = 1.0
        self.root.title("agy-conure")
        self.root.overrideredirect(True)
        self.root.configure(bg=C["border"])
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", float(self.st["overlay"].get("opacity") or 0.97))
        self._build_ui()
        self._place_window()
        self.root.deiconify()
        self.root.update_idletasks()
        hide_from_taskbar(self.root)
        self._apply_private(self.st["overlay"].get("privateMode", True), announce=False)
        if self.st["overlay"].get("clickThrough"):
            set_click_through(self.root, True)
        self._register_hotkeys()
        if self.st["general"].get("showTray", True):
            self._start_tray()
        self.root.after(50, self._pump)
        self.root.after(500, self._tick)
        threading.Thread(target=self._bg_init, daemon=True).start()
        self.root.protocol("WM_DELETE_WINDOW", self.quit)

    # ---------- threading helpers ----------
    def ui(self, fn, *a, **kw):
        self.ui_q.put((fn, a, kw))

    def _pump(self):
        try:
            while True:
                fn, a, kw = self.ui_q.get_nowait()
                try:
                    fn(*a, **kw)
                except Exception:
                    log("ui error", traceback.format_exc())
        except queue.Empty:
            pass
        self.root.after(30, self._pump)

    def _bg_init(self):
        if not os.path.isfile(AGY_EXE):
            self.ui(self.notice, "agy.exe not found at %s" % AGY_EXE, "error", 20000)
            return
        self.ui(self._set_model_label)
        # Warm the selected model immediately; catalog lookup can take several seconds.
        self.agy.warm()
        ms = self.agy.list_models()
        if ms:
            log("models:", ", ".join(m for m, _ in ms))

    # ---------- UI construction ----------
    def _font(self, size, weight="normal", mono=False):
        s = max(7, int(round(size * float(self.st["overlay"].get("fontScale") or 1.0))))
        return ("Consolas" if mono else "Segoe UI", s, weight)

    def _btn(self, parent, text, cmd, kind="normal", width=None):
        bg = {"primary": C["accent2"], "accent": "#2a6b4f", "normal": C["btn"], "ghost": C["panel"]}.get(kind, C["btn"])
        b = tk.Label(parent, text=text, bg=bg, fg=C["text"], font=self._font(9, "bold" if kind in ("primary", "accent") else "normal"),
                     padx=10, pady=4, cursor="hand2", width=width)
        b.base_bg = bg
        b.active = False
        b.bind("<Button-1>", lambda e: cmd())
        b.hover_bg = C["btn_hover"]
        b.bind("<Enter>", lambda e: b.configure(bg=b.hover_bg if not b.active else C["on"]))
        b.bind("<Leave>", lambda e: b.configure(bg=b.base_bg if not b.active else C["on"]))
        return b

    def _set_toggle(self, b, on):
        b.active = bool(on)
        b.configure(bg=C["on"] if on else b.base_bg, fg=C["text"] if on else C["muted"])

    def _build_ui(self):
        r = self.root
        self.frame = tk.Frame(r, bg=C["bg"])
        self.frame.pack(fill="both", expand=True, padx=1, pady=1)

        # title bar
        tb = tk.Frame(self.frame, bg=C["panel"], height=34)
        tb.pack(fill="x")
        tb.pack_propagate(False)
        self.titlebar = tb
        logo = tk.Label(tb, text="\u25c9", bg=C["panel"], fg=C["accent"], font=self._font(12, "bold"))
        logo.pack(side="left", padx=(10, 4))
        title = tk.Label(tb, text="agy-conure", bg=C["panel"], fg=C["text"], font=self._font(10, "bold"))
        title.pack(side="left")
        self.status_dot = tk.Label(tb, text="\u25cf", bg=C["panel"], fg=C["muted"], font=self._font(9))
        self.status_dot.pack(side="left", padx=(12, 2))
        self.status_lbl = tk.Label(tb, text="Idle", bg=C["panel"], fg=C["muted"], font=self._font(9))
        self.status_lbl.pack(side="left")
        self.timer_lbl = tk.Label(tb, text="", bg=C["panel"], fg=C["muted"], font=self._font(9, mono=True))
        self.timer_lbl.pack(side="left", padx=8)
        self.meter = tk.Canvas(tb, width=46, height=10, bg=C["panel"], highlightthickness=0)
        self.meter.pack(side="left")
        self.m_sys = self.meter.create_rectangle(0, 2, 2, 8, fill=C["them"], outline="")
        self.m_mic = self.meter.create_rectangle(24, 2, 26, 8, fill=C["you"], outline="")
        for txt, cmd in (("\u2715", self.hide), ("\u2699", self.open_settings), ("\u2922", self.cycle_size)):
            l = tk.Label(tb, text=txt, bg=C["panel"], fg=C["muted"], font=self._font(11), cursor="hand2", padx=6)
            l.pack(side="right")
            l.bind("<Button-1>", lambda e, c=cmd: c())
            l.bind("<Enter>", lambda e, w=l: w.configure(fg=C["text"]))
            l.bind("<Leave>", lambda e, w=l: w.configure(fg=C["muted"]))
        for w in (tb, logo, title, self.status_lbl, self.timer_lbl):
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)

        # toolbar
        bar = tk.Frame(self.frame, bg=C["bg"])
        bar.pack(fill="x", padx=8, pady=(8, 4))
        self.toolbar = bar
        self.b_listen = self._btn(bar, "\U0001F399 Listen", self.toggle_listening, "primary")
        self.b_listen.pack(side="left", padx=(0, 4))
        self.b_answer = self._btn(bar, "\u26a1 Answer", lambda: self.ask(kind="manual"), "accent")
        self.b_answer.pack(side="left", padx=(0, 4))
        self.b_shot = self._btn(bar, "\U0001F4F7 Screen", self.screenshot_answer)
        self.b_shot.pack(side="left", padx=(0, 10))
        self.b_auto = self._btn(bar, "Auto", lambda: self.toggle_setting("autoAnswer"))
        self.b_auto.pack(side="left", padx=(0, 4))
        self.b_private = self._btn(bar, "Private", self.toggle_private)
        self.b_private.pack(side="left", padx=(0, 4))
        self.b_trans = self._btn(bar, "Text", self.toggle_transcript)
        self.b_trans.pack(side="left", padx=(0, 4))
        self.b_stealth = self._btn(bar, "Stealth", self.toggle_stealth)
        self.b_stealth.pack(side="left", padx=(0, 4))
        bar.bind("<ButtonPress-1>", self._drag_start)
        bar.bind("<B1-Motion>", self._drag_move)
        self.b_clear = self._btn(bar, "Clear", self.clear, "ghost")
        self.b_clear.pack(side="right")

        # notice
        self.notice_lbl = tk.Label(self.frame, text="", bg=C["panel2"], fg=C["amber"], font=self._font(9), anchor="w", padx=10, pady=4, wraplength=480, justify="left")

        # footer first (packed at bottom so panels can expand in the middle)
        ft = tk.Frame(self.frame, bg=C["bg"])
        self.footer = ft
        self.hints = tk.Label(self.frame, text="", bg=C["bg"], fg=C["muted"], font=self._font(7), anchor="w", padx=8, wraplength=520, justify="left")
        self.hints.pack(fill="x", side="bottom")
        ft.pack(fill="x", side="bottom", padx=8, pady=(4, 4))
        self.ask_var = tk.StringVar()
        ent = tk.Entry(ft, textvariable=self.ask_var, bg=C["panel2"], fg=C["text"], insertbackground=C["text"], relief="flat",
                       font=self._font(10), highlightthickness=1, highlightbackground=C["border"], highlightcolor=C["accent"])
        ent.pack(side="left", fill="x", expand=True, ipady=6, padx=(0, 6))
        ent.bind("<Return>", lambda e: self.submit_ask())
        self.ask_entry = ent
        self._placeholder(ent, "Ask anything... 'summarize what they said', 'shorter', 'a STAR story about conflict'")
        self.b_send = self._btn(ft, "\u27a4", self.submit_ask, "primary")
        self.b_send.pack(side="right")
        self._update_hints()

        # transcript panel
        self.tpanel = tk.Frame(self.frame, bg=C["panel"], highlightbackground=C["border"], highlightthickness=1)
        hd = tk.Frame(self.tpanel, bg=C["panel"])
        hd.pack(fill="x")
        tk.Label(hd, text="Live transcript", bg=C["panel"], fg=C["muted"], font=self._font(8, "bold")).pack(side="left", padx=8, pady=(4, 0))
        tk.Label(hd, text="Other = call \u00b7 Me = mic \u00b7 click a line to answer it", bg=C["panel"], fg=C["muted"], font=self._font(8)).pack(side="right", padx=8, pady=(4, 0))
        self.transcript = tk.Text(self.tpanel, bg=C["panel"], fg=C["text"], font=self._font(9), wrap="word", bd=0, padx=8, pady=4,
                                  height=max(2, int(self.st["overlay"].get("transcriptHeight", 190) / 20)), cursor="arrow", highlightthickness=0, state="disabled")
        self.transcript.pack(fill="both", expand=True)
        self.transcript.tag_configure("them", foreground=C["them"], font=self._font(8, "bold"))
        self.transcript.tag_configure("you", foreground=C["you"], font=self._font(8, "bold"))
        self.transcript.tag_configure("q", foreground="#ffd9a8")
        self.transcript.tag_configure("line", spacing1=2, spacing3=2)
        self.transcript.bind("<Button-1>", self._transcript_click)
        self.partial_lbl = tk.Label(self.tpanel, text="", bg=C["panel"], fg=C["muted"], font=self._font(9), anchor="w", padx=8, justify="left", wraplength=480)
        self.partial_lbl.pack(fill="x", pady=(0, 4))

        # splitter
        self.splitter = tk.Frame(self.frame, bg=C["bg"], height=6, cursor="sb_v_double_arrow")
        self.splitter.bind("<B1-Motion>", self._split_drag)

        # answers panel
        self.apanel = tk.Frame(self.frame, bg=C["panel"], highlightbackground=C["border"], highlightthickness=1)
        hd2 = tk.Frame(self.apanel, bg=C["panel"])
        hd2.pack(fill="x")
        self.ahead = hd2
        tk.Label(hd2, text="AI answers", bg=C["panel"], fg=C["muted"], font=self._font(8, "bold")).pack(side="left", padx=8, pady=(4, 0))
        self.model_lbl = tk.Label(hd2, text="", bg=C["panel"], fg=C["muted"], font=self._font(8), cursor="hand2")
        self.model_lbl.pack(side="right", padx=8, pady=(4, 0))
        self.model_lbl.bind("<Button-1>", lambda e: self.open_settings())
        self.answers = tk.Text(self.apanel, bg=C["panel"], fg=C["text"], font=self._font(10), wrap="word", bd=0, padx=10, pady=6,
                               highlightthickness=0, state="disabled", cursor="arrow")
        sb = tk.Scrollbar(self.apanel, command=self.answers.yview, bg=C["panel"], troughcolor=C["panel"], width=8)
        self.answers.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.asb = sb
        self.answers.pack(fill="both", expand=True)
        self._config_answer_tags()
        self._show_empty_state()

        self._pack_panels()

        # resize grip
        self.grip = tk.Label(self.frame, text="\u25e2", bg=C["bg"], fg=C["border"], font=("Segoe UI", 10), cursor="size_nw_se")
        self.grip.place(relx=1.0, rely=1.0, anchor="se")
        self.grip.bind("<ButtonPress-1>", self._resize_start)
        self.grip.bind("<B1-Motion>", self._resize_move)

        self._set_toggle(self.b_auto, self.st["assistant"]["autoAnswer"])
        self._set_toggle(self.b_private, self.st["overlay"]["privateMode"])
        self._set_toggle(self.b_trans, self.st["overlay"]["showTranscript"])
        # frameless (overrideredirect) windows never become the OS foreground window on
        # their own, so keyboard input would keep going to the previous app: fix that.
        self.root.bind_all("<Button-1>", self._on_any_click, add="+")
        self.root.bind_all("<Escape>", lambda e: self.stealth and self.toggle_stealth(), add="+")

    def _on_any_click(self, e):
        try:
            if e.widget.winfo_toplevel() is self.root:
                user32.SetForegroundWindow(root_hwnd(self.root))
                w = e.widget
                if isinstance(w, (tk.Entry, tk.Text)) and str(w.cget("state")) == "normal":
                    self.root.after(1, w.focus_set)
        except Exception:
            pass

    def _update_hints(self):
        hk = self.st["hotkeys"]
        self.hints.configure(text="%s hide \u00b7 %s answer \u00b7 %s screen \u00b7 %s listen \u00b7 %s auto \u00b7 %s private \u00b7 %s click-through \u00b7 %s regenerate" % (
            hk["toggleVisibility"], hk["answer"], hk["screenshotAnswer"], hk["toggleListening"], hk["toggleAutoAnswer"], hk["togglePrivate"], hk["toggleClickThrough"], hk["regenerate"]))

    def _placeholder(self, ent, text):
        ent.ph = text
        ent.insert(0, text)
        ent.configure(fg=C["muted"])

        def fin(e):
            if ent.get() == ent.ph:
                ent.delete(0, "end")
                ent.configure(fg=C["text"])

        def fout(e):
            if not ent.get() and not self.stealth:
                ent.insert(0, ent.ph)
                ent.configure(fg=C["muted"])
        ent.bind("<FocusIn>", fin)
        ent.bind("<FocusOut>", fout)

    def _config_answer_tags(self):
        a = self.answers
        a.tag_configure("qhead", foreground=self._sfg() if self.stealth else C["accent"], font=self._font(9, "bold"), spacing1=10)
        a.tag_configure("meta", foreground=self._sfg(True) if self.stealth else C["muted"], font=self._font(8))
        a.tag_configure("body", font=self._font(10), spacing1=1)
        a.tag_configure("b", font=self._font(10, "bold"))
        a.tag_configure("h", font=self._font(11, "bold"), spacing1=6)
        a.tag_configure("code", font=self._font(9, mono=True), background="#ffffff" if self.stealth else C["code"], foreground=self._sfg() if self.stealth else "#c9d1d9", lmargin1=8, lmargin2=8, spacing1=1)
        a.tag_configure("icode", font=self._font(9, mono=True), background="#ffffff" if self.stealth else C["code"], foreground=self._sfg() if self.stealth else "#e3b341")
        a.tag_configure("bullet", lmargin1=12, lmargin2=24)
        a.tag_configure("err", foreground=self._sfg() if self.stealth else C["red"])
        a.tag_configure("empty", foreground=self._sfg(True) if self.stealth else C["muted"], font=self._font(10))
        a.tag_configure("sep", font=("Segoe UI", 4))

    def _show_empty_state(self):
        self.answers.configure(state="normal")
        self.answers.delete("1.0", "end")
        if self.stealth:  # blank page in stealth mode
            self.answers.configure(state="disabled")
            self.answer_blocks.clear()
            return
        self.answers.insert("end", "Ready. ", ("b", "empty"))
        self.answers.insert("end", "Press Listen to transcribe the call and your mic. The latest message from the other party is answered automatically while Auto is on; earlier messages and previous answers are kept as context.\n\n", "empty")
        self.answers.insert("end", "Brain: Antigravity CLI (agy) \u00b7 model %s" % self.st["llm"]["model"], "meta")
        self.answers.configure(state="disabled")
        self.answer_blocks.clear()

    def _pack_panels(self):
        self.tpanel.pack_forget()
        self.splitter.pack_forget()
        self.apanel.pack_forget()
        if self.st["overlay"].get("showTranscript", True) and not self.stealth:
            self.tpanel.pack(fill="x", padx=8, pady=(4, 0))
            self.splitter.pack(fill="x", padx=8)
        self.apanel.pack(fill="both", expand=True, padx=8, pady=(0, 4))

    def _set_model_label(self):
        self.model_lbl.configure(text="agy \u00b7 %s" % self.st["llm"]["model"])

    # ---------- window geometry ----------
    def px(self, dip):
        return int(round(dip * self.dpi))

    def _place_window(self):
        o = self.st["overlay"]
        w, h = self.px(int(o.get("width") or 540)), self.px(int(o.get("height") or 720))
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        x = self.px(o["x"]) if o.get("x") is not None else sw - w - self.px(24)
        y = self.px(o["y"]) if o.get("y") is not None else self.px(40)
        x = max(0, min(int(x), sw - 100))
        y = max(0, min(int(y), sh - 100))
        self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))

    def _save_geometry(self):
        d = self.dpi
        self.st["overlay"].update({"width": int(self.root.winfo_width() / d), "height": int(self.root.winfo_height() / d),
                                   "x": int(self.root.winfo_x() / d), "y": int(self.root.winfo_y() / d)})
        self.st.save()

    def _drag_start(self, e):
        self._drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def _drag_move(self, e):
        dx, dy = self._drag
        self.root.geometry("+%d+%d" % (e.x_root - dx, e.y_root - dy))
        self.root.after_idle(self._save_geometry)

    def _resize_start(self, e):
        self._rs = (e.x_root, e.y_root, self.root.winfo_width(), self.root.winfo_height())

    def _resize_move(self, e):
        x0, y0, w0, h0 = self._rs
        w = max(self.px(380), w0 + e.x_root - x0)
        h = max(self.px(320), h0 + e.y_root - y0)
        self.root.geometry("%dx%d" % (w, h))
        self.root.after_idle(self._save_geometry)

    def _split_drag(self, e):
        h = self.transcript.winfo_height() + e.y
        lines = max(2, min(30, int(h / 20)))
        self.transcript.configure(height=lines)
        self.st["overlay"]["transcriptHeight"] = lines * 20
        self.root.after_idle(self.st.save)

    def move_by(self, dx, dy):
        self.root.geometry("+%d+%d" % (self.root.winfo_x() + self.px(dx), self.root.winfo_y() + self.px(dy)))
        self._save_geometry()

    def grow(self, d):
        w = max(self.px(380), self.root.winfo_width() + self.px(d))
        h = max(self.px(320), self.root.winfo_height() + self.px(int(d * 1.3)))
        self.root.geometry("%dx%d" % (w, h))
        self._save_geometry()

    def cycle_size(self):
        sizes = [(420, 560), (540, 720), (680, 900), (860, 1100)]
        cur_w = self.root.winfo_width() / self.dpi
        nxt = sizes[0]
        for i, s in enumerate(sizes):
            if cur_w <= s[0] + 10:
                nxt = sizes[(i + 1) % len(sizes)]
                break
        self.root.geometry("%dx%d" % (self.px(nxt[0]), self.px(nxt[1])))
        self._save_geometry()

    # ---------- status / notices ----------
    def set_status(self, text, color):
        self.status_lbl.configure(text=text)
        self.status_dot.configure(fg=color)

    def notice(self, text, level="info", ms=5000):
        if self.stealth:  # no banners in stealth mode
            log("notice[%s] %s" % (level, text))
            return
        col = {"info": C["accent"], "warn": C["amber"], "error": C["red"]}.get(level, C["accent"])
        self.notice_lbl.configure(text=text, fg=col)
        self.notice_lbl.pack(fill="x", padx=8, pady=(2, 2), after=self.toolbar)
        if self.notice_timer:
            self.root.after_cancel(self.notice_timer)
        self.notice_timer = self.root.after(ms, self.notice_lbl.pack_forget)
        log("notice[%s] %s" % (level, text))

    def _tick(self):
        if self.listening and self.session.started:
            s = int(time.time() - self.session.started)
            self.timer_lbl.configure(text="%02d:%02d" % (s // 60, s % 60))
        else:
            self.timer_lbl.configure(text="")
        for key, item, x0 in (("them", self.m_sys, 0), ("you", self.m_mic, 24)):
            lvl = min(1.0, self.levels.get(key, 0.0) * 6)
            self.meter.coords(item, x0, 2, x0 + 2 + int(lvl * 18), 8)
            self.levels[key] *= 0.6
        self.root.after(120, self._tick)

    # ---------- transcript ----------
    def _render_transcript_line(self, u):
        t = self.transcript
        t.configure(state="normal")
        tag_id = "u_" + u["id"]
        who = "Me" if u["source"] == "you" else "Other"
        isq = u["source"] == "them" and question_score(u["text"]) >= QUESTION_THRESHOLD
        t.insert("end", who + "  ", (u["source"], tag_id, "line"))
        t.insert("end", u["text"] + "\n", ("line", tag_id) + (("q",) if isq else ()))
        t.configure(state="disabled")
        t.see("end")

    def _transcript_click(self, e):
        idx = self.transcript.index("@%d,%d" % (e.x, e.y))
        for tag in self.transcript.tag_names(idx):
            if tag.startswith("u_"):
                uid = tag[2:]
                for u in self.session.utterances:
                    if u["id"] == uid:
                        self.ask(kind="manual", question=u["text"], utterance_id=uid)
                        return

    def _update_partials(self):
        parts = []
        if self.partials["them"]:
            parts.append("Other... " + self.partials["them"])
        if self.partials["you"]:
            parts.append("Me... " + self.partials["you"])
        self.partial_lbl.configure(text="\n".join(parts))

    def on_partial(self, source, text):
        self.partials[source] = text
        self.ui(self._update_partials)

    def on_final(self, source, text):
        if not self.session.active:
            return
        u = self.session.add_final(source, text)
        self.ui(self._render_transcript_line, u)
        self.ui(self._schedule_auto, u)

    def _schedule_auto(self, u):
        if u["source"] != "them" or not self.st["assistant"]["autoAnswer"]:
            return
        if self.auto_timer:
            self.root.after_cancel(self.auto_timer)

        def fire():
            self.auto_timer = None
            if not self.st["assistant"]["autoAnswer"] or not self.session.active or u["id"] in self.auto_answered:
                return
            if len(u["text"].split()) < int(self.st["assistant"].get("minQuestionWords") or 3):
                return
            if question_score(u["text"]) < QUESTION_THRESHOLD:
                return
            self.auto_answered.add(u["id"])
            self.ask(kind="auto", question=u["text"], utterance_id=u["id"])
        self.auto_timer = self.root.after(700, fire)

    # ---------- listening ----------
    def toggle_listening(self):
        if self.listening:
            self.stop_listening()
        else:
            self.start_listening()

    def start_listening(self):
        if self.listening:
            return
        self.set_status("Starting...", C["amber"])
        self.b_listen.configure(text="\u25a0 Stop")
        self.listening = True
        self.session.start()
        self.auto_answered.clear()
        threading.Thread(target=self._start_capture_thread, daemon=True).start()

    def _start_capture_thread(self):
        t0 = time.time()
        try:
            if self.stt is None:
                self.stt = LocalSTT(self.st, self.on_partial, self.on_final, lambda m: log("[stt]", m))
            self.audio = AudioCapture(self.stt.feed, self._on_level)
            a = self.st["audio"]
            opened = self.audio.start(a.get("captureSystem", True), a.get("captureMic", True), a.get("micDevice", "default"))
            if not opened["sys"] and not opened["mic"]:
                raise RuntimeError("no audio source could be opened")
            if a.get("captureSystem", True) and not opened["sys"]:
                self.ui(self.notice, "System (call) audio is not being captured - only your microphone is transcribed.", "warn", 12000)
            self.ui(self.set_status, "Listening", C["green"])
            self.ui(self.notice, "Listening - local Zipformer (ready in %.1f s)" % (time.time() - t0), "info", 3000)
        except Exception as e:
            log("capture start failed", traceback.format_exc())
            self.ui(self.notice, "Audio capture failed: %s" % e, "error", 15000)
            self.ui(self.stop_listening, True)

    def _on_level(self, source, rms):
        self.levels[source] = max(self.levels.get(source, 0.0), rms)

    def stop_listening(self, silent=False):
        if not self.listening:
            return
        self.listening = False
        self.b_listen.configure(text="\U0001F399 Listen")
        self.set_status("Idle", C["muted"])
        if self.auto_timer:
            self.root.after_cancel(self.auto_timer)
            self.auto_timer = None
        audio, stt = self.audio, self.stt
        self.audio = None

        def _stop():
            if audio:
                audio.stop()
            if stt:
                stt.flush()
                time.sleep(0.4)
            res = self.session.stop()
            if not silent:
                self._finish_session(res)
        threading.Thread(target=_stop, daemon=True).start()

    def _finish_session(self, res):
        s = self.st
        if not res["utterances"]:
            return
        notes = ""
        if s["session"].get("autoNotes", True) and len(res["utterances"]) >= 4:
            try:
                dur = "%d min" % max(1, round(res["durationMs"] / 60000))
                msgs = build_summary_messages(res["utterances"], res["answers"], s["assistant"]["mode"], dur)
                prompt = flatten_for_agy("You write crisp, accurate post-meeting notes in Markdown. Never invent facts that are not in the transcript.", msgs)
                done = threading.Event()
                holder = {"text": ""}
                a = self.session.add_answer(question="Session notes", kind="summary", model=s["llm"]["model"])
                self.ui(self._answer_start, a)
                self._run_agy(a, prompt, on_finish=lambda txt: (holder.update(text=txt), done.set()))
                done.wait(180)
                notes = holder["text"]
            except Exception as e:
                log("auto notes failed", e)
        if s["session"].get("saveTranscripts", True):
            d = s["session"].get("saveDir") or SESSIONS_DIR
            try:
                path = self.session.save(d, "agy-conure %s session" % s["assistant"]["mode"], notes)
                self.ui(self.notice, "Session saved: %s" % os.path.basename(path), "info", 6000)
            except Exception as e:
                self.ui(self.notice, "Could not save session: %s" % e, "warn", 8000)

    # ---------- answers ----------
    def _answer_start(self, a):
        if not self.answer_blocks:
            self.answers.configure(state="normal")
            self.answers.delete("1.0", "end")
            self.answers.configure(state="disabled")
        t = self.answers
        t.configure(state="normal")
        head = {"auto": "Q", "manual": "Q", "screenshot": "Screen", "summary": "Session notes", "regenerate": "Q (again)"}.get(a.get("kind"), "Q")
        q = (a.get("question") or "").strip()
        if t.index("end-1c") != "1.0":
            t.insert("end", "\n", "sep")
        t.insert("end", ("%s \u00b7 %s\n" % (head, truncate(q, 160))) if (q and a.get("kind") != "summary") else head + "\n", "qhead")
        mb, me = "b_" + a["id"], "e_" + a["id"]
        t.mark_set(mb, "end-1c")
        t.mark_gravity(mb, "left")
        t.insert("end", "...", "meta")
        t.mark_set(me, "end-1c")
        t.mark_gravity(me, "right")
        t.configure(state="disabled")
        t.see("end")
        self.answer_blocks[a["id"]] = {"mark_b": mb, "mark_e": me, "text": "", "a": a, "dirty": False}
        self.set_status("Thinking...", C["accent"])

    def _answer_delta(self, aid, delta):
        blk = self.answer_blocks.get(aid)
        if not blk:
            return
        blk["text"] += delta
        if not blk["dirty"]:
            blk["dirty"] = True
            self.root.after(60, lambda: self._rerender(aid))

    def _rerender(self, aid, final=False, ms=None, error=None):
        blk = self.answer_blocks.get(aid)
        if not blk:
            return
        blk["dirty"] = False
        t = self.answers
        at_bottom = t.yview()[1] > 0.95
        t.configure(state="normal")
        t.delete(blk["mark_b"], blk["mark_e"])
        self._insert_markdown(t, blk["text"], blk["mark_e"])
        if error:
            t.insert(blk["mark_e"], "Error: %s\n" % error, "err")
        if final and not error:
            t.insert(blk["mark_e"], "%s \u00b7 %.1f s \u00b7 " % (blk["a"].get("model", ""), (ms or 0) / 1000.0), ("meta",))
            tag = "copy_" + aid
            t.insert(blk["mark_e"], "copy", ("meta", tag))
            t.tag_configure(tag, foreground=self._sfg() if self.stealth else C["accent"], underline=True)
            t.tag_bind(tag, "<Button-1>", lambda e, txt=blk["text"]: self._copy(txt))
            t.tag_bind(tag, "<Enter>", lambda e: t.configure(cursor="hand2"))
            t.tag_bind(tag, "<Leave>", lambda e: t.configure(cursor="arrow"))
            t.insert(blk["mark_e"], "\n", "meta")
        t.configure(state="disabled")
        if at_bottom:
            t.see("end")

    def _copy(self, txt):
        self.root.clipboard_clear()
        self.root.clipboard_append(txt)
        self.notice("Answer copied", "info", 1500)

    def _insert_markdown(self, t, md, at):
        """Minimal Markdown renderer into a tk.Text at mark `at`."""
        in_code = False
        for ln in md.split("\n"):
            if ln.strip().startswith("```"):
                in_code = not in_code
                continue
            if in_code:
                t.insert(at, ln + "\n", ("code",))
                continue
            s = ln.rstrip()
            m = re.match(r"^(#{1,6})\s+(.*)$", s)
            if m:
                self._insert_inline(t, m.group(2) + "\n", at, ("h",))
                continue
            mb = re.match(r"^\s*([-*\u2022]|\d+[.)])\s+(.*)$", s)
            if mb:
                bullet = "\u2022 " if mb.group(1) in "-*\u2022" else mb.group(1) + " "
                t.insert(at, bullet, ("body", "bullet"))
                self._insert_inline(t, mb.group(2) + "\n", at, ("body", "bullet"))
                continue
            if not s.strip():
                t.insert(at, "\n", ("sep",))
                continue
            self._insert_inline(t, s + "\n", at, ("body",))

    def _insert_inline(self, t, s, at, base):
        pos = 0
        for m in re.finditer(r"(\*\*(.+?)\*\*|`([^`]+)`)", s):
            if m.start() > pos:
                t.insert(at, s[pos:m.start()], base)
            if m.group(2) is not None:
                t.insert(at, m.group(2), base + ("b",))
            else:
                t.insert(at, m.group(3), base + ("icode",))
            pos = m.end()
        if pos < len(s):
            t.insert(at, s[pos:], base)

    def _answer_done(self, aid, text, ms):
        blk = self.answer_blocks.get(aid)
        if blk:
            blk["text"] = text or blk["text"]
            blk["a"].update(text=blk["text"], status="done", ms=ms)
            self._rerender(aid, final=True, ms=ms)
        self.set_status("Listening" if self.listening else "Idle", C["green"] if self.listening else C["muted"])

    def _answer_error(self, aid, msg):
        blk = self.answer_blocks.get(aid)
        if blk:
            blk["a"].update(status="error")
            self._rerender(aid, final=True, error=msg)
        self.set_status("Listening" if self.listening else "Idle", C["green"] if self.listening else C["muted"])
        self.notice(msg, "error", 8000)

    def cancel_inflight(self):
        p = self.inflight
        if p:
            self.inflight = None
            try:
                p.kill()
            except Exception:
                pass

    def _run_agy(self, a, prompt, on_finish=None, image_path=None):
        """Run one answer in a background thread; streams into the UI."""
        self.cancel_inflight()
        model = a.get("model") or self.st["llm"]["model"]
        t0 = time.time()

        def worker():
            proc = self.agy.take(model)
            self.inflight = proc
            proc.ready.wait(60)
            if proc.dead or proc.error:
                self.ui(self._answer_error, a["id"], proc.error or "agy failed to start")
                if on_finish:
                    on_finish("")
                return
            done = threading.Event()
            buf = []

            def on_delta(d):
                buf.append(d)
                self.ui(self._answer_delta, a["id"], d)

            def on_done(r):
                text = "".join(buf) or (r.get("response") or "")
                ms = int((time.time() - t0) * 1000)
                self.ui(self._answer_done, a["id"], text, ms)
                log("answer done in %.1fs (%d chars)" % (ms / 1000.0, len(text)))
                done.set()
                if on_finish:
                    on_finish(text)

            def on_error(msg):
                self.ui(self._answer_error, a["id"], msg)
                done.set()
                if on_finish:
                    on_finish("")
            proc.ask(prompt, on_delta, on_done, on_error)
            done.wait(300)
            if self.inflight is proc:
                self.inflight = None
            proc.kill()
            if image_path:
                try:
                    os.remove(image_path)
                except Exception:
                    pass
        threading.Thread(target=worker, daemon=True).start()

    def _history(self):
        n = int(self.st["assistant"].get("historyTurns") or 4)
        hs = [a for a in self.session.answers if a.get("status") == "done" and a.get("kind") != "summary"]
        return hs[-n:] if n > 0 else []

    def ask(self, kind="manual", question="", prompt="", utterance_id=None, regenerate=False):
        s = self.st
        N = int(s["assistant"].get("contextWindowUtterances") or 12)
        allu = self.session.utterances
        q = (question or "").strip()
        if not q and not prompt.strip():
            them = [u for u in allu if u["source"] == "them"]
            if them:
                q = them[-1]["text"]
                utterance_id = them[-1]["id"]
                # merge a tiny trailing fragment with the previous message
                if len(q.split()) <= 4 and len(them) > 1 and them[-1]["ts"] - them[-2]["ts"] <= 2.5:
                    q = them[-2]["text"] + " " + q
            elif self.partials["them"]:
                q = self.partials["them"]
            else:
                self.notice("Nothing to answer yet - wait for the other party to speak, or type a question below.", "warn", 4000)
                return
        if utterance_id:
            idx = next((i for i, u in enumerate(allu) if u["id"] == utterance_id), len(allu))
            ctx = allu[max(0, idx - N):idx]
        else:
            ctx = allu[-N:]
        history = self._history()
        msgs = build_answer_messages(q, ctx, history, s, manual_prompt=prompt, regenerate=regenerate)
        full = flatten_for_agy(build_system_prompt(s), msgs)
        a = self.session.add_answer(question=prompt.strip() or q, kind=kind if not regenerate else "regenerate", model=s["llm"]["model"])
        self.last_ask = {"kind": kind, "question": q, "prompt": prompt, "utterance_id": utterance_id}
        self._answer_start(a)
        self._run_agy(a, full)

    def regenerate(self):
        if not self.last_ask:
            self.notice("Nothing to regenerate yet", "warn", 2500)
            return
        la = self.last_ask
        if la["kind"] == "screenshot":
            self.screenshot_answer(la.get("prompt", ""))
        else:
            self.ask(kind=la["kind"], question=la["question"], prompt=la["prompt"], utterance_id=la["utterance_id"], regenerate=True)

    def submit_ask(self):
        txt = self.ask_var.get().strip()
        if not txt or txt == getattr(self.ask_entry, "ph", None):
            return
        self.ask_var.set("")
        self.ask(kind="manual", prompt=txt)
        self.root.focus_set()
        self.ask_entry.event_generate("<FocusOut>")

    def screenshot_answer(self, extra=""):
        s = self.st
        hide = self.visible and not s["overlay"].get("privateMode", True)
        if hide:
            self.root.withdraw()
            self.root.update()
            time.sleep(0.15)
        path = None
        try:
            import mss
            from PIL import Image
            with mss.mss() as sct:
                mon = sct.monitors[1]
                shot = sct.grab(mon)
                img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
            # Preserve native pixels for legible small text; only downscale very large displays.
            max_edge = 2560
            if max(img.size) > max_edge:
                scale = max_edge / max(img.size)
                resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
                new_size = (max(1, round(img.width * scale)), max(1, round(img.height * scale)))
                img = img.resize(new_size, resampling)
            path = os.path.join(IMG_DIR, "shot-%d.png" % int(time.time() * 1000))
            img.save(path, "PNG", compress_level=2)
        except Exception as e:
            log("screenshot failed", traceback.format_exc())
            self.notice("Screenshot failed: %s" % e, "error", 8000)
        if hide:
            self.root.deiconify()
            self.root.attributes("-topmost", True)
        if not path:
            return
        N = int(s["assistant"].get("contextWindowUtterances") or 12)
        msgs = build_screenshot_messages(self.session.utterances[-N:], self._history(), s, extra)
        full = flatten_for_agy(build_system_prompt(s), msgs, image_path=path)
        a = self.session.add_answer(question=extra.strip() or "What is on my screen?", kind="screenshot", model=s["llm"]["model"])
        self.last_ask = {"kind": "screenshot", "question": "", "prompt": extra, "utterance_id": None}
        self._answer_start(a)
        self._run_agy(a, full, image_path=path)

    # ---------- toggles ----------
    def toggle_setting(self, key):
        v = not self.st["assistant"][key]
        self.st["assistant"][key] = v
        self.st.save()
        if key == "autoAnswer":
            self._set_toggle(self.b_auto, v)
            self.notice("Auto-answer %s" % ("on" if v else "off"), "info", 1500)

    def _apply_private(self, on, announce=True):
        ok = set_private(self.root, on)
        self.st["overlay"]["privateMode"] = bool(on)
        self.st.save()
        self._set_toggle(self.b_private, on)
        if announce:
            if on and not ok:
                self.notice("Private mode not supported on this Windows version", "warn", 5000)
            else:
                self.notice("Private mode %s - overlay %s screen shares" % ("on" if on else "off", "hidden from" if on else "visible in"), "info", 2500)

    def toggle_private(self):
        self._apply_private(not self.st["overlay"].get("privateMode", True))

    def toggle_click_through(self):
        v = not self.st["overlay"].get("clickThrough", False)
        self.st["overlay"]["clickThrough"] = v
        self.st.save()
        set_click_through(self.root, v)
        self.notice("Click-through %s%s" % ("on" if v else "off", (" - press %s to interact again" % self.st["hotkeys"]["toggleClickThrough"]) if v else ""), "info", 4000)

    def toggle_transcript(self):
        v = not self.st["overlay"].get("showTranscript", True)
        self.st["overlay"]["showTranscript"] = v
        self.st.save()
        self._set_toggle(self.b_trans, v)
        self._pack_panels()

    # ---------- stealth mode extras (flat scrollbar, edge resize, opacity + text sliders) ----------
    def _sfg(self, dim=False):
        """Stealth text colour: light grey (slider 0) .. black (slider 1); dim = secondary text."""
        g = int(round(217 * (1.0 - getattr(self, "_stealth_text", 1.0))))
        if dim:
            g = int(round(g + (217 - g) * 0.47))
        return "#%02x%02x%02x" % (g, g, g)

    def _stealth_build(self):
        if getattr(self, "stealth_row", None) is not None:
            return
        W = "#ffffff"
        # borderless scrollbar (the native Windows one cannot be drawn without an outline)
        self._ssb_frac = (0.0, 1.0)
        self.ssb = tk.Canvas(self.apanel, width=self.px(6), bg=W, highlightthickness=0, bd=0, relief="flat", cursor="arrow")
        self.ssb.bind("<Configure>", lambda e: self._ssb_draw())
        self.ssb.bind("<Button-1>", self._ssb_drag)
        self.ssb.bind("<B1-Motion>", self._ssb_drag)
        # sliders: window opacity (left) and text darkness (right)
        self.stealth_row = tk.Frame(self.frame, bg=W, bd=0, highlightthickness=0)
        kw = dict(orient="horizontal", showvalue=0, resolution=0.01, length=self.px(90), width=self.px(6), sliderlength=self.px(12),
                  bg=W, troughcolor="#f0f0f0", activebackground="#e0e0e0", highlightthickness=0, bd=0, relief="flat",
                  sliderrelief="flat", takefocus=0)
        self.s_alpha = tk.Scale(self.stealth_row, from_=0.3, to=1.0, **kw)
        self.s_alpha.set(max(0.3, min(1.0, float(self.root.attributes("-alpha")))))
        self.s_alpha.configure(command=self._stealth_alpha_set)
        self.s_alpha.pack(side="left", padx=(0, 10))
        self.s_text = tk.Scale(self.stealth_row, from_=0.0, to=1.0, **kw)
        self.s_text.set(getattr(self, "_stealth_text", 1.0))
        self.s_text.configure(command=self._stealth_text_set)
        self.s_text.pack(side="left")
        # invisible resize handles on all 4 edges + 4 corners
        t, c = self.px(5), self.px(10)
        self._edge_handles = []
        for edge, cur, pk in (
            ("n", "size_ns", dict(x=0, y=0, relwidth=1.0, height=t)),
            ("s", "size_ns", dict(x=0, rely=1.0, anchor="sw", relwidth=1.0, height=t)),
            ("w", "size_we", dict(x=0, y=0, relheight=1.0, width=t)),
            ("e", "size_we", dict(relx=1.0, y=0, anchor="ne", relheight=1.0, width=t)),
            ("nw", "size_nw_se", dict(x=0, y=0, width=c, height=c)),
            ("ne", "size_ne_sw", dict(relx=1.0, y=0, anchor="ne", width=c, height=c)),
            ("sw", "size_ne_sw", dict(x=0, rely=1.0, anchor="sw", width=c, height=c)),
            ("se", "size_nw_se", dict(relx=1.0, rely=1.0, anchor="se", width=c, height=c)),
        ):
            h = tk.Frame(self.root, bg=W, bd=0, highlightthickness=0, cursor=cur)
            h.bind("<ButtonPress-1>", lambda e, ed=edge: self._edge_start(e, ed))
            h.bind("<B1-Motion>", self._edge_move)
            self._edge_handles.append((h, pk))

    def _stealth_enter(self):
        self._stealth_build()
        self._stealth_prev_alpha = float(self.root.attributes("-alpha"))
        self.asb.pack_forget()
        self.ssb.pack(side="right", fill="y", before=self.answers)
        self.answers.configure(yscrollcommand=self._ssb_set)
        self._ssb_set(*self.answers.yview())
        self.grip.place_forget()
        self.stealth_row.pack(fill="x", side="bottom", before=self.footer, padx=8, pady=(0, 6))
        for h, pk in self._edge_handles:
            h.place(**pk)
            h.lift()
        self.root.attributes("-alpha", float(self.s_alpha.get()))

    def _stealth_exit(self):
        for h, _ in self._edge_handles:
            h.place_forget()
        self.stealth_row.pack_forget()
        self.ssb.pack_forget()
        self.asb.pack(side="right", fill="y", before=self.answers)
        self.answers.configure(yscrollcommand=self.asb.set)
        self.asb.set(*self.answers.yview())
        self.grip.place(relx=1.0, rely=1.0, anchor="se")
        self.root.attributes("-alpha", self._stealth_prev_alpha)

    def _stealth_alpha_set(self, v):
        if self.stealth:
            self.root.attributes("-alpha", max(0.3, min(1.0, float(v))))

    def _stealth_text_set(self, v):
        self._stealth_text = max(0.0, min(1.0, float(v)))
        if not self.stealth:
            return
        self.answers.configure(fg=self._sfg())
        self._config_answer_tags()
        for tag in self.answers.tag_names():
            if tag.startswith("copy_"):
                self.answers.tag_configure(tag, foreground=self._sfg())

    def _ssb_set(self, lo, hi):
        self._ssb_frac = (float(lo), float(hi))
        self._ssb_draw()

    def _ssb_draw(self):
        c = self.ssb
        lo, hi = self._ssb_frac
        c.delete("all")
        if hi - lo >= 0.999:
            return
        h, w = c.winfo_height(), c.winfo_width()
        y0 = lo * h
        c.create_rectangle(0, y0, w, max(hi * h, y0 + self.px(14)), fill="#e4e4e4", outline="")

    def _ssb_drag(self, e):
        lo, hi = self._ssb_frac
        self.answers.yview_moveto(max(0.0, e.y / max(1, self.ssb.winfo_height()) - (hi - lo) / 2))

    def _edge_start(self, e, edge):
        r = self.root
        self._ers = (edge, e.x_root, e.y_root, r.winfo_x(), r.winfo_y(), r.winfo_width(), r.winfo_height())

    def _edge_move(self, e):
        edge, x0, y0, wx, wy, w0, h0 = self._ers
        dx, dy = e.x_root - x0, e.y_root - y0
        mw, mh = self.px(380), self.px(320)
        x, y, w, h = wx, wy, w0, h0
        if "e" in edge:
            w = max(mw, w0 + dx)
        if "s" in edge:
            h = max(mh, h0 + dy)
        if "w" in edge:
            w = max(mw, w0 - dx)
            x = wx + w0 - w
        if "n" in edge:
            h = max(mh, h0 - dy)
            y = wy + h0 - h
        self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))
        self.root.after_idle(self._save_geometry)

    def toggle_stealth(self):
        """Stealth mode: plain white notepad look - only Screen, the ask box, send and the answers stay."""
        on = not self.stealth
        self.stealth = on
        W, K = "#ffffff", "#000000"
        ent = self.ask_entry
        btns = (self.b_shot, self.b_send, self.b_stealth)
        flat = {"relief": "flat", "borderwidth": 0, "highlightthickness": 0}  # stealth: no outlines anywhere
        if on:
            spec = [
                (self.root, {"bg": W}), (self.frame, {"bg": W}), (self.toolbar, {"bg": W}), (self.footer, {"bg": W}),
                (self.apanel, {"bg": W, "highlightbackground": W, **flat}),
                (self.answers, {"bg": W, "fg": self._sfg(), **flat}),
                (self.asb, {"bg": W, "troughcolor": W}),
                (ent, {"bg": W, "fg": K, "insertbackground": K, "highlightbackground": W, "highlightcolor": W, **flat}),
                (self.b_shot, {"bg": W, "fg": K, **flat}), (self.b_send, {"bg": W, "fg": K, **flat}),
                (self.b_stealth, {"bg": W, "fg": "#bbbbbb", "text": "\u22ef", **flat}),
                (self.grip, {"bg": W, "fg": "#dddddd"}),
            ]
            self._stealth_saved = [(w, {k: w.cget(k) for k in o}) for w, o in spec]
            self._stealth_bar = [(w, w.pack_info()) for w in self.toolbar.pack_slaves()]
            for w, o in spec:
                w.configure(**o)
            for b in btns:
                b.normal_bg = (b.base_bg, b.hover_bg)
                b.base_bg, b.hover_bg = W, "#eeeeee"
            for w, _ in self._stealth_bar:
                if w not in (self.b_shot, self.b_stealth):
                    w.pack_forget()
            for w in (self.titlebar, self.hints, self.notice_lbl, self.ahead):
                w.pack_forget()
            self._stealth_enter()
            if ent.get() == getattr(ent, "ph", None):
                ent.delete(0, "end")
        else:
            self._stealth_exit()
            for w, o in self._stealth_saved:
                w.configure(**o)
            for b in btns:
                b.base_bg, b.hover_bg = b.normal_bg
                b.configure(bg=b.base_bg)
            for w, _ in self._stealth_bar:
                w.pack_forget()
            for w, info in self._stealth_bar:
                w.pack(**info)
            self.titlebar.pack(fill="x", before=self.toolbar)
            self.hints.pack(fill="x", side="bottom", before=self.footer)
            self.ahead.pack(fill="x", before=self.asb)
            try:
                focused = self.root.focus_get() is ent
            except Exception:
                focused = False
            if not ent.get() and not focused:
                ent.insert(0, ent.ph)
                ent.configure(fg=C["muted"])
            else:
                ent.configure(fg=C["text"])
        self._pack_panels()
        self._config_answer_tags()
        for tag in self.answers.tag_names():
            if tag.startswith("copy_"):
                self.answers.tag_configure(tag, foreground=self._sfg() if on else C["accent"])
        if not self.answer_blocks:
            self._show_empty_state()
        self.grip.lift()

    def toggle_visibility(self):
        if self.visible:
            self.hide()
        else:
            self.show()

    def hide(self):
        self.visible = False
        self.root.withdraw()

    def show(self):
        self.visible = True
        self.root.deiconify()
        self.root.attributes("-topmost", True)
        self.root.update_idletasks()
        hide_from_taskbar(self.root)
        set_private(self.root, self.st["overlay"].get("privateMode", True))
        if self.st["overlay"].get("clickThrough"):
            set_click_through(self.root, True)
        self.root.lift()

    def clear(self):
        self.cancel_inflight()
        self.session.clear()
        self.auto_answered.clear()
        self.transcript.configure(state="normal")
        self.transcript.delete("1.0", "end")
        self.transcript.configure(state="disabled")
        self.partials = {"them": "", "you": ""}
        self._update_partials()
        self._show_empty_state()
        self.notice("Cleared", "info", 1200)

    def scroll_answers(self, d):
        self.answers.yview_scroll(d * 5, "units")

    # ---------- hotkeys ----------
    def _register_hotkeys(self):
        try:
            import keyboard
        except Exception as e:
            self.notice("Global hotkeys unavailable (pip install keyboard): %s" % e, "warn", 8000)
            return
        hk = self.st["hotkeys"]
        handlers = {
            "toggleVisibility": self.toggle_visibility, "answer": lambda: self.ask(kind="manual"), "screenshotAnswer": self.screenshot_answer,
            "clear": self.clear, "regenerate": self.regenerate, "toggleClickThrough": self.toggle_click_through, "togglePrivate": self.toggle_private,
            "toggleListening": self.toggle_listening, "toggleAutoAnswer": lambda: self.toggle_setting("autoAnswer"),
            "moveLeft": lambda: self.move_by(-40, 0), "moveRight": lambda: self.move_by(40, 0), "moveUp": lambda: self.move_by(0, -40), "moveDown": lambda: self.move_by(0, 40),
            "growOverlay": lambda: self.grow(40), "shrinkOverlay": lambda: self.grow(-40), "scrollUp": lambda: self.scroll_answers(-1), "scrollDown": lambda: self.scroll_answers(1),
        }
        try:
            keyboard.unhook_all_hotkeys()
        except Exception:
            pass
        failed = []
        for name, combo in hk.items():
            fn = handlers.get(name)
            if not fn or not combo:
                continue
            try:
                keyboard.add_hotkey(combo, lambda f=fn: self.ui(f), suppress=False, trigger_on_release=False)
            except Exception as e:
                failed.append("%s=%s" % (name, combo))
                log("hotkey failed", name, combo, e)
        if failed:
            self.notice("Hotkeys not registered: " + ", ".join(failed), "warn", 8000)
        self._update_hints()

    # ---------- tray ----------
    def _start_tray(self):
        try:
            import pystray
            from PIL import Image, ImageDraw
        except Exception as e:
            log("tray unavailable", e)
            return
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse((6, 6, 58, 58), fill=(124, 156, 255, 255))
        d.ellipse((22, 22, 42, 42), fill=(14, 17, 23, 255))
        menu = pystray.Menu(
            pystray.MenuItem("Show / hide overlay", lambda: self.ui(self.toggle_visibility), default=True),
            pystray.MenuItem("Listen / stop", lambda: self.ui(self.toggle_listening)),
            pystray.MenuItem("Answer screen", lambda: self.ui(self.screenshot_answer)),
            pystray.MenuItem("Settings", lambda: self.ui(self.open_settings)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda: self.ui(self.quit)),
        )
        self.tray = pystray.Icon("agy-conure", img, "agy-conure", menu)
        threading.Thread(target=self.tray.run, daemon=True).start()

    # ---------- settings window ----------
    def open_settings(self):
        if self.settings_win is not None and self.settings_win.winfo_exists():
            self.settings_win.lift()
            return
        w = tk.Toplevel(self.root)
        self.settings_win = w
        w.title("agy-conure Settings")
        w.configure(bg=C["bg"])
        w.geometry("760x680+%d+%d" % (max(0, self.root.winfo_x() - 780), self.root.winfo_y()))
        w.attributes("-topmost", True)
        w.update_idletasks()
        set_private(w, self.st["overlay"].get("privateMode", True))
        style = ttk.Style(w)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TNotebook", background=C["bg"], borderwidth=0)
        style.configure("TNotebook.Tab", background=C["btn"], foreground=C["text"], padding=(12, 6))
        style.map("TNotebook.Tab", background=[("selected", C["on"])])
        style.configure("TFrame", background=C["bg"])
        style.configure("TLabel", background=C["bg"], foreground=C["text"])
        style.configure("TCheckbutton", background=C["bg"], foreground=C["text"])
        style.map("TCheckbutton", background=[("active", C["bg"])])
        style.configure("TCombobox", fieldbackground=C["panel2"], background=C["btn"], foreground=C["text"])
        nb = ttk.Notebook(w)
        nb.pack(fill="both", expand=True, padx=10, pady=10)
        vars_ = {}

        def tab(name):
            f = ttk.Frame(nb)
            nb.add(f, text=name)
            return f

        def row(parent, label, widget_fn, r, hint=None):
            ttk.Label(parent, text=label).grid(row=r, column=0, sticky="nw", padx=8, pady=6)
            wdg = widget_fn(parent)
            wdg.grid(row=r, column=1, sticky="ew", padx=8, pady=6)
            if hint:
                ttk.Label(parent, text=hint, foreground=C["muted"]).grid(row=r + 1, column=1, sticky="w", padx=8)
            parent.columnconfigure(1, weight=1)
            return wdg

        def entry(key, val):
            v = tk.StringVar(value=str(val if val is not None else ""))
            vars_[key] = v
            return lambda p: tk.Entry(p, textvariable=v, bg=C["panel2"], fg=C["text"], insertbackground=C["text"], relief="flat")

        def combo(key, val, values):
            v = tk.StringVar(value=str(val))
            vars_[key] = v
            return lambda p: ttk.Combobox(p, textvariable=v, values=values, state="normal")

        def check(key, val, text=""):
            v = tk.BooleanVar(value=bool(val))
            vars_[key] = v
            return lambda p: ttk.Checkbutton(p, variable=v, text=text)

        def text(key, val, h=6):
            def mk(p):
                t = tk.Text(p, height=h, bg=C["panel2"], fg=C["text"], insertbackground=C["text"], relief="flat", wrap="word", font=self._font(9))
                t.insert("1.0", val or "")
                vars_[key] = t
                return t
            return mk

        s = self.st
        # AI tab
        f = tab("AI (agy)")
        models = [m for m, _ in self.agy.list_models()] or [s["llm"]["model"]]
        row(f, "Model", combo("llm.model", s["llm"]["model"], models), 0, "Models come from `agy models`. Fast: gemini-3.8-flash-low. Strong: claude-sonnet-4-6.")
        row(f, "Keep a warm agy process", check("llm.warm", s["llm"].get("warm", True), "answers start in ~3 s instead of ~10 s"), 2)
        row(f, "agy.exe", entry("_agy", AGY_EXE), 4, "Set the AGY_EXE environment variable to override.")
        ttk.Label(f, text="Nothing else to configure: agy uses your Antigravity login, no API key needed.", foreground=C["muted"]).grid(row=6, column=0, columnspan=2, sticky="w", padx=8, pady=12)
        # Assistant tab
        f = tab("Assistant")
        a = s["assistant"]
        row(f, "Mode", combo("assistant.mode", a["mode"], ["interview", "meeting", "sales", "support"]), 0)
        row(f, "Answer style", combo("assistant.answerStyle", a["answerStyle"], ["concise", "detailed", "star"]), 1)
        row(f, "Response language", entry("assistant.responseLanguage", a["responseLanguage"]), 2, "auto = same language as the question")
        row(f, "Auto-answer detected questions", check("assistant.autoAnswer", a["autoAnswer"]), 4)
        row(f, "Min. words for auto-answer", entry("assistant.minQuestionWords", a["minQuestionWords"]), 5)
        row(f, "Transcript lines as context", entry("assistant.contextWindowUtterances", a["contextWindowUtterances"]), 6)
        row(f, "Previous answers as context", entry("assistant.historyTurns", a["historyTurns"]), 7)
        row(f, "Include transcript context", check("assistant.includeTranscriptContext", a["includeTranscriptContext"]), 8)
        row(f, "Custom instructions", text("assistant.customInstructions", a["customInstructions"], 6), 9)
        # Context tab
        f = tab("Context")
        c = s["context"]
        row(f, "Resume / background", text("context.resumeText", c["resumeText"], 7), 0)
        row(f, "Job description / goal", text("context.jobDescription", c["jobDescription"], 5), 1)
        row(f, "Notes / talking points", text("context.notes", c["notes"], 4), 2)
        docs_lbl = ttk.Label(f, text="", foreground=C["muted"], wraplength=520)
        docs_lbl.grid(row=3, column=1, sticky="w", padx=8)

        def refresh_docs():
            docs_lbl.configure(text="Documents: " + (", ".join("%s (%d chars)" % (d["name"], len(d.get("text", ""))) for d in c["documents"]) or "none"))
        refresh_docs()

        def add_doc():
            p = filedialog.askopenfilename(parent=w, title="Add document", filetypes=[("Documents", "*.pdf;*.docx;*.txt;*.md"), ("All", "*.*")])
            if not p:
                return
            try:
                txt = extract_text(p)
                c["documents"].append({"id": uuid.uuid4().hex[:8], "name": os.path.basename(p), "text": txt[:60000]})
                refresh_docs()
            except Exception as e:
                messagebox.showerror("agy-conure", "Could not read document: %s" % e, parent=w)

        def clear_docs():
            c["documents"].clear()
            refresh_docs()
        bf = ttk.Frame(f)
        bf.grid(row=4, column=1, sticky="w", padx=8, pady=4)
        self._btn(bf, "Add document (pdf / docx / txt)", add_doc).pack(side="left", padx=(0, 6))
        self._btn(bf, "Remove all", clear_docs, "ghost").pack(side="left")
        # Speech tab
        f = tab("Speech & audio")
        au, stt = s["audio"], s["stt"]
        row(f, "Capture call / system audio", check("audio.captureSystem", au["captureSystem"], "WASAPI loopback of the default speakers"), 0)
        row(f, "Capture microphone", check("audio.captureMic", au["captureMic"]), 1)
        mics = ["default"]
        try:
            mics += ["%d: %s" % (i, n) for i, n in AudioCapture(lambda *a: None, lambda *a: None).list_mics()]
        except Exception as e:
            log("mic list failed", e)
        cur_mic = au.get("micDevice", "default")
        cur_lbl = next((m for m in mics if m.split(":")[0] == str(cur_mic)), "default")
        row(f, "Microphone", combo("audio.micDevice", cur_lbl, mics), 2)
        row(f, "Punctuation & casing", check("stt.punctuation", stt["punctuation"]), 3)
        row(f, "End of sentence silence (ms)", entry("stt.endpointSilenceMs", stt["endpointSilenceMs"]), 4, "lower = faster finals, more fragments")
        row(f, "CPU threads", entry("stt.numThreads", stt["numThreads"]), 6)
        ttk.Label(f, text="Engine: local sherpa-onnx Zipformer (English, bundled with Conure). Changes apply on the next Listen.", foreground=C["muted"]).grid(row=8, column=0, columnspan=2, sticky="w", padx=8, pady=12)
        # Overlay tab
        f = tab("Overlay")
        o = s["overlay"]
        row(f, "Opacity (0.3 - 1)", entry("overlay.opacity", o["opacity"]), 0)
        row(f, "Font scale (0.8 - 1.6)", entry("overlay.fontScale", o["fontScale"]), 1, "needs restart")
        row(f, "Private mode by default", check("overlay.privateMode", o["privateMode"]), 3)
        row(f, "Show tray icon", check("general.showTray", s["general"]["showTray"]), 4)
        row(f, "Launch at login", check("general.launchAtLogin", s["general"]["launchAtLogin"], "adds a shortcut to the Startup folder"), 5)
        # Session tab
        f = tab("Session")
        se = s["session"]
        row(f, "Save transcripts", check("session.saveTranscripts", se["saveTranscripts"]), 0)
        row(f, "Auto notes after session", check("session.autoNotes", se["autoNotes"]), 1)
        row(f, "Save folder", entry("session.saveDir", se["saveDir"]), 2, "empty = %s" % SESSIONS_DIR)
        self._btn(f, "Open sessions folder", lambda: os.startfile(se.get("saveDir") or SESSIONS_DIR)).grid(row=4, column=1, sticky="w", padx=8, pady=8)
        # Hotkeys tab
        f = tab("Hotkeys")
        for i, (k, v) in enumerate(s["hotkeys"].items()):
            row(f, k, entry("hotkeys." + k, v), i)
        ttk.Label(f, text="Syntax of the `keyboard` package, e.g. ctrl+alt+h, ctrl+shift+space. Applied on Save.", foreground=C["muted"]).grid(row=40, column=0, columnspan=2, sticky="w", padx=8, pady=8)

        def save():
            try:
                for key, v in vars_.items():
                    if key.startswith("_"):
                        continue
                    sect, name = key.split(".", 1)
                    val = v.get("1.0", "end").strip() if isinstance(v, tk.Text) else v.get()
                    if key == "audio.micDevice":
                        val = "default" if str(val).startswith("default") else str(val).split(":")[0]
                    cur = s[sect][name]
                    if isinstance(cur, bool):
                        val = bool(val)
                    elif isinstance(cur, int):
                        val = int(float(val))
                    elif isinstance(cur, float):
                        val = float(val)
                    s[sect][name] = val
                s.save()
            except Exception as e:
                messagebox.showerror("agy-conure", "Invalid value: %s" % e, parent=w)
                return
            self.root.attributes("-alpha", max(0.3, min(1.0, float(s["overlay"]["opacity"]))))
            self._set_toggle(self.b_auto, s["assistant"]["autoAnswer"])
            self._set_model_label()
            self._register_hotkeys()
            set_launch_at_login(bool(s["general"]["launchAtLogin"]))
            self.stt = None  # re-create with new STT settings on next Listen
            threading.Thread(target=self.agy.warm, daemon=True).start()
            self.notice("Settings saved", "info", 2000)
            w.destroy()
        bb = tk.Frame(w, bg=C["bg"])
        bb.pack(fill="x", padx=10, pady=(0, 10))
        self._btn(bb, "Save", save, "primary").pack(side="right")
        self._btn(bb, "Cancel", w.destroy, "ghost").pack(side="right", padx=6)
        self._btn(bb, "Open log", lambda: os.startfile(LOG_FILE), "ghost").pack(side="left")

    # ---------- lifecycle ----------
    def quit(self):
        try:
            if self.listening:
                self.stop_listening(silent=True)
            self.agy.shutdown()
            if self.tray:
                self.tray.stop()
            try:
                import keyboard
                keyboard.unhook_all()
            except Exception:
                pass
        finally:
            self.root.after(200, self.root.destroy)

    def run(self):
        self.root.mainloop()


# ----------------------------------------------------------------------------
# misc helpers
# ----------------------------------------------------------------------------
def extract_text(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        from pypdf import PdfReader
        return "\n".join((p.extract_text() or "") for p in PdfReader(path).pages)
    if ext == ".docx":
        import docx
        return "\n".join(p.text for p in docx.Document(path).paragraphs)
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def startup_shortcut_path():
    return os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs", "Startup", "agy-conure.vbs")


def set_launch_at_login(on):
    p = startup_shortcut_path()
    try:
        if on:
            pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
            if not os.path.isfile(pyw):
                pyw = sys.executable
            script = os.path.abspath(__file__)
            with open(p, "w", encoding="utf-8") as f:
                f.write('Set sh = CreateObject("WScript.Shell")\nsh.CurrentDirectory = "%s"\nsh.Run """%s"" ""%s""", 0, False\n' % (APP_DIR, pyw, script))
        elif os.path.isfile(p):
            os.remove(p)
    except Exception as e:
        log("launch at login failed", e)


def single_instance():
    """Return False if another agy-conure is already running (named mutex)."""
    try:
        k32 = ctypes.windll.kernel32
        k32.CreateMutexW(None, False, "Local\\agy-conure-single-instance")
        return k32.GetLastError() != 183  # ERROR_ALREADY_EXISTS
    except Exception:
        return True


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    if not single_instance():
        log("already running")
        return
    log("starting", APP_NAME, "python", sys.version.split()[0])
    app = App()
    app.run()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log("fatal", traceback.format_exc())
        raise
