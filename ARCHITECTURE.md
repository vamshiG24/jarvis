# MARK LV — Architecture

> **Maintained by Gowni Vamshi** · based on [MARK LV](https://github.com/FatihMakes/Mark-LV) by FatihMakes (CC BY-NC 4.0)

This document shows how the assistant is put together: the processes and
threads it runs, how audio and tool calls flow through it, and how a Live
session survives the server dropping it. The diagrams are [Mermaid](https://mermaid.js.org/)
and render directly on GitHub; an ASCII version of the overview comes first for
reading in a terminal.

---

## 1. The big picture

```text
                ┌──────────────────────────────── YOUR COMPUTER ─────────────────────────────────┐
                │                                                                                │
  🎤 Mic ──────▶│  sounddevice ──▶ EchoGuard / Wake word / PTT gate ──▶ out_queue ─┐             │
                │                                                                  │             │
  🔊 Speakers ◀─│  RawOutputStream ◀── audio_in_queue ◀──┐                         ▼             │       ┌────────────────────┐
                │        │ visemes / level               │                ┌─────────────────┐    │  WSS  │  Gemini Live API   │
                │        ▼                               └────────────────│   JarvisLive    │◀───┼──────▶│  gemini-3.8-live   │
                │  ┌───────────────┐   Qt signals    ┌────────────┐       │  (main.py,      │    │       │  (stable)          │
                │  │  PyQt6 HUD    │◀───────────────▶│ JarvisUI   │◀─────▶│   asyncio loop) │    │       └────────────────────┘
                │  │  (ui.py)      │                 │  facade    │       └───────┬─────────┘    │       ┌────────────────────┐
                │  │ Nexus · Face  │                 └────────────┘               │ tool calls   │ HTTPS │ Gemini REST models │
                │  │ · Core        │                                              ▼              │◀─────▶│ (one-shot ladder,  │
                │  └───────────────┘                      ┌────────────────────────────────────┐ │       │  core/gemini.py)   │
                │                                         │ Inline tools │ actions/*.py │ plugins│ │       └────────────────────┘
                │  📱 Phone ◀── HTTPS :8000 ──▶ Dashboard │ (vision,     │ (open_app,   │ (drop- │ │
                │               (FastAPI)  commands/audio │  memory, …)  │  files, web…)│  in)   │ │
                │                                         └──────────────┬─────────────────────┘ │
                │                         memory/long_term.json ◀────────┘  config/api_keys.json │
                └────────────────────────────────────────────────────────────────────────────────┘
```

```mermaid
flowchart LR
    subgraph USER["👤 User"]
        MIC["🎤 Microphone"]
        SPK["🔊 Speakers"]
        KB["⌨️ Keyboard / text box"]
        PHONE["📱 Phone browser"]
    end

    subgraph APP["🖥️ MARK LV process"]
        direction TB
        subgraph UI_THREAD["Qt main thread — ui.py"]
            HUD["HudCanvas<br/>Nexus · Face · Core"]
            PANELS["Telemetry tiles · Activity log<br/>Drawers · Overlays"]
            FACADE["JarvisUI facade<br/>(thread-safe signals)"]
        end
        subgraph LOOP["asyncio thread — main.py"]
            LIVE["JarvisLive<br/>session lifecycle"]
            TOOLS["Tool dispatcher"]
        end
        subgraph REG["Skill registries"]
            INLINE["Inline tools<br/>screen_process · save/recall_memory<br/>undo · manage_monitor · shutdown"]
            ACTIONS["actions/*.py<br/>auto-discovered TOOL dicts"]
            PLUGINS["plugins/*.py<br/>drop-in, crash-isolated"]
        end
        DASH["Dashboard<br/>FastAPI · HTTPS :8000"]
        MEM[("memory/long_term.json")]
        CFG[("config/api_keys.json")]
    end

    subgraph GOOGLE["☁️ Google"]
        GLIVE["Gemini Live API<br/>bidirectional WebSocket"]
        GREST["Gemini REST models<br/>one-shot ladder"]
    end

    MIC --> LIVE
    LIVE --> SPK
    KB --> FACADE
    PHONE <--> DASH
    DASH --> LIVE
    FACADE <--> LIVE
    HUD --- FACADE
    PANELS --- FACADE
    LIVE <-->|"PCM 16 kHz up · PCM 24 kHz down<br/>tool calls · transcripts"| GLIVE
    LIVE --> TOOLS
    TOOLS --> INLINE & ACTIONS & PLUGINS
    ACTIONS & PLUGINS -->|"core/gemini.py"| GREST
    INLINE <--> MEM
    LIVE <--> CFG
```

---

## 2. Threads and tasks

The app runs two long-lived threads. Qt owns the main thread; everything that
talks to Gemini runs on one asyncio loop in a daemon thread. They only meet
through `JarvisUI`, whose methods emit Qt signals, so no widget is ever touched
from the wrong thread.

```mermaid
flowchart TB
    subgraph MAIN["Main thread"]
        QAPP["QApplication.exec()<br/>MainWindow · HudCanvas 60 Hz step / 20–30 Hz paint"]
    end
    subgraph RUNNER["Daemon thread: asyncio.run(JarvisLive.run())"]
        direction TB
        RUN["run() — connect / recover loop"]
        subgraph TG["asyncio.TaskGroup — one per Live session"]
            T1["_send_realtime<br/>out_queue → WebSocket"]
            T2["_listen_audio<br/>mic stream (callback thread)"]
            T3["_receive_audio<br/>server messages · tools · GoAway"]
            T4["_play_audio<br/>audio_in_queue → speakers · visemes"]
            T5["_watch_reconnect<br/>voluntary rebuilds"]
            T6["_run_system_monitor"]
            T7["_run_background_monitor"]
            T8["_run_proactive_mode"]
            T9["_run_sleep_watch"]
            T10["_relay_phone_audio"]
        end
        BG["Lifetime tasks<br/>dashboard.serve · dashboard command relay"]
    end
    subgraph POOL["Thread pool (run_in_executor)"]
        EXE["Blocking tool handlers<br/>actions · plugins · screen capture"]
    end
    RUN --> TG
    T3 --> EXE
    RUNNER <-->|"Qt signals via JarvisUI"| MAIN
```

If any task in the group raises, the whole group is torn down and `run()`
decides what to do next (see §5). One failure path covers every way a session
can end.

---

## 3. Audio pipeline

```mermaid
flowchart LR
    MIC["🎤 Mic<br/>16 kHz int16<br/>1024-sample blocks"] --> CB["Input callback"]
    CB --> WAKE{"Wake-word<br/>asleep?"}
    WAKE -->|yes| DET["openWakeWord detector<br/>(own thread, local)"]
    DET -.->|Hey Jarvis| WAKE
    WAKE -->|no| SPEAK{"Assistant<br/>speaking?"}
    SPEAK -->|yes| DROP1["dropped"]
    SPEAK -->|no| TAIL{"Echo tail<br/>active?"}
    TAIL -->|own echo| DROP2["dropped"]
    TAIL -->|real voice or no tail| PTT{"Push-to-talk on<br/>and key not held?"}
    PTT -->|yes| DROP3["dropped"]
    PTT -->|no| OQ["out_queue"] --> WS["send_realtime_input(audio)"] --> GL["Gemini Live"]

    GL --> RX["receive()"] --> SLICE["50 ms slices<br/>(fast interrupt)"] --> AQ["audio_in_queue"]
    AQ --> BATCH["≤200 ms batch"] --> OUT["RawOutputStream<br/>24 kHz"] --> SPK["🔊"]
    BATCH --> VIS["FFT visemes<br/>openness · width · level"] --> HUD["HUD mouth / sphere / waveform"]
    BATCH --> EG["EchoGuard.note_output<br/>(learns own voice)"]
    EG -.-> TAIL
```

* **Echo tail.** `stream.write()` returns before the sound has left the
  speakers, so for `output latency + 250 ms` after speaking the microphone is
  filtered by `core/echo.py`: blocks that match the assistant's own voice are dropped, and
  a real reply still gets through.
* **Visemes.** `_pcm_visemes()` reads F1/F2 formant energy from each 20 ms of
  outgoing audio. That drives the face's mouth, the Nexus sphere's ripple and the
  waveform, all from one source so they stay in sync.

---

## 4. A tool call, end to end

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant G as Gemini Live
    participant R as _receive_audio
    participant X as _execute_tool
    participant H as Handler (thread pool)
    participant UI as HUD

    U->>G: "Open WhatsApp" (streamed PCM)
    G-->>R: tool_call open_app {app_name}
    R->>UI: set_state(THINKING)
    R->>X: execute(fc)
    X->>H: run_in_executor(action.handler)
    H-->>X: "Opened WhatsApp."
    X-->>R: FunctionResponse(id, result)
    Note over X,R: scheduling only on 2.5 models —<br/>3.x Live is synchronous-only
    R->>G: send_tool_response()
    opt screen_process captured an image
        R->>G: send_client_content(image + question)
    end
    G-->>R: audio + transcription
    R->>UI: log line · visemes · SPEAKING
```

Tools come from three registries, checked in this order:

| Registry | Where | How it's found |
|---|---|---|
| Inline | `TOOL_DECLARATIONS` in `main.py` | Tied to live-session state (vision, memory, shutdown) |
| Actions | `actions/*.py` | Any module exporting a `TOOL` dict (`core/action_loader.py`) |
| Plugins | `plugins/*.py` | Drop-in files, validated and crash-isolated (`core/plugin_loader.py`) |

Names can't collide: actions can't shadow inline tools, and plugins can't shadow either.

---

## 5. Session lifecycle and recovery

This is the part that changed most in this build. The Live server can end a
session in several ways, and each one now has its own way back.

```mermaid
stateDiagram-v2
    [*] --> Connecting
    Connecting --> Online: connect ok
    Connecting --> Recovering: quota / model unavailable
    Online --> Refreshing: GoAway (connection lifetime)
    Refreshing --> Connecting: resume with handle,<br/>between turns
    Online --> Connecting: voice / device change<br/>(voluntary rebuild)
    Online --> Recovering: 1011 internal error
    Online --> Offline: network drop (1006) / other
    Recovering --> Connecting: drop poisoned handle,<br/>seed transcript recap,<br/>backoff 1→2→5→10→20→30 s
    Offline --> Connecting: backoff
    Connecting --> Connecting: resumed session dies within 20 s<br/>→ drop handle, recap
```

**How each failure is handled**

| Failure | Detected by | What happens |
|---|---|---|
| `1011 Internal error encountered` | close code from the leaf exception | Resumption handle **dropped** (resuming replays the broken state). A fresh session starts with the last 24 turns in its system instruction. Gentle backoff if it repeats. |
| `1011 Resource has been exhausted` / 429 | `core/gemini.is_quota_error` | Step down the Live model ladder. |
| Resumed session dies within 20 s | connection age | Handle dropped, conversation carried over from the transcript. |
| `GoAway` from server | `response.go_away` | Rebuild on the resumption handle at the next quiet moment, before the deadline. |
| Voice change | `_ReconnectSignal(keep_context=False)` | New session (the voice is fixed at connect), with the conversation seeded from the transcript. |
| Bad API key | `api key not valid` | Setup screen reopens and the app waits for a new key. |
| Unsupported config field (1007/1008, INVALID_ARGUMENT) | leaf error text | Tuning knobs dropped first, then proactive audio. |
| Network drop (1006, timeouts) | close code / error text | Exponential backoff, up to 60 s. |

**Why "leaf" exceptions.** A session runs in a `TaskGroup`, so failures arrive
wrapped in an `ExceptionGroup` whose message is *"unhandled errors in a
TaskGroup"*. The old code matched keywords against that wrapper text, and
`"unhandled"` contains `"handle"`. So every failure after a resume looked like a
rejected resumption handle, and quota errors raised mid-session were never
recognised. `_leaf_errors()`, `_close_code()` and `_error_text()` in `main.py`
always look inside the group.

**Model-aware config.** `_live_caps(model)` returns what each Live
generation accepts, because the generations disagree, sometimes in opposite
directions. The live ladder is `gemini-3.8-live` → `gemini-3.1-flash-live-preview`
→ `gemini-2.5-flash-native-audio`, and `live_model` in the config can put another
model, such as `gemini-3.8-live-extended-thinking`, in front.

| Setting | 2.5 native-audio | 3.1 flash live | 3.8 live *(default)* |
|---|---|---|---|
| Proactive audio | ✅ configurable (`v1alpha`) | ❌ not supported | always on, and setting the field is an **error**, so it's never sent |
| Context-window compression | ✅ on | ❌ off by default (the identified 1011 trigger) | off by default (`auto`) |
| Thinking | `thinking_budget` | `thinking_level` | **no `thinking_config` allowed** (interleaved reasoning is built in) |
| Tool calls | `behavior` as declared | sync only, so `behavior`/`scheduling` are stripped | **async by default**, so every tool is declared `BLOCKING` (a tool that asks for `NON_BLOCKING` keeps it and gets `scheduling`) |

**Continuity across restarts and model changes.**

| Situation | Mechanism |
|---|---|
| App crash, kill or restart | `core/session_state.py` keeps the last 24 turns, the unanswered request and a clean-exit flag on disk. It's written atomically within 5 s of a change and touched every 30 s. The next launch seeds a **fresh** session from it and sends a `[RESUMED]` turn that asks before finishing anything left half-done. No morning briefing after a crash. |
| Why not the old resumption handle | Resuming a session killed mid-answer replayed that answer and sometimes stopped responding (measured on 3.8). Handles are used only for reconnects within one run. |
| Quota on the main model | Step down the Live ladder. The handle is model-specific, so the conversation crosses over through the transcript. A per-day 429 rests the model until midnight Pacific; others rest 5 → 10 → 20 → 40 → 60 min. |
| Main model recovers | `_watch_primary` checks every 30 s, probes the better model with one real turn, and switches back in a quiet moment with the transcript. |
| New API key | `core/gemini.api_key()` re-reads the file when it changes, and the setup screen merges the key into the config instead of replacing it. |

**3.8 resume quirk.** Right after a connection closes, 3.8 refuses a resume with
a 1011 for about 1.5 s (gemini-live-api-examples #60). Deliberate reconnects
therefore wait 2 s before resuming, and a resume refused at once is retried a
single time before falling back to a fresh session with the transcript.

---

## 6. UI composition

The HUD canvas is the **backdrop of the whole window**: one widget behind
everything else, sized to the window. The layout sits on top of it, with
opaque floating panels and an empty, transparent slot in the centre. The
canvas centres the scene on that slot (`area_provider`).

```mermaid
flowchart TB
    subgraph WIN["MainWindow — frameless"]
        direction TB
        CANVAS["HudCanvas — full-window backdrop<br/>Stark 3D scene · Nexus · Face · Core"]
        subgraph LAYOUT["Layout on top of the canvas"]
            direction TB
            HDR["_DragBar header (opaque) — drag to move<br/>logo · ⚙ · 🎛 · J.A.R.V.I.S. title · link chip · clock · window controls"]
            subgraph BODY["Body"]
                direction LR
                LEFT["HoloPanel: SYSTEM DIAGNOSTICS<br/>CPU · MEM · NET · GPU · TMP · DISK · PWR tiles"]
                subgraph CENTER["Centre (QSplitter)"]
                    STACK["QStackedWidget<br/>0 transparent HUD slot · 1 Camera · 2 Video"]
                    CONTENT["Content panel · Quiz panel"]
                end
                RIGHT["HoloPanel: COMMS // CONTROL<br/>Activity log · File drop · Command · Interrupt · Mic"]
            end
            FTR["Footer (opaque) — shortcuts · credits · size grip"]
        end
    end
    OVER["Floating overlays<br/>Setup · Customise · Audio devices · Memory · Plugins<br/>Remote QR · Confirm banner · Clipboard · Camera preview"]
    CANVAS -.->|centres scene on| STACK
    WIN --- OVER
```

**Why the panels are opaque.** The scene repaints 20–30 times a second. Qt
skips everything under an opaque widget when it repaints the widget behind it.
With translucent header and footer bars, every frame also repainted fifteen
labels and buttons; now only the canvas and three empty containers repaint.
`HoloPanel` paints its glass look once into a cached pixmap.

**HUD styles.** These live in `HudCanvas`; the 🎛 HUD button cycles through them and the choice is saved as `hud_style`:

| Style | What it draws |
|---|---|
| `stark` *(default)* | `core/hud3d.py`, a 3D projection table. Seven gyroscope rings on different axes, an arc-reactor core, a perspective grid floor with a projector beam, drifting particles, an audio equalizer crown, floor sonar pulses, CPU/MEM arc gauges, a heading tape and a boot sequence. The camera orbits slowly and leans toward the mouse. |
| `nexus` | A 340-point Fibonacci sphere with orbit rings, a voice trail, a radar sweep and corner telemetry. |
| `face` | The original holographic head with formant-driven lip-sync and facial acting (`core/avatar.py`). |
| `core` | The original reactor core. |

Everything is drawn in software with `QPainter`, so no GPU or OpenGL is needed.
The Stark scene uses numpy for rotation and perspective projection, batches
geometry into depth buckets (near is bright and thick, far is faint), draws the
faint floor without antialiasing, and caches its background. That comes to
about 10 ms a frame, dropping to about 10 fps when the window isn't focused.

```mermaid
flowchart LR
    STEP["HudCanvas._step (60 Hz)<br/>audio level · visemes · state"] --> SCENE["StarkScene.step<br/>ring spin · camera orbit + mouse lean<br/>audio history · sonar pulses"]
    SCENE --> PAINT["StarkScene.paint (10–30 Hz)"]
    PAINT --> B1["cached background"] --> B2["floor grid + sonar (no AA)"] --> B3["projector beam · particles"] --> B4["voice crown"] --> B5["gyroscope rings (depth-bucketed)"] --> B6["arc reactor"] --> B7["gauges · heading tape · boot text"]
```

### Holograms

A hologram is a tool answer that lands on the canvas instead of in the log. It
is drawn by the same canvas over whichever HUD style is active. The scene is
dimmed (`StarkScene.focus`) while one is up.

```mermaid
flowchart TB
    ASK["“show me a water molecule”"] --> LIVE["Gemini Live → holo_show(subject)"]
    LIVE --> QUICK["_design — STRUCTURED ladder<br/>3.1-flash-lite, JSON mode, SPEC_GUIDE<br/>2–5 s"]
    QUICK --> SPEC["scene JSON<br/>sphere · box · cylinder · cone · torus · ring<br/>polyline · label · orbit / spin"]
    SPEC --> BUILD["holo3d.from_spec<br/>parsed + clamped, never executed<br/>→ HoloModel (vertices, edges, orbs, labels)"]
    FILE["holo_show(file_path) .obj / .stl"] --> MESH["holo3d.from_mesh<br/>welded, feature edges only"] --> MODEL
    BUILD --> MODEL["HoloModel"]
    MODEL -->|"JarvisUI.show_hologram → _holo_sig"| VIEW["HudCanvas.holo — HoloView<br/>rotation matrix · offset · scale · request id"]
    LIVE -.->|"background thread"| RICH["_refine — STRUCTURED_RICH ladder<br/>3.8-flash … 45 s per model"]
    RICH -->|"refine_hologram(model, id)<br/>same request id and < 100 s, else dropped"| VIEW
    VIEW --> PAINTH["HoloView.paint<br/>glow · lit orbs back-to-front · depth-bucketed edges<br/>labels (nearest first, no overlaps) · title card"]
```

Input reaches the same `HoloView` three ways. All of them change only its pose
on the Qt thread:

```mermaid
flowchart LR
    MOUSE["_HoloInput — the transparent centre slot<br/>drag · right-drag · wheel · double-click · close chip"] --> VIEW["HoloView<br/>rotate · move · zoom · roll · reset · close"]
    VOICE["holo_control tool<br/>rotate_left … zoom_in … spin_on … close"] -->|"_holo_cmd_sig"| VIEW
    CAM["HandTracker thread (core/hands.py)<br/>webcam → MediaPipe HandLandmarker (VIDEO, 2 hands)"] --> GM["GestureMapper<br/>pinch / fist / two-hand · smoothing<br/>3-frame hold · resting-hand filter"]
    GM -->|"_hands_sig (hands, ops)"| VIEW
    GM -->|"cursors"| HUDC["hand cursors on the HUD"]
```

`HandTracker` owns the webcam only while hand control is on. While it does,
`hands.latest_frame()` gives the camera snapshot tool and the live camera panel
its frames, so they don't try to open the device a second time. Frames are
kept in memory only: one frame at a time, replaced by the next.

---

## 7. Module map

```mermaid
flowchart LR
    MAINPY["main.py"] --> UIPY["ui.py"]
    MAINPY --> CORE
    MAINPY --> MEMPKG
    MAINPY --> ACTS
    UIPY --> AVATAR["core/avatar.py<br/>core/avatar_mesh.py"]
    UIPY --> MEMPKG
    subgraph CORE["core/"]
        GEM["gemini.py — one-shot model ladder"]
        AL["action_loader.py"]
        PL["plugin_loader.py"]
        ECHO["echo.py"]
        VIS["viseme.py"]
        AD["audio_devices.py"]
        WW["wake_word.py"]
        HK["hotkey.py"]
        UNDO["undo.py"]
        CONF["confirm.py"]
        KF["known_folders.py — real Desktop / Downloads"]
        H3D["hud3d.py — Stark scene"]
        HOLO["holo3d.py — hologram models + view"]
        HANDS["hands.py — webcam gestures"]
        PROMPT["prompt.txt"]
    end
    subgraph MEMPKG["memory/"]
        MM["memory_manager.py"]
        CM["config_manager.py"]
    end
    subgraph ACTS["actions/"]
        A1["open_app · browser_control · file_controller"]
        A2["web_search · screen_processor · video_player"]
        A3["computer_settings · computer_control · desktop"]
        A4["reminder · weather_report · send_message · …"]
        A5["holo_show · holo_control"]
    end
    DASHB["dashboard/server.py"] --> MAINPY
```

---

## 8. Data on disk

| File | Contents | In git? |
|---|---|---|
| `config/api_keys.json` | Gemini key, OS, names, voice, colour, toggles, plugin settings | ❌ ignored |
| `config/certs/` | Self-signed TLS key and certificate for the phone dashboard | ❌ ignored |
| `memory/long_term.json` | What the assistant remembers about you, plus session summaries | ❌ ignored |
| `memory/session_state.json` | The last 24 lines of conversation, for continuing after a crash or restart | ❌ ignored |
| `models/hand_landmarker.task` | MediaPipe hand model, downloaded the first time hand control is switched on | ❌ ignored |
| `core/prompt.txt` | System-prompt template; `{tokens}` are filled in at connect | ✅ |
| `core/face_model.obj` | MediaPipe canonical face (Apache-2.0) | ✅ |
