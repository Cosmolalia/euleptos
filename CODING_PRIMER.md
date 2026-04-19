# Coding Primer — Cognitive Operating System

This is not a language reference. The syntax is in your weights. This is the cognitive frame that activates the difference between a developer who writes code that works and an engineer who builds systems that cannot fail unexpectedly.

---

## THE ONE PRINCIPLE

**Understand the system before you touch it. Then touch it as little as possible.**

Every line you write is a liability. Every line you don't write is a feature that never breaks. The master's primary tool is restraint informed by deep comprehension. The novice's failure mode is action driven by shallow pattern matching.

Kent Beck: "Make the change easy, then make the easy change." These are two separate steps. Never do both simultaneously. Refactoring (restructure without behavior change) and feature work (behavior change within clean structure) are different operations. Mixing them is how working systems get destroyed.

---

## UNIVERSAL ENGINEERING COGNITION

### Think in Data Flow, Not Control Flow

A program is a pipeline of data shapes. At every stage, know what the data looks like — its type, its invariants, its edge cases. If you're fighting the language with nested loops and flag variables, you've modeled the data wrong. Restructure the data and the code writes itself.

Before writing any function, answer: What goes in? What comes out? What are the failure modes of the transformation? If you can't answer these without reading surrounding code — read the surrounding code first.

### Mechanical Sympathy

Every abstraction leaks. Hold a mental model of the actual machine:

- A network call is 10^6x slower than an L1 cache hit
- `open().read()` in an async handler blocks every concurrent WebSocket
- A subprocess with `shell=True` and user input is command injection
- A hash map lookup is O(1) amortized but cache-hostile; a sorted array is cache-friendly
- A goroutine/greenlet is cheap; a thread is expensive; a process is more expensive
- `SELECT *` makes the database do work you'll discard

You don't need to optimize for these. You need to be AWARE so you don't accidentally create pathological behavior while thinking you wrote simple code.

### Boundaries, Not Interiors

Defend at system boundaries (user input, network, file I/O, external APIs). Trust within modules. A function called by other functions in your own codebase does not need to validate that its integer argument is actually an integer. But a WebSocket handler receiving JSON from a browser validates EVERYTHING.

Ousterhout's "deep modules": narrow, well-guarded interface; clean, trusting internals. Paranoid code that validates everywhere obscures logic, makes debugging harder, and often creates the failure modes it was trying to prevent.

### The Smallest Change

The hardest skill. The novice impulse is to rewrite surrounding code to match a mental model. The expert finds the one line where actual behavior diverges from intended behavior.

Before every edit, ask: "What is the MINIMUM change that solves this problem?" Then make half of that. If the minimum change is deleting a line, verify ten times that the line is actually dead. If the minimum change seems like rewriting the function, you don't understand the function yet.

### Code as Communication

Code is read 10x more than written. Naming is the primary design tool. A function name is a contract. A variable name is documentation. Cleverness is debt.

If a function is named `sendViaWebSocket()` but sends via HTTP POST, the name is a lie and the lie will burn someone. Fix the name or fix the behavior — never leave the contradiction.

---

## PYTHON — COGNITIVE FRAME

### The Mental Model

Python's data model IS the language. `__getitem__`, `__iter__`, `__enter__`, `__call__` — these aren't magic methods, they're the interface contract. Your first question about any object: "What protocols does it speak?" not "What class is it?"

Pythonic means the code's structure mirrors the problem's structure. Transform each item → comprehension. Manage a resource lifetime → context manager. Produce values lazily → generator. Fighting these shapes produces code that works but that nobody can read.

### The Six Traps

1. **Object lifetime / closure capture.** A closure captures the variable binding, not the value. The classic loop-variable bug (`for i in range: lambda: i` — all lambdas share the final `i`). Know who owns a reference and when it dies.

2. **GIL is not thread safety.** The GIL protects CPython internals, not your data. `dict[key] = value` is atomic. `dict[key] += 1` is not (read-modify-write, three bytecodes). Use threading for I/O, multiprocessing for CPU.

3. **async/await is cooperative scheduling.** Every `await` is a voluntary yield point. No `await` = you block the entire event loop. The #1 async bug: calling sync functions (file I/O, `requests.get`, CPU work) inside async handlers without `run_in_executor`. Your server's ThreadPoolExecutor for STT transcription is the correct pattern.

4. **EAFP vs LBYL boundary.** `try/except KeyError` beats `if key in dict` when the key is almost always present. But LBYL for irreversible operations — file deletion, subprocess launch, network calls. Never bare `except:` — it swallows `KeyboardInterrupt` and `SystemExit`.

5. **Import-time vs run-time.** Module-level side effects (creating connections, spawning threads, writing files) are a persistent bug source. Lazy initialization at first call, not at import.

6. **Mutable default arguments.** `def f(x=[])` — the list is created once at definition, shared across all calls. Use `None` sentinel + create inside the function.

### FastAPI / ASGI / WebSocket Patterns

- WebSocket handlers: `try: while True: data = await ws.receive_json()` / `except WebSocketDisconnect: cleanup()`. Never let exceptions propagate past the handler.
- Background tasks: `asyncio.create_task` from lifespan handler, STORE THE REFERENCE. Unreferenced tasks get garbage collected silently.
- Signal handling in async: `loop.add_signal_handler`, not `signal.signal`.
- File I/O in async: `aiofiles` or `run_in_executor`. One blocking `open().read()` freezes every concurrent connection.
- `asyncio.create_task()` without holding reference → task silently disappears.

### Error Handling Hierarchy

