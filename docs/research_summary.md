# Heterogeneous RL Training with Mac + GPU: Research Proposal Summary

*Comprehensive summary of a research design session covering hardware infrastructure, training frameworks, SWE agentic RL, iOS agent RL, and benchmark creation.*

---

## Executive Summary

This document captures a multi-part research proposal for an open-source framework that enables **agentic reinforcement learning training at academic lab scale** using a heterogeneous compute cluster combining NVIDIA GPU nodes (for gradient steps) with Apple Silicon Mac Studios (for rollout generation). The proposal covers the core infrastructure, two primary application domains — software engineering (SWE) agents and iOS device control agents — and a companion evaluation benchmark for iOS agents.

The central thesis is that a genuine gap exists in the open-source ecosystem: no framework supports disaggregated RL rollout across GPU + Apple Silicon, no iOS RL training harness exists, and no iOS benchmark analogous to AndroidWorld has been built. These three gaps are connected, and addressing them together produces a coherent, novel contribution.

---

## Part 1: Heterogeneous RL Training Infrastructure

### 1.1 The Gap

All major open-source agentic RL training frameworks — VeRL (ByteDance), OpenRLHF, NeMo-RL (NVIDIA), ROLL (Alibaba), SkyRL (Berkeley), AReaL (Ant Research) — are built entirely on the CUDA stack. Their rollout engines use vLLM or SGLang, which require NVIDIA GPUs. No framework handles the specific combination of:

- NVIDIA GPU nodes for training (gradient computation, optimizer state)
- Apple Silicon Mac nodes for rollout generation (episode sampling, environment interaction)
- Weight synchronization between PyTorch on GPU and MLX on Apple Silicon
- Prefix KV cache management for long-context agentic rollouts on Mac

### 1.2 Why Apple Silicon for Rollout?

The case for Mac is not that it is faster or cheaper per FLOP than H100 — it is not. The case rests on three specific properties:

**Memory capacity per dollar.** A Mac Studio M3 Ultra at $5,999 provides 256 GB of unified memory. An H100 provides 80 GB. For long-context agentic rollout (32k–64k token contexts with many parallel episodes), KV cache is the binding constraint. Mac fits 4–5× more parallel episodes in memory than H100 at similar price points.

**Already-owned hardware.** Most academic labs have Mac Studios and Mac Minis used for development. Their marginal cost for rollout is electricity (~$0.022/hr). Against cloud H100 at $2.00/hr, this is the strongest possible economic argument.

**Bandwidth-bound decode at long context.** LLM token generation is memory-bandwidth-limited. At 819 GB/s (M3 Ultra) vs 3,350 GB/s (H100), Mac has a 4.1× bandwidth disadvantage. But Mac's memory advantage means it fits 4–5× more episodes per pass, nearly canceling the bandwidth gap for decode-dominated workloads.

### 1.3 The Cost Analysis

Using the key metric of **cost per trajectory** for a realistic SWE agentic RL run (64 prompts × 8 rollouts = 512 episodes, 35 turns, 500 token generation + 750 token env response per turn, 13B BF16 model):

| Hardware | Wall time (512 traj.) | $/hr amortized | Cost per trajectory |
|---|---|---|---|
| H100 cloud ($2.00/hr) | ~4.9 hours | $2.00 | $0.0180 |
| H100 owned (3yr amort.) | ~4.9 hours | $1.39 | $0.0125 |
| Mac Studio M3U 256GB × 5 (owned, 5yr) | ~4.0 hours | $0.855 total | **$0.0052** |
| MacStadium M3U 192GB × 5 (cloud $1/hr) | ~4.0 hours | $5.00 total | $0.0195 |

The owned Mac advantage is 2.5–3.5× cheaper per trajectory vs cloud H100. The MacStadium (cloud Mac) option is slightly more expensive than cloud H100 because the Mac's 3.7× slower per-episode throughput exceeds the 2× price advantage.

**Key insight**: MacStadium never beats cloud H100 on cost per trajectory. The cost advantage of Mac is entirely from owned hardware amortization, not from intrinsic efficiency.

### 1.4 The Prefill Problem

Prefill (processing input tokens) is compute-bound, not bandwidth-bound. The M3 Ultra has ~26 TFLOPS FP16 vs H100's ~989 TFLOPS — a 38× gap. For agentic rollout with 750 token env responses per turn:

```
Prefill FLOPs per episode per turn = 
    2 × 13B × 750 tokens          (linear layers, constant)
  + 4 × 40 layers × 5120 dim × 750 × context_length   (attention, grows with context)
```

The attention component grows linearly with total context — FlashAttention reduces memory from O(n²) to O(n) but does not reduce FLOPs. This means Mac's prefill penalty is real and grows with context, dominating wall time for long episodes. The decode/prefill split in the corrected model:

- H100: ~80% decode, ~20% prefill (fast on both)
- Mac: ~35% decode, ~65% prefill (decode acceptable, prefill the bottleneck)

### 1.5 Prefix KV Cache as the Key Optimization

For SWE agentic RL where all episodes in a batch share the same initial code context (system prompt + repo description + initial task), prefix KV caching provides a structural advantage:

**Without prefix caching**: each new turn prefills the full accumulated context → O(n²) total prefill cost.

**With prefix caching**: shared prefix (8–32k tokens) is computed once and reused. Each turn only prefills new tokens (750 env response tokens). This reduces prefill by 8–9× for a 15-turn episode.

The shared prefix fits in a fixed pinned region of Mac's unified memory. Mac's 256 GB advantage over H100's 80 GB means it can pin a much larger shared prefix while still fitting more parallel episodes than H100.