Catch at the boundary where you can do something meaningful:
```
Let it crash       → programming errors (fix the code)
Log and propagate  → infrastructure errors (database down, disk full)
Handle and continue → expected user errors (bad input, missing field)
Retry with backoff  → transient network errors (with a limit)
```

A database function should NOT catch its own connection errors — let them propagate to the handler that returns 503. A WebSocket loop MUST catch everything inside its `while True` because uncaught exceptions kill the connection.

---

## JAVASCRIPT — COGNITIVE FRAME

### The Mental Model

The browser runs a single-threaded event loop. Internalize Jake Archibald's model: execute synchronous code to completion → drain microtask queue (Promises, MutationObserver) → execute one macrotask (setTimeout, I/O) → render if needed.

Every piece of JS you write executes within this cycle. If you block the synchronous phase, the page freezes. If you schedule too many microtasks, rendering starves. If you don't understand when your code runs relative to rendering, you will create race conditions that only manifest under load.

### The Core Mental Models

1. **Closure scope chains.** Every function captures a reference to its entire lexical scope, not a snapshot. Closures holding references to large objects prevent garbage collection. This is the root of most JS memory leaks.

2. **`this` binding (Simpson's 4 rules, in precedence).** `new` > explicit (`call`/`apply`/`bind`) > implicit (method call) > default (`window` / `undefined` in strict). Arrow functions lexically inherit `this`. Never guess — trace the call-site.

3. **Prototype chain.** Property lookup walks up the chain. `hasOwnProperty` matters. `Object.create(null)` makes a truly empty object.

4. **`fetch` does not reject on HTTP errors.** `fetch('/api/foo')` resolves successfully on 404 and 500. You MUST check `response.ok`. This is the #1 fetch bug in AI-generated code.

### WebSocket Lifecycle

Treat the WebSocket as a state machine: CONNECTING → OPEN → CLOSING → CLOSED. Explicit transitions. Reconnection with exponential backoff and jitter. `onclose` must distinguish clean closes from failures (check `event.code` and `event.wasClean`). Keep-alive pings detect zombie connections.

State that mutated during disconnection must be reconciled on reconnect — not blindly replayed.

### DOM — The Real Rules

- **`innerHTML` with any user-derived string is XSS.** Period. Use `textContent` for text, `createElement` for structure. `innerHTML` only with static, developer-controlled strings.
- **Event delegation** over direct binding. One listener on a parent, dispatch with `event.target.closest()`. Handles dynamic children, reduces memory.
- **Detached DOM nodes** held by JS references cannot be garbage collected. `removeChild` without nullifying the variable creates a leak. Listeners on removed nodes that close over the scope chain — same problem.
- **Never read state from the DOM.** One JS object is truth. User actions update the object. A render function projects object to DOM. This is React's model without React.

### CSS — What Actually Matters

- **Specificity is a tuple** `(inline, IDs, classes, elements)`. `0,1,0,0` beats `0,0,99,99`. Never use `!important` to fix specificity — fix the selector.
- **Stacking contexts.** `z-index` only works within a stacking context. `opacity < 1`, `transform`, `filter`, `position: fixed/sticky` each create new ones. Most z-index "bugs" are stacking context misunderstandings.
- **`box-sizing: border-box`** should be universal default. Without it, `width: 100%` + `padding` overflows the parent.

---

## AI-GENERATED CODE — KNOWN FAILURE MODES

These are the specific patterns that AI coding produces that a human reviewer would catch instantly. Monitoring for these is the primary value of the self-monitoring primer.

### Hallucinated APIs
Generating function calls with signatures that don't exist. `asyncio.wait_for` with wrong kwargs. `subprocess.run` with invented parameters. FastAPI dependencies with parameter names from a different framework. **Mitigation:** If you're not 100% certain of a function's signature, check it. `help()`, docs, or reading the source. Never guess.

### Confident False Claims
Stating "this code does X" based on what code USUALLY does in training data, not what THIS code does. The `sendViaWebSocket()` that actually sends via HTTP POST. **Mitigation:** For every claim about code behavior, answer: "Did I READ this, or did I INFER this?"

### Fix by Removal
The function errors → remove the function call. The error is gone. So is the functionality. **Mitigation:** The error is in YOUR edit, not in the existing function. Revert. Fix the delta.

### Silent Exception Swallowing
`except: pass` blocks that make code "not crash" during generation while making it impossible to debug later. **Mitigation:** Never write bare except. Catch specific exceptions. Log or re-raise.

### Compulsive Rewriting
Replacing working code with "cleaner" code that subtly breaks invariants. Adding "improvements" nobody asked for. Refactoring adjacent code when tasked with a bug fix. **Mitigation:** Read the task. Do the task. Stop.

### State Coherence Drift
Editing one file without understanding its role in the whole system. Adding a parameter to a function without updating callers. Changing a message format without updating the parser. **Mitigation:** Before editing, trace the call chain. Who calls this? Who reads its output? What breaks if the interface changes?

---

## THE EXPERT'S SEQUENCE (for every coding task)

```
1. READ the task. What exactly was asked?
2. READ the code. What does it actually do right now?
3. TRACE the execution path. What calls what? What data flows where?
4. IDENTIFY the minimal change. What is the smallest edit that accomplishes the task?
5. SNAPSHOT the file (safe_edit.py begin).
6. MAKE the change. One change.
7. TEST. Does the change work? Did anything else break?
8. COMMIT the snapshot (safe_edit.py commit). Document what and why.
9. If the test failed: REVERT (safe_edit.py rollback). Go to step 4 with new understanding.
```

Do not skip steps. Do not reorder. The sequence exists because each step catches errors that the next step would compound.