A simplified but effective implementation (no custom Metal kernels required):
1. Compute shared prefix KV tensor once at rollout start
2. Store as a pinned MLX array in unified memory
3. At each turn, concatenate cached KV with new private KV
4. Pass to attention

Full RadixAttention (SGLang-style) on Metal would require custom Metal compute shaders for non-contiguous paged KV storage — 3–6 months of systems engineering. The simplified version is 4–6 weeks and captures most of the benefit.

### 1.6 The M5 Ultra Projection

The M5 Max was officially released with **614 GB/s bandwidth** (vs M3 Max's 400 GB/s). M5 Ultra = 2× M5 Max = **1,228 GB/s bandwidth**, a 50% improvement over M3 Ultra's 819 GB/s. GPU compute improvement is ~60% (50% graphics + Neural Accelerators for matrix multiply). This uniformly improves both decode and prefill, reducing M5 Ultra wall time to roughly 65% of M3 Ultra. 5 M5 Ultra nodes would match an H100 in wall time for the target workload.

### 1.7 Framework Architecture

The proposed framework makes three engineering contributions, each scoped to a specific existing codebase:

**Contribution 1 — MacRolloutExecutor (fork of OpenRLHF)**

OpenRLHF's `AgentExecutorBase` provides a clean hook for custom rollout backends via `--agent_func_path`. Subclassing it to route HTTP requests to a Mac inference cluster takes ~200 lines:

```python
class MacRolloutExecutor(AgentExecutorBase):
    async def execute(self, prompts, sampling_params):
        # Sticky routing: same SWE task → same Mac node (prefix cache hit)
        responses = await self.mac_cluster.generate(prompts)
        return responses
    
    async def update_weights(self, state_dict):
        # Broadcast safetensors over HTTP to all Mac nodes
        await asyncio.gather(*[
            node.post("/update_weights", state_dict)
            for node in self.mac_nodes
        ])
```

**Contribution 2 — `/update_weights` endpoint (fork of vllm-mlx)**

vllm-mlx provides an OpenAI-compatible server with continuous batching and prefix caching for Apple Silicon. It needs one addition: a FastAPI endpoint that accepts a PyTorch state dict and hot-reloads model weights without server restart:

```python
@app.post("/update_weights")
async def update_weights(payload: WeightPayload):
    # Accept safetensors over HTTP
    # Convert PyTorch BF16 → MLX arrays (via numpy view, zero-copy)
    # Update model.parameters() in-place
    # Call mx.eval() to materialize on GPU
    pass
```

**Contribution 3 — Sticky prefix router**

All episodes from the same SWE task instance should route to the same Mac node, so the shared prefix KV cache is reused across all parallel rollouts for that task. A simple consistent-hash router on task_id achieves this in ~50 lines.

**Weight synchronization strategy**: Checkpoint-based async sync (Mechanism C). Trainer writes safetensors to shared storage after each gradient update. Mac nodes poll and reload. This introduces a one-training-step lag (standard in async RL, supported by OpenRLHF's `--async_train`) and requires no CUDA-IPC or NCCL involvement.

---

## Part 2: SWE Agentic RL Recipe

### 2.1 Why SWE as the Demonstration Task

Software engineering agent benchmarks (SWE-Bench) are the dominant evaluation setting for agentic RL research in 2025. Key properties that make SWE ideal for demonstrating this infrastructure:

- **Long context**: real codebases mean 8–32k token contexts, maximizing Mac's memory advantage
- **Long episodes**: 15–50 turns of think + tool call + env response
- **High rollout cost**: environment execution (Docker + bash + test runners) is slow, creating GPU-starved rollout phases where compute efficiency matters
- **Programmatic reward**: test pass/fail is binary and unambiguous — no judge model needed
- **Established benchmarks**: SWE-Bench Verified, R2E-Gym provide ready evaluation infrastructure

### 2.2 The Rollout Structure

Each SWE episode follows:

```
Turn 1–N:
  1. Prefill: env response (~750 tokens) → compute-bound
  2. Decode: model thinking + tool call (~500 tokens) → bandwidth-bound
  3. Environment: Docker bash execution → CPU/IO, model idle
  4. Repeat
```

The environment execution step (step 3) creates natural GPU idle time. This is where Mac cluster design pays off: the Mac nodes can be processing other episodes' decode steps during this idle time, while the environment containers run concurrently on the Mac's CPU cores.

Memory breakdown for 40 parallel SWE episodes on M3 Ultra 256 GB (turn 15, 20k context):
- Model weights: 26 GB
- Shared prefix KV (8k tokens): ~1.3 GB (one copy, reused by all 40 episodes)
- Private KV per episode (12k private tokens): ~1.9 GB × 40 = 76 GB
- Total: ~103 GB — fits comfortably in 256 GB

### 2.3 RL Algorithm

For SWE agentic RL, the recommended algorithm is **REINFORCE++ or GRPO** (rather than PPO) because:
- No critic model needed — saves 13B parameters of VRAM on GPU
- Binary pass/fail reward works well with GRPO's group-relative advantage
- Simpler to implement correctly than PPO with GAE

Training setup for a 13B dense model:
- Training: 2× H100 (or A100 80GB) with DeepSpeed ZeRO-3 + gradient checkpointing
- Rollout: 4–8 Mac Studios M3 Ultra 256 GB (disaggregated, async)
- Batch: 64 prompts × 8 rollouts = 512 episodes
- Training step time: ~20 minutes (2–4 gradient steps)
- Target rollout time: ~20 minutes (matching training step)

---

## Part 3: iOS Agentic RL

### 3.1 The Unique Position of iOS for RL Training

The Android mobile agent ecosystem has mature infrastructure: Docker-containerized Android Virtual Devices (AVDs) run headlessly on any Linux machine, enabling 1,000+ parallel environments on a GPU cluster. The entire field (MobileRL, AndroidWorld, AndroidLab, SPA-Bench) builds on this.

iOS cannot be containerized. Apple's macOS EULA prohibits running iOS simulators on non-Apple hardware. Every parallel iOS environment requires a Mac. This explains why no iOS RL training work exists — until you have a Mac cluster purpose-built for RL rollout, it's not feasible.

The proposed Mac cluster infrastructure from Part 1 is precisely the missing piece that unlocks iOS agentic RL.

### 3.2 iOS Simulator Capabilities Relevant to RL

**Internet access**: Full, unrestricted. The simulator uses the Mac's native network connection. Maps search, Safari browsing, and all network calls work exactly as on a real device. Optionally throttlable via Network Link Conditioner.

**Multiple simulators**: No hard limit. 14+ simultaneous instances are documented in production CI workflows. On M3 Ultra 256 GB, 40–50 simulators fit alongside the inference server (each uses ~500 MB–1.5 GB RAM).

**Screenshots**: `xcrun simctl io $UDID screenshot frame.png` — latency ~50–150ms. Continuous video via `idb video-stream --fps N --format h264`.

**Accessibility tree**: `idb ui describe-all --udid $UDID --json` — latency **~120ms**, returns full JSON hierarchy. Per benchmarks: 3.4× more token-efficient and 16× faster than screenshot analysis.

**AXObserver**: Push-based notification system. Rather than polling the tree, register for `AXUIElement` change notifications and get called back in <5ms when the UI settles after an action. Eliminates fixed sleep() waits between turns.

**State reset**: `xcrun simctl io $UDID snapshot save/restore clean_state` — resets to a known state in ~2 seconds between episodes.

### 3.3 Observation Design: Accessibility Tree vs Screenshot

For RL training, the accessibility tree primary / screenshot fallback design is preferred over pure vision:

| Property | Accessibility Tree | Screenshot |
|---|---|---|
| Latency | ~120ms | ~100–2,000ms (analysis) |
| Token cost | ~50 tokens | ~1,000–2,000 tokens |
| Enables text-only LLM | ✅ Yes | ❌ No (requires VLM) |
| Works on all apps | ❌ ~70% of apps | ✅ All apps |
| Reward verification | ✅ Reads underlying DB directly | ❌ Requires screenshot parsing |
| Structured for RL | ✅ Deterministic, diffable | ❌ Noisy, ambiguous |

Apple's first-party apps (all 20 Tier A and B apps in the benchmark) have excellent accessibility tree coverage. Third-party apps vary — custom canvas and games are the failure modes.

The hybrid approach: use the accessibility tree as the primary observation. If a required element has no accessibility label (empty `AXLabel`, no `AXUniqueId`), fall back to a targeted screenshot of that element's bounding box. This covers 95%+ of first-party app surfaces.

### 3.4 The Observation Loop Per Turn

```
Agent generates action (text LLM via MLX on Mac GPU, ~N seconds)
         ↓
Execute action: idb ui tap/type/swipe (~5ms)
         ↓
AXObserver fires when UI settles (<5ms–500ms depending on animation)
         ↓
Fetch accessibility tree: idb ui describe-all (~120ms)
         ↓
Optionally: screenshot for elements with missing AX labels (~100ms)
         ↓
Feed to model as next turn's observation
```

Total observation overhead: **~200–700ms per turn** — negligible against model generation time.

### 3.5 Reward Design

For iOS tasks, rewards are read from the underlying data stores in the simulator's filesystem, not from parsing UI. This makes rewards deterministic and tamper-proof:

```python
# Example: alarm task reward function
def is_successful(udid, target_time="17:00"):
    sim_path = f"~/Library/Developer/CoreSimulator/Devices/{udid}/data"
    alarms = sqlite3.connect(f"{sim_path}/Containers/.../alarm.db")
    return any(alarm.time == target_time for alarm in alarms)
```

Apps supported with database-level reward verification: Clock (alarm.db), Calendar (EventKit SQLite), Reminders (RemindersDB), Contacts (AddressBook), Notes (NoteStore.sqlite), Files (filesystem), Health (HealthKit SQLite), Settings (preferences plists).

---

## Part 4: iOSWorld — iOS Agent Evaluation Benchmark

### 4.1 The Gap in Existing Benchmarks

| Benchmark | Platform | Tasks | Apps | Programmatic reward | Year |
|---|---|---|---|---|---|
| AndroidWorld | Android only | 116 | 20 | ✅ | 2024 |
| AndroidLab | Android only | 138 | 9 | ✅ | 2024 |
| MobileRL | Android only | ~1000 | multi | ✅ | 2025 |
| **iOSWorld (proposed)** | **iOS only** | **160–200** | **20** | **✅** | **2026** |

No published iOS benchmark with programmatic rewards exists. The reason: until a Mac RL cluster exists, building one at scale is not feasible.

### 4.2 App Selection

Apps organized by RL training suitability:

**Tier A — Prime RL targets** (8–15 steps, full local reward, excellent AX tree):
Shortcuts, Health, Settings, Files, Calendar, Reminders, Notes, Maps, Contacts, Clock, Photos, Mail

**Tier B — Good secondary** (5–7 steps, partial/full reward):
Messages, Podcasts, Keynote, Pages, Numbers, Safari, Music, Phone, Home, Voice Memos, Books

**Exclude**: Camera (no hardware in simulator), FaceTime (requires active call), Weather/Stocks/TV (network-only), Calculator (stateless), Freeform (custom canvas, no AX tree)

**Apps that make iOSWorld uniquely iOS**: Shortcuts (no Android equivalent — enables deep sequential automation tasks up to 25 steps), Health (HealthKit is iOS-exclusive), Maps+Messages ETA sharing (tight iOS integration with no Android counterpart).

### 4.3 Task Taxonomy

Tasks are parameterized templates across five structural flow types:

**Flow A — Create → Schedule**: Make content in one app, time-anchor it in another (Notes → Calendar)

**Flow B — Create → Communicate**: Create or find content, share it to a person (Calendar → Messages)

**Flow C — Capture → Organize**: Capture something new, file it with structure (Voice Memos → Notes → Files)

**Flow D — Plan → Track**: Set a goal, add monitoring (Health → Reminders)

**Flow E — Research → Act**: Discover information, take downstream action (Safari → Reminders, Maps → Contacts)

**Flow F — Contact → Coordinate**: A person is the hub, apps connect around them (Contacts → Calendar → Mail)

**Flow G — Build → Automate**: Shortcuts workflow orchestrates other apps (Shortcuts → Health → Reminders)

**Flow H — Multi-step Compound**: Two or more flows chained across 3 apps

### 4.4 Three New Task Categories Beyond "Create"

Beyond simple creation tasks, the benchmark includes three categories that require different agent capabilities:

**Search-then-Act (Flow S)**: Agent must query real-world information (Maps place search, Safari web search) and use the result in a downstream UI action. The generator cannot hardcode the expected value — only the query and result type. Verifier checks that the downstream field is non-empty and plausible.

*Example*: "Find the nearest Starbucks to Union Square. Create a Calendar event there with Alex."

**Fetch/Read (Flow R)**: Environment is pre-populated with specific data. Agent reads and reports it. Verification checks the agent's text response against pre-populated ground truth — not a database write. Includes negative variant: entity doesn't exist, agent must report "not found" rather than hallucinating.

*Example*: "When is my Haircut appointment?" (pre-populated with a 3pm Wednesday event)

**Update (Flow U)**: Entity exists with an old value. Agent finds it and changes a specific field. Verifier checks new value is present AND old value is gone.

*Example*: "Update Greg's phone number to 415-555-0199" (pre-populated with Greg having 650-555-0144)

### 4.5 Task Generator Architecture

A Python task generator produces parameterized tasks across all flows. Key design decisions:

**Mandatory vs Optional parameters**:
- `MandatoryParam`: always in instruction, always in verifier. Task is undefined without it (alarm time, contact name, folder destination).
- `OptionalParam`: appears in instruction AND verifier only if sampled. Task is valid without it (alarm sound, note tag, event recurrence). Verifier never checks what the instruction didn't specify.

**`detail_level` parameter (0.0–1.0)**:
- `0.0` → minimal tasks (only mandatory params, ~3–5 steps)
- `0.5` → medium density (some optional params, ~7–12 steps)
- `1.0` → maximum specification (all optional params, ~12–20 steps)

This enables curriculum learning: train on `detail_level=0.0` tasks first, gradually increasing to `1.0` as the agent improves.

**Initial state variants**:
- `present`: entity exists, agent navigates to it. Setup command pre-creates it via simctl.
- `absent`: entity doesn't exist, agent must create it first. Adds 2–5 steps.
- `blocking`: entity is missing and task requires it. Correct behavior is graceful refusal, not hallucination. Reward: agent reports failure.

**`PrePopulatedData`**: for Fetch and Update tasks, declares specific field values that must be injected into the simulator before the episode starts. Includes `setup_commands` using simctl to inject the data.

### 4.6 Benchmark Scale and Novelty

Target scope: **160–200 tasks across 20 apps** (matching AndroidWorld's scope), with:
- ~8–10 task templates per app
- Each template generating thousands of parameterized variants via `CANONICAL` value sampling
- Three detail levels × three initial state variants = 9 difficulty configurations per template
- Cross-app tasks (2-app and 3-app) covering all flow combinations

**Novel contributions vs AndroidWorld**:

1. **Shortcuts orchestration tasks** — deepest sequential tasks in any mobile benchmark (15–25 steps), no Android equivalent
2. **iMessage + Maps + Calendar pipeline** — exploits iOS-specific tight integration
3. **Three new task categories** — Search-then-act, Fetch/read, Update — go beyond AndroidWorld's pure creation tasks
4. **AX-tree-primary reward** — deterministic, token-efficient, enables text-only LLM training without VLM
5. **iOS-exclusive apps** — HealthKit, Shortcuts, iMessage deep links have no Android counterparts

### 4.7 Legal Status

Using the iOS simulator for this purpose is legally sound:
- Using Xcode/simctl for automated testing is explicitly the intended purpose and is industry-standard in CI/CD
- Reading accessibility tree data from simulator apps generates no copyright-protectable content (functional metadata, not creative works)
- Publishing the benchmark as task scripts + reward functions (not the simulator itself) matches the model of all Android benchmarks
- The one clear prohibition: do not distribute the iOS simulator or Xcode tools

---

## Part 5: Summary of Contributions

The proposed work produces five connected contributions:

| Contribution | What it is | Who benefits |
|---|---|---|
| **HeteroRL framework** | OpenRLHF fork + vllm-mlx fork enabling GPU training + Mac rollout | Academic labs doing agentic RL with owned Mac hardware |
| **SWE agentic RL recipe** | End-to-end training recipe for 9B–13B SWE agents on the hybrid cluster | Labs wanting to reproduce/extend SkyRL, AReaL-scale work cheaply |
| **iOS RL training harness** | iOS simulator environment layer: multi-episode management, AX tree observations, reward functions | First group to train RL agents on iOS tasks |
| **iOSWorld benchmark** | 160–200 programmatic tasks across 20 Apple apps, 3 task categories, parameterized generator | Entire iOS agent community — first evaluatable benchmark |
| **iOS task generator** | Python library generating infinite parameterized task variants with detail level and initial state control | Training data pipeline for iOS agent research |

---

## Part 6: Open Engineering Questions

Several non-trivial engineering problems remain to be solved:

**Weight sync latency**: How fast can safetensors be transferred from GPU training node to Mac cluster? For a 13B BF16 model that's 26 GB. Over 10 GbE LAN: ~21 seconds. Over 25 GbE: ~8 seconds. This sets the minimum async lag between training steps. Quantization to INT8 halves transfer time.

**MLX prefix caching**: vllm-mlx has prefix caching but not full paged KV. Building a two-tier slab allocator (Pool A: 32k max, Pool B: 64k max) with episode-level pre-allocation avoids custom Metal kernels while capturing most of the memory efficiency benefit.

**iOS simulator CPU saturation**: When 40 simulators all respond to taps simultaneously, 40–80 CPU cores spike briefly. M3 Ultra has 24 cores. Staggering interactions via natural episode divergence and capping simultaneous active interactions at 10–12 mitigates this.

**AX tree quality for novel apps**: For apps with poor accessibility coverage (custom canvas, games), the fallback to screenshots requires a VLM rather than the text LLM. The benchmark should explicitly classify which tasks require which observation modality.

**Simulator reset speed**: `xcrun simctl snapshot restore` takes ~2 seconds. For 512 episodes with 10 second average episodes, reset adds ~1% overhead — negligible. For very short episodes (<10 turns), consider episode queuing to amortize reset cost.

---

## Appendix: Key Numbers for Reference

| Parameter | Value | Source |
|---|---|---|
| M3 Ultra memory bandwidth | 819 GB/s | Apple spec |
| M5 Ultra memory bandwidth (est.) | 1,228 GB/s | 2× M5 Max at 614 GB/s |
| H100 SXM memory bandwidth | 3,350 GB/s | NVIDIA spec |
| A100 80GB memory bandwidth | 2,000 GB/s | NVIDIA spec |
| 13B BF16 model size | 26 GB | 13B × 2 bytes |
| KV per token (13B, BF16, GQA) | 0.16 MB | 40L × 2 × 8H × 128D × 2B |
| M3 Ultra KV budget (256 GB) | ~224 GB | 256 − 26 − 6 |
| H100 KV budget (80 GB) | ~50 GB | 80 − 26 − 4 |
| AX tree latency (idb) | ~120ms | XC-MCP benchmarks |
| Screenshot latency (simctl) | ~50–150ms | Apple docs |
| AXObserver callback latency | <5ms | macOS AX API |
| Mac Studio M3 Ultra 256 GB price | $5,999 | Apple list price (2025) |
| H100 80 GB market price (used/new) | $10,000–$27,000 | Secondary market 2025–2026 |
| Mac amortization period | 5 years | Standard for inference hardware |
| GPU amortization period | 3 years | Standard datacenter practice |
| Mac owned $/hr (5yr, 80% util) | $0.171/hr | $5,999 / (5 × 8,760 × 0.8) + power |
| H100 cloud $/hr | ~$2.00/hr | Market rate 2026 |
| MacStadium M3U 192GB $/hr | ~$1.00/hr | Estimated from community reports |
| idb AX tree vs screenshot token ratio | 3.4× cheaper | XC-MCP benchmarks |
| idb AX tree vs screenshot speed ratio | 16× faster | XC-MCP benchmarks |

---

*Document generated from a multi-session research design discussion. All cost estimates are based on market prices as of early 2026 and hardware specifications at that time. M5 Ultra bandwidth is an extrapolation from confirmed M5 Max specs.*


---

## Part 7: Legal Considerations — Naming, Release, and Apple's IP

*Note: This section provides an analysis of the relevant legal landscape for research and planning purposes. It is not legal advice. Consult an IP attorney before making final naming and release decisions, particularly if the project becomes commercially significant.*

### 7.1 Apple's Trademark Position — What Is Actually Protected

Apple's trademark portfolio is extensive. The following are registered trademarks relevant to this project:

**Apple-owned registered marks:** Mac, MacBook, MacBook Pro, Mac mini, macOS, iPhone, iPad, iPadOS, Xcode, Swift, iMessage, FaceTime, Siri, App Store, iCloud, HomePod, and many others.

**"iOS" specifically:** This is the most important nuance for this project. iOS is actually a trademark or registered trademark of Cisco in the U.S. and other countries — Apple uses "iOS" under license from Cisco. This matters because Apple's own guidelines technically only restrict Apple-owned marks. iOS sits in a legally unusual position: it is not Apple's trademark to enforce, it is Cisco's.

**What Apple's guidelines actually prohibit for third parties:**

You may not use or register, in whole or in part, Apple, iPod, iTunes, Macintosh, iMac, or any other Apple trademark, including Apple-owned graphic symbols, logos, icons, or an alteration thereof, as or as part of a company name, trade name, product name, or service name except as specifically noted in these guidelines.

The compatibility exception: developers may use Apple, Macintosh, iMac, or any other Apple word mark in a referential phrase to describe that a third-party product is compatible with the referenced Apple product or technology, provided the Apple word mark is not part of the product name.

This is the critical distinction: **referential use is permitted, use as a project name is not.**

### 7.2 What This Means Concretely for Each Term

| Term | Apple's status | Using in project name | Using descriptively in docs/paper |
|---|---|---|---|
| **iOS** | Cisco trademark, Apple licensee | ⚠️ Risky — not Apple's mark to enforce but Cisco's | ✅ Universally done in academia |
| **Mac** | Apple registered trademark | ❌ Prohibited as project name per Apple guidelines | ✅ Permitted as compatibility descriptor |
| **macOS** | Apple registered trademark | ❌ Prohibited as project name | ✅ Permitted descriptively |
| **Apple Silicon** | Apple trademark | ❌ Prohibited as project name | ✅ Permitted descriptively |
| **Xcode** | Apple registered trademark | ❌ Prohibited as project name | ✅ Permitted descriptively |
| **iPhone** | Apple registered trademark | ❌ Prohibited as project name | ✅ Permitted descriptively |
| **Simulator** | Generic word | ✅ Fine — generic, not trademarked | ✅ Fine |
| **Accessibility** | Generic word | ✅ Fine | ✅ Fine |
| **M3 Ultra / M5 Ultra** | Apple chip names (trademarked) | ❌ Risky as project name | ✅ Fine in technical description |

### 7.3 Academic Research Precedent — What the Field Actually Does

The research community has a well-established pattern of using platform names in project names without legal challenge. Every major Android benchmark uses "Android" prominently:

- **AndroidWorld** (Google DeepMind, 2024)
- **AndroidLab** (Tsinghua, 2024)
- **Android in the Wild** (Google, 2023)
- **MobileAgentBench** (uses "Android" throughout)

Google owns the "Android" trademark and has made no moves against academic benchmark projects using it descriptively. This sets a strong practical precedent.

Similarly, thousands of open-source repositories on GitHub use "iOS" in their names — `open-source-ios-apps` has tens of thousands of stars, `ios-simulator-mcp` is a public MCP server, `ios-task-generator` appears routinely. Apple has never taken action against academic or open-source repositories using "iOS" as a descriptive term in a project name.

The distinction Apple's guidelines draw is between commercial products that could imply Apple affiliation and descriptive use by developers and researchers clearly building tools for Apple platforms. The former is prohibited; the latter has decades of unenforced precedent.

### 7.4 The Real Risk Hierarchy

**Low risk (precedent is on your side):**
- Using "iOS" in the benchmark name (Cisco's mark, not Apple's; universally used in academia)
- Using "iOS" or "Apple" descriptively in paper titles, README files, and documentation
- Publishing the benchmark scripts, reward functions, and task generator on GitHub
- Describing the framework as "for Apple Silicon" or "for macOS"

**Medium risk (be careful with framing):**
- Using "Mac" as a core word in the framework name (Apple trademark; avoid as standalone)
- Any name that could imply official Apple endorsement or affiliation
- Using Apple's app icons or product photographs in documentation

**High risk (do not do):**
- Using the Apple logo anywhere
- Naming the project "Apple RL" or "Apple Agent Framework" (direct trademark use as product name)
- Claiming the project is "approved by Apple" or "official Apple tooling"
- Distributing Xcode, the iOS Simulator, or any Apple SDK components

### 7.5 Naming Recommendations

Given the legal landscape and practical precedent, here are concrete naming options at different risk levels:

**Option A — Fully safe, no Apple trademarks in name:**

| Component | Suggested name | Rationale |
|---|---|---|
| Framework | **HeteroRL** | Describes heterogeneous RL — no trademarks |
| Framework | **MacRollout** | "Mac" alone is borderline; "rollout" makes it descriptive of function, not a brand |
| Framework | **AppleSilicon-RL** | Uses Apple Silicon as descriptor with a hyphen; same pattern as "CUDA-RL" |
| Benchmark | **PhoneAgentBench** | Fully generic; describes phone agents |
| Benchmark | **MobileAgentWorld** | Generic; follows naming pattern of AndroidWorld |
| Benchmark | **DeviceAgentBench** | Fully generic |

**Option B — Descriptive use, widely precedented:**

| Component | Suggested name | Rationale |
|---|---|---|
| Framework | **iOSRollout** | "iOS" is Cisco's mark, widely used in open source; clearly descriptive |
| Benchmark | **iOSWorld** | Direct analog to AndroidWorld; "iOS" as platform descriptor |
| Benchmark | **iOSAgentBench** | Follows academic naming conventions |
| Task generator | **ios-task-generator** | GitHub repository naming; thousands of precedents |

**Option C — Most defensible academic framing:**

Follow the pattern that AndroidWorld, AndroidLab, and ALE (Arcade Learning Environment) established. Use the platform name as a descriptor in the project title, make clear in all documentation that the project is an independent academic research tool and is not affiliated with or endorsed by Apple, and add a standard disclaimer in the README and paper.

The AndroidWorld paper includes no special trademark disclaimers and uses "Android" freely throughout — this is the established norm in benchmark papers.

**Recommended naming for this project specifically:**

- **Framework (GitHub repo):** `hetero-rl` or `mac-rollout` — the hyphenated form is conventional for GitHub repos and reads as descriptive rather than a product name
- **Benchmark:** `iOSWorld` — clean, memorable, directly comparable to AndroidWorld, uses Cisco's mark (iOS) not Apple's, has massive open-source precedent
- **Paper title:** "iOSWorld: A Benchmark for Training and Evaluating Agents on iOS Applications" — academic papers routinely use trademarked platform names in titles without issue
- **Dataset name:** `iosworld-tasks` or `ios-agent-bench` — again, following the androidworld-tasks convention

### 7.6 What to Put in the README and Paper

Every major open-source project that uses platform names includes a boilerplate disclaimer. Use something like:

> *This project is an independent academic research tool and is not affiliated with, endorsed by, or sponsored by Apple Inc. "iOS," "Mac," "macOS," and other Apple product names are trademarks of their respective owners and are used here solely to identify the platforms with which this software is compatible.*

This language:
- Explicitly denies affiliation (the main thing Apple's guidelines prohibit implying)
- Uses marks descriptively (the permitted use)
- Is standard practice in the open-source ecosystem
- Would be your first line of defense in any trademark dispute, making it clear there is no consumer confusion about the project's relationship to Apple

### 7.7 Dataset Release Strategy

For the benchmark dataset specifically, the release model should follow AndroidWorld's approach exactly:

**Release:** Task descriptions, reward functions, task generator Python code, evaluation harness scripts, setup commands

**Do not release:** iOS Simulator binaries, Xcode tools, any Apple SDK component, screenshots of Apple apps (copyright risk), Apple app icons

The dataset as described — parameterized task templates, Python generator code, reward verification functions — contains no Apple-copyrightable content. The task descriptions ("Create an alarm at 6:45 AM") are factual instructions, not protectable expression. The reward functions are original code. The canonical value lists are original data. This is analogous to how AndroidWorld releases task code without including Android OS components.

**Licensing for the dataset and framework:**

Use **Apache 2.0** or **MIT**. Both are standard for ML research code. Apache 2.0 has an explicit patent grant which is slightly stronger protection for contributors. Either is fine — the choice matters more for community adoption than legal protection.

### 7.8 Summary Recommendation

Use **iOSWorld** for the benchmark and **HeteroRL** (or **mac-rollout**) for the framework. Add a one-paragraph disclaimer to both READMEs and the paper. Release the code under MIT or Apache 2.0. Do not distribute Apple tooling. This puts the project on the same legal footing as every major Android benchmark and thousands of iOS open-source projects — none of which have faced trademark enforcement from Apple or Cisco in an academic/open-source context.

---

## Part 8: Simulator Compatibility and App Availability

### 8.1 iOS 26 App Availability

Verified against iOS 26.3 simulator (build 23D8133, iPhone 17). 17 of 22 target apps confirmed present:

| App | Bundle Name | Status |
|---|---|---|
| Shortcuts | Shortcuts | ✅ Present |
| Health | Health | ✅ Present |
| Settings | Preferences | ✅ Present |
| Files | Files | ✅ Present |
| Calendar | MobileCal | ✅ Present |
| Reminders | Reminders | ✅ Present |
| Notes | MobileNotes | ✅ Present |
| Maps | Maps | ✅ Present |
| Contacts | Contacts | ✅ Present |
| Clock | MobileTimer | ✅ Present (bundle changed) |
| Photos | Photos | ✅ Present |
| Mail | MobileMail | ✅ Present |
| Messages | MobileSMS | ✅ Present |
| Podcasts | Podcasts | ✅ Present |
| Safari | MobileSafari | ✅ Present |
| Music | Music | ✅ Present |
| Books | Books | ✅ Present |
| Keynote | — | ❌ Not in simulator |
| Pages | — | ❌ Not in simulator |
| Numbers | — | ❌ Not in simulator |
| Phone | — | ❌ Not in simulator |
| Voice Memos | — | ❌ Not found (may be renamed) |

**Key finding**: Clock app bundle ID changed from `com.apple.mobiletimer` to a new ID in iOS 26. The `simctl snapshot` command was also removed in iOS 26 — episode reset requires simulator clone instead.

**Key finding**: Keynote, Pages, Numbers, and Phone are not present in the iOS 26 simulator runtime — these are device-only apps not included in the simulator bundle. Voice Memos likely renamed; needs further investigation.

### 8.2 iOS 26 Schema Changes Discovered

**Reminders database** (confirmed via live testing):
- Path: `Containers/Shared/AppGroup/<UUID>/Container_v1/Stores/Data-<UUID>.sqlite`
  (NOT `Data-local.sqlite` which is empty)
- App group identifier: `group.com.apple.reminders`
- Table: `ZREMCDREMINDER`
- Columns confirmed: `ZTITLE`, `ZPRIORITY` (1=high, 5=medium, 9=low, 0=none), `ZFLAGGED`, `ZCOMPLETED`
- **Breaking change**: `ZFLAGGED` is always 0 in iOS 26 simulator — flagging requires iCloud sign-in. Remove `flag` from `OptionalParam` for iOS 26.
- `ZREMCDBASELIST` table exists but is empty in simulator (list names not stored locally without iCloud)

**Clock app**: Bundle `MobileTimer.app` present but launch via bundle ID fails — launch ID changed in iOS 26. Needs investigation.

### 8.3 First-Launch Dialog Problem and Solution

**The problem**: When an agent opens an app for the first time in a fresh simulator, it encounters setup dialogs (iCloud sync prompts, notification permission requests, onboarding carousels) that are not part of the task but consume turns and add noise to trajectories.

**Apps with confirmed first-launch dialogs**: Messages, Mail, Photos, Music, Podcasts, Books, Notes, Reminders, Calendar.

**Solution: Pre-warmed baseline clone**

Since `simctl snapshot` was removed in iOS 26, the replacement workflow is:

```bash
# Step 1: Create and boot a base simulator
xcrun simctl create "SIBB-Baseline" "iPhone 17" \
  "com.apple.CoreSimulator.SimRuntime.iOS-26-3"
xcrun simctl boot <BASELINE_UDID>

# Step 2: Pre-warm all target apps manually
# Launch each app, dismiss all first-launch dialogs,
# configure any required settings, then close
# Do this for all 17 available apps

# Step 3: Clone the pre-warmed simulator
# This clone becomes the "clean state" for episodes
xcrun simctl clone <BASELINE_UDID> "SIBB-Clean"

# Step 4: Per episode — clone the clean state
xcrun simctl clone <CLEAN_UDID> "SIBB-Episode-001"
# Run the episode
# After episode: delete the clone
xcrun simctl delete <EPISODE_UDID>
```

Clone time on M3 Ultra: approximately 3-8 seconds per clone. At 512 episodes per batch, total clone overhead is under 1 hour — acceptable.

**Programmatic dialog dismissal** (for automatable dialogs):

```bash
# Pre-grant notification permissions for specific apps
xcrun simctl spawn <UDID> defaults write com.apple.reminders \
  SBAppUsesLocalNotifications -bool YES

# Skip iCloud setup prompt
xcrun simctl spawn <UDID> defaults write com.apple.reminders \
  DidShowCloudKitMigrationDialog -bool YES
```

These keys vary by app and iOS version — the compatibility audit (Section 8.4) discovers them systematically.

### 8.4 Compatibility Audit Plan

A systematic audit runs once per iOS version to:
1. Detect which task actions work in the simulator
2. Detect which actions require iCloud and therefore fail silently
3. Update the compatibility matrix JSON used by the task generator
4. Detect AX tree issues via Xcode's `XCUIAccessibilityAudit`

**Audit structure:**

```python
AUDIT_ACTIONS = {
    "Reminders": [
        "create_list", "add_item", "set_priority_high",
        "flag_item",       # fails on iOS 26 without iCloud
        "set_due_date", "add_tag",
    ],
    "Calendar":  ["create_event", "set_alert", "set_recurrence", "add_location"],
    "Clock":     ["create_alarm", "set_label", "set_repeat", "enable_snooze"],
    "Notes":     ["create_note", "add_tag", "lock_note", "create_folder"],
    "Contacts":  ["create_contact", "add_phone", "add_email", "add_birthday"],
    "Health":    ["log_workout", "log_water", "add_medication"],
    "Files":     ["create_folder", "create_file", "move_file", "compress"],
    "Maps":      ["search_place", "save_pin", "add_to_contacts"],
    "Shortcuts": ["create_shortcut", "add_action", "run_shortcut"],
    "Settings":  ["configure_focus", "set_screen_time", "toggle_wifi"],
}
```

**Output**: `compatibility_ios<VERSION>.json` — consumed by task generator to zero out `include_prob` for unavailable actions.

**Schedule**: Run within 1 week of each new iOS simulator runtime release.

### 8.5 Final App Scope: SIBB-11

After live testing on iOS 26.3 simulator (Xcode 26.3, build 17C529), the confirmed available apps are:

**Available — 11 apps (SIBB-11):**

| # | App | Tier | Bundle ID |
|---|---|---|---|
| 1 | Reminders | A | com.apple.reminders |
| 2 | Calendar | A | com.apple.mobilecal |
| 3 | Contacts | A | com.apple.MobileAddressBook |
| 4 | Settings | A | com.apple.Preferences |
| 5 | Files | A | com.apple.DocumentsApp |
| 6 | Health | A | com.apple.Health |
| 7 | Maps | A | com.apple.Maps |
| 8 | Photos | A | com.apple.mobileslideshow |
| 9 | Shortcuts | A | com.apple.shortcuts |
| 10 | Safari | B | com.apple.mobilesafari |
| 11 | Messages | B | com.apple.MobileSMS |

**Unavailable in iOS 26.3 simulator — two categories:**

*App Store downloadable model (available on real device, not in simulator):*
Notes, Clock, Music, Podcasts, Books, Mail

Root cause: iOS 26 moved these apps to a downloadable-defaults model. The simulator has no App Store. The app bundles exist in the runtime filesystem but as empty resource-only stubs with no executable or Info.plist. No simctl mechanism can install or launch them.

*Not in simulator runtime at all:*
Phone, Voice Memos, Keynote, Pages, Numbers

**Re-enablement path:**
The `APP_REGISTRY` in `sibb_task_generator_v3.py` tracks availability per app with `"available": True/False`. When a future Xcode/simulator update restores these apps:
1. Set `"available": True` in `APP_REGISTRY`
2. Move the corresponding generators from `GENERATORS_PENDING` into `ALL_GENERATORS`
3. Run the compatibility audit for that app

**iOS 26 DB findings from live testing:**

Reminders database confirmed at:
`Containers/Shared/AppGroup/<UUID>/Container_v1/Stores/Data-<UUID>.sqlite`
(not `Data-local.sqlite` which is empty)

Schema: `ZREMCDREMINDER` table, columns `ZTITLE`, `ZPRIORITY` (1=high, 5=medium, 9=low), `ZFLAGGED`, `ZCOMPLETED`

Known limitation: `ZFLAGGED` is always 0 in iOS 26.3 simulator without iCloud sign-in. Remove `flag` from `OptionalParam` for Reminders tasks targeting iOS 26.
