# BotoKo: Code for the Distributed Part

BotoKo is a client-server system. Browsers (and the separate program `rpc_client.py`) call **remote procedures** on the server using **JSON-RPC 2.0 over HTTP**. The server runs as several **worker processes** (one per CPU core), each with several **threads** and its own **database connection pool**. All workers share one PostgreSQL database, and the database's locks keep every ballot correct.

All excerpts below are copied from the project files. `...` marks lines that were skipped.

## JSON-RPC method names (22)

All calls go to the single endpoint `POST /rpc`. `GET /rpc` lists the methods.

| Who can call | Methods |
|---|---|
| Anyone | `get_events`, `get_results`, `verify_receipt`, `create_account`, `get_account_status` |
| Logged-in student | `get_my_events`, `get_ballot`, `cast_vote`, `get_receipt`, `register_for_event` |
| COMELEC admin | `get_worker_stats`, `open_voting`, `close_voting`, `get_turnout`, `get_accounts`, `verify_account`, `reject_account`, `verify_all_pending_accounts`, `get_registrations`, `approve_registration`, `reject_registration`, `approve_all_pending` |

## A real request and response

These were captured from the running app (on the test database `botoko_test`, with a synthetic `TEST-` student), using the exact JSON format that `static/rpc.js` sends. Every POST also carries the login cookie and an `X-CSRF-Token: <the page's CSRF token>` header.

**Request** (`POST /rpc`, `Content-Type: application/json`):
```json
{"jsonrpc":"2.0","method":"cast_vote","params":{"event_id":1,"choices":{"1":1,"2":3,"3":[6,7]}},"id":1}
```

**Response** (the ballot was recorded by worker process 7412, thread `waitress-2`):
```json
{"id":1,"jsonrpc":"2.0","meta":{"duration_ms":135.2,"thread_name":"waitress-2","worker_pid":7412},"result":{"candidates_chosen":4,"event_id":1,"positions_voted":3,"receipt_code":"51FDE50D","receipt_url":"/e/1/receipt","recorded_by":{"thread_name":"waitress-2","worker_pid":7412},"verify_url":"http://127.0.0.1:5071/verify?code=51FDE50D"}}
```

**The same request sent again** (refused with an error; a *different* worker process, 12160, answered):
```json
{"error":{"code":-32011,"data":{"receipt_url":"/e/1/receipt"},"message":"You have already voted."},"id":1,"jsonrpc":"2.0","meta":{"duration_ms":65.8,"thread_name":"waitress-1","worker_pid":12160}}
```

---

## 1. Client side RPC call (browser)

**File:** `static/rpc.js`, lines 43–73 (lines 59–66 skipped)

```javascript
  function rpc(method, params) {
    var id = nextId++;
    return fetch(ENDPOINT, {
      method: 'POST',
      credentials: 'same-origin',
      cache: 'no-store',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken() },
      body: JSON.stringify({ jsonrpc: '2.0', method: method, params: params || {}, id: id })
    }).then(function (res) {
      return res.json().catch(function () {
        throw new RpcError(CODES.NETWORK, 'The server sent an unexpected answer (HTTP ' + res.status + ').');
      });
    }, function () {
      throw new RpcError(CODES.NETWORK, 'Could not reach the server. Check your internet connection.');
    }).then(function (reply) {
      rpc.lastMeta = reply && reply.meta;
      ...
      if (reply && reply.error) {
        throw new RpcError(reply.error.code, reply.error.message, reply.error.data, reply.meta);
      }
      if (!reply || reply.id !== id) throw new RpcError(CODES.NETWORK, 'The server sent an unexpected answer.');
      return reply.result;
    });
  }
```

This is the client stub. A page calls `rpc('cast_vote', {...})` as if it were a local function. The stub **marshals** the call into a JSON-RPC 2.0 message with `JSON.stringify` and sends it with `fetch` as an HTTP POST, together with the login cookie and the CSRF token.

When the reply arrives, `res.json()` **unmarshals** it. If the reply contains `"error"`, the call fails with an `RpcError` that carries the server's code and message. Otherwise it returns `"result"`. The `id` is checked so that a reply is matched to its own request, and if the server can't be reached the call fails with code 0 (`NETWORK`).

---

## 2. Server RPC endpoint and dispatcher

### 2a. The endpoint: receive and parse

**File:** `rpc.py`, lines 191–209

```python
@bp.post("/rpc")
def rpc_endpoint():
    """The one RPC endpoint. Accepts a single request, or a batch (JSON array) of requests."""
    if not request.is_json:
        return jsonify(_error(INVALID_REQUEST, "Send the request as JSON (Content-Type: application/json).")), 415
    try:
        payload = json.loads(request.get_data(as_text=True) or "")
    except ValueError:
        return jsonify(_error(PARSE_ERROR, "Parse error: the body is not valid JSON."))
    csrf_valid = _csrf_ok()
    if isinstance(payload, list):
        if not payload:
            return jsonify(_error(INVALID_REQUEST, "Invalid Request: empty batch."))
        if len(payload) > 20:
            return jsonify(_error(INVALID_REQUEST, "Invalid Request: at most 20 calls per batch."))
        replies = [r for r in (_handle_one(item, csrf_valid) for item in payload) if r is not None]
        return jsonify(replies) if replies else Response(status=204)
    reply = _handle_one(payload, csrf_valid)
    return jsonify(reply) if reply is not None else Response(status=204)
```

This is the single network entry point for all remote procedures. It **unmarshals** the HTTP body with `json.loads` and returns the standard error `-32700` if the body is not valid JSON. It checks the CSRF token, accepts either one call or a batch of up to 20 calls, and **marshals** each reply back to JSON with `jsonify`.

### 2b. The dispatcher: validate, run the procedure, build the result or error

**File:** `rpc.py`, lines 149–188 (some lines skipped)

```python
def _handle_one(req, csrf_valid):
    ...
    is_notification = "id" not in req
    req_id = req.get("id")
    if req_id is not None and (isinstance(req_id, bool) or not isinstance(req_id, (str, int, float))):
        return _error(INVALID_REQUEST, "Invalid Request: id must be a string, a number or null.")
    method = req.get("method")
    ...
    try:
        if req.get("jsonrpc") != "2.0" or not isinstance(method, str):
            raise RpcError(INVALID_REQUEST, 'Invalid Request: needs "jsonrpc": "2.0" and a "method" string.')
        spec = METHODS.get(method)
        if spec is None:
            raise RpcError(METHOD_NOT_FOUND, f"Method not found: {method}")
        if not csrf_valid:
            raise RpcError(CSRF_FAILED, "Missing or wrong X-CSRF-Token header. Get a token with GET /rpc "
                                        "(or reload the page) and send it with every call.")
        kwargs = _bind(spec, req.get("params"))
        if spec["role"] == "student":
            _require_student()
        elif spec["role"] == "admin":
            _require_admin()
        result = spec["func"](**kwargs)
        response = {"jsonrpc": "2.0", "result": result, "id": req_id}
    except RpcError as e:
        status = f"error {e.code} {e.message}"
        response = _error(e.code, e.message, req_id, e.data)
    ...
    m = _meta(started)
    ...
    return None if is_notification else response
```

For each call, the dispatcher checks the JSON-RPC fields in order: `id`, `"jsonrpc": "2.0"`, the `method` name, the CSRF token, the `params` (`_bind` matches them against the Python function's signature) and the caller's login role. It then looks up the method in the `METHODS` table and runs the matching Python function. The reply is either `{"result": ...}` or `{"error": {"code", "message"}}`, and it includes the same `id` plus a `meta` field naming the worker process (PID) and thread that ran it.

---

## 3. RPC method table and error codes

### 3a. Error codes

**File:** `rpc.py`, lines 37–54

```python
# ---- JSON-RPC 2.0 error codes (standard) ----
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603
# ---- BotoKo application errors (the -32000..-32099 range is reserved for servers) ----
NOT_LOGGED_IN = -32001      # no student/admin session, or it expired
ADMIN_ONLY = -32002         # admin procedure called without an admin session
CSRF_FAILED = -32003        # X-CSRF-Token header missing or wrong
VOTING_CLOSED = -32010
ALREADY_VOTED = -32011
INVALID_VOTE = -32012       # candidate doesn't exist or isn't running for that position
NOT_FOUND = -32013          # e.g. receipt code that isn't yours
PASSWORD_CHANGE_REQUIRED = -32014
NOT_REGISTERED = -32015       # logged in, but not registered for this voting event
NOT_STARTED = -32016          # upcoming event: no ballot or results yet
NO_EVENT = -32017             # no voting event with that id
REGISTRATION_REFUSED = -32018  # registration: invalid details, wrong existing password, or event ended
TOO_MANY_ATTEMPTS = -32019     # rate limit per IP address (create account / Voter ID lookup)
ACCOUNT_NOT_VERIFIED = -32020  # the student's account is still waiting for COMELEC (or was rejected)
RESULTS_NOT_PUBLISHED = -32021 # voting ended, COMELEC hasn't published the official results yet
```

The first five codes are the standard JSON-RPC 2.0 errors. The rest are BotoKo's own errors, in the range JSON-RPC reserves for servers. The browser has the same list in `static/rpc.js` (`CODES`), so client and server agree on what each number means, e.g. `-32011` = already voted.

### 3b. The method table (registry)

**File:** `rpc.py`, lines 56 and 65–73, plus one registered procedure, lines 286–293

```python
METHODS = {}                # name -> {"func", "role", "doc", "params"}
...
def rpc_method(name, role="public"):
    """Register a Python function as a remote procedure. role: "public", "student" or "admin"."""
    def register(func):
        sig = inspect.signature(func)
        METHODS[name] = {"func": func, "role": role, "signature": sig,
                         "doc": (inspect.getdoc(func) or "").split("\n")[0],
                         "params": list(sig.parameters)}
        return func
    return register
...
@rpc_method("get_results")
def get_results(event_id=None):
    """Live tally of one event: votes per candidate, winners/leaders (top N for multi-seat), no-vote counts."""
    ev = _event(event_id, allow_upcoming=False)
    if not core.results_visible(ev):
        raise RpcError(RESULTS_NOT_PUBLISHED, "Voting has ended. The official results will be announced by the "
                                              "election committee soon.", {"event_id": ev["id"]})
    return core.public_results(core.compute_results(ev["id"]))     # no worker PIDs / threads: see get_worker_stats
```

`METHODS` is the method table: it maps each method name to its Python function, the role allowed to call it and its parameter names. The `@rpc_method(...)` decorator adds a function to the table, so turning a function into a remote procedure takes one line. All 22 registrations are in `rpc.py`: `get_events` (278), `get_results` (286), `get_worker_stats` (296), `verify_receipt` (303), `get_my_events` (332), `get_ballot` (346), `cast_vote` (363), `get_receipt` (415), `open_voting` (453), `close_voting` (459), `get_turnout` (465), `create_account` (482), `get_account_status` (501), `register_for_event` (517), `get_accounts` (530), `verify_account` (543), `reject_account` (552), `verify_all_pending_accounts` (563), `get_registrations` (570), `approve_registration` (585), `reject_registration` (600), `approve_all_pending` (612).

---

## 4. Remote procedure: `cast_vote`

### 4a. The transaction and the locks

**File:** `app.py`, lines 592–647 (some lines skipped)

```python
    with db.get_pool().connection() as conn:
        with conn.transaction():                       # BEGIN ... COMMIT (or ROLLBACK on error)
            # The event's lock, SHARED: ballots never wait for each other here, but "End voting now"
            # (set_event_mode, EXCLUSIVE) waits until the ballots being saved right now are committed, and
            # ballots that arrive after it read the new status below: fully counted or cleanly rejected.
            conn.execute("SELECT pg_advisory_xact_lock_shared(%s::int, %s::int)", (EVENT_LOCK, event_id))
            row = conn.execute(f"SELECT {EVENT_STATUS_SQL} FROM events e WHERE e.id = %s", (event_id,)).fetchone()
            ...
            if row[0] != "current":
                raise VoteError("Voting is closed for this event. Your ballot was not recorded.")

            # LOCK: only one request per (event, student) gets past this line at a time.
            # Another event's registration row is not locked: voting in Event A never blocks Event B.
            row = conn.execute(
                """SELECT has_voted FROM event_voters
                    WHERE event_id = %s AND student_id = %s FOR UPDATE""", (event_id, student_id)).fetchone()
            if row is None:
                raise VoteError("You are not registered for this voting event.")
            if row[0]:
                raise VoteError("You have already voted.")
            ...
            ballot_id = conn.execute(
                """INSERT INTO ballots (event_id, student_id, receipt_code, worker_pid, thread_name)
                   VALUES (%s, %s, %s, %s, %s) RETURNING id""",
                (event_id, student_id, receipt, worker_pid, thread_name)).fetchone()[0]
            ...
            conn.execute("""UPDATE event_voters SET has_voted = TRUE, voted_at = now()
                             WHERE event_id = %s AND student_id = %s""", (event_id, student_id))
    # Leaving the transaction block = COMMIT = locks released.
    return receipt, worker_pid, thread_name
```

Each ballot is saved in one database **transaction**: either everything is saved or nothing is. The shared **event lock** (`pg_advisory_xact_lock_shared`) lets many ballots be saved at the same time but makes "End voting now" wait for them, so a ballot is either fully counted or cleanly rejected.

`SELECT ... FOR UPDATE` locks this student's registration row. If the same student submits twice at the same instant from two worker processes, the second request waits, then sees `has_voted = TRUE` and is refused. The ballot row also records which worker process and thread saved it.

### 4b. The UNIQUE safety net

**File:** `rpc.py`, lines 386–404 (the `cast_vote` remote procedure)

```python
    try:
        # The SAME function as the web form: one transaction, SELECT ... FOR UPDATE on the
        # (event, student) registration row, then all checks (seats, duplicates, event) and inserts.
        receipt, pid, thread = core.cast_vote(ev["id"], student["id"], picked)
    except core.VoteError as e:
        msg = str(e)
        if "already voted" in msg:
            raise RpcError(ALREADY_VOTED, msg, {"receipt_url": receipt_url})
        if "closed" in msg:
            raise RpcError(VOTING_CLOSED, msg)
        if "not started" in msg:
            raise RpcError(NOT_STARTED, msg)
        if "not registered" in msg:
            raise RpcError(NOT_REGISTERED, core.NOT_REGISTERED, {"event_id": ev["id"]})
        raise RpcError(INVALID_VOTE, msg)
    except psycopg.errors.UniqueViolation:            # safety net: UNIQUE(event_id, student_id)
        raise RpcError(ALREADY_VOTED, "You have already voted.", {"receipt_url": receipt_url})
    except psycopg.errors.CheckViolation:             # safety net: the votes_guard trigger
        raise RpcError(INVALID_VOTE, "Too many candidates for a position.")
```

**File:** `db.py`, lines 140–143 (the database constraints)

```python
    # new rules first ...
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ballots_event_student_key ON ballots (event_id, student_id)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS votes_ballot_candidate_key ON votes (ballot_id, candidate_id)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS positions_event_name_key ON positions (event_id, name)")
```

The RPC procedure calls the same `cast_vote()` function as the web form, then turns each failure into a JSON-RPC error code. As a final safety net, the database itself has a UNIQUE index on `(event_id, student_id)`. Even if two inserts got past the application checks, PostgreSQL would reject the second one (`UniqueViolation`), and the student would get `-32011 "You have already voted."`.

---

## 5. Server / middleware start-up

### 5a. Worker processes and threads from the CPU cores

**File:** `concurrency.py`, lines 41–57

```python
def plan():
    """The numbers every part of BotoKo uses. Same machine + same settings = same answer in every
    process, so the launcher, the workers and the pool size always agree."""
    cores = physical_cores()
    override = os.environ.get("WEB_CONCURRENCY", "").strip()
    if override:
        workers, reason = _int_env("WEB_CONCURRENCY", 1), "set by WEB_CONCURRENCY"
    else:
        cap = _int_env("MAX_WORKERS", DEFAULT_MAX_WORKERS)
        workers = max(1, min(cores, cap))
        reason = f"capped by MAX_WORKERS={cap}" if cores > cap else "1 per core"
    threads = _int_env("THREADS", DEFAULT_THREADS)
    wanted_pool = _int_env("DB_POOL_SIZE", threads)
    db_max = _int_env("DB_MAX_CONNECTIONS", DEFAULT_DB_MAX_CONNECTIONS)
    pool = max(1, min(wanted_pool, db_max // workers))
    return {"cores": cores, "workers": workers, "threads": threads, "pool": pool,
            "reason": reason, "db_max": db_max}
```

This works out how many worker processes to run: one per physical CPU core, up to a limit of 4 by default. Each worker gets 8 threads by default. It also sets each worker's database pool size so that workers × pool never exceeds what PostgreSQL allows. On a 4-core laptop the result is 4 processes × 8 threads = 32 requests handled at the same time.

### 5b. Starting the workers (Waitress) on one shared socket

**File:** `serve.py`, lines 82–89 and 124–130

```python
def start_worker(index, sock, threads):
    parent_end, child_end = mp.Pipe()
    p = mp.Process(target=worker_main, args=(index, child_end, threads), name=f"botoko-worker-{index}", daemon=True)
    p.start()
    child_end.close()
    parent_end.send(sock.share(p.pid))           # duplicate the socket into that process
    parent_end.close()
    return p
...
    plan = concurrency.plan()
    print(concurrency.banner(plan), flush=True)
    print(f"Serving on http://localhost:{port}  (Waitress, {plan['workers']} processes sharing one socket; "
          f"{plan['pool']} database connections per worker)  Stop with Ctrl+C", flush=True)

    workers = {i: start_worker(i, sock, plan["threads"]) for i in range(plan["workers"])}
    print("Worker PIDs: " + ", ".join(str(p.pid) for p in workers.values()), flush=True)
```

The launcher opens one listening socket on port 5000 and starts one separate Python process per worker. It hands each process a copy of the same socket (`sock.share(...)`), so all workers accept connections on the same port and Windows gives each new connection to one of them. If a worker crashes, the launcher starts a new one.

**File:** `serve.py`, lines 41–46 and 50–55 (inside each worker process)

```python
    try:
        sock = socket.fromshare(pipe.recv())     # the launcher's listening socket, duplicated for us
        pipe.close()
        import threading
        from waitress.server import create_server     # imported here: only the workers need it
        import app as botoko
        ...
        server = create_server(botoko.app, sockets=[sock], threads=threads, ident="BotoKo",
                               channel_timeout=60, connection_limit=1000, backlog=1024,
                               trusted_proxy="127.0.0.1", trusted_proxy_count=1,
                               trusted_proxy_headers={"x-forwarded-for", "x-forwarded-proto"},
                               clear_untrusted_proxy_headers=True,
                               _start=False)          # Waitress does NOT watch the listening socket
```

Each worker process receives the shared socket and runs the Flask app inside **Waitress**, a production WSGI server, with its own thread pool (`threads=threads`). This is the middleware layer between the network and the application code.

### 5c. One database connection pool per worker

**File:** `db.py`, lines 22–43

```python
def get_pool():
    """Create the pool lazily, once per worker process (safe with gunicorn forking)."""
    global _pool, _pool_pid
    pid = os.getpid()
    if _pool is None or _pool_pid != pid:
        with _pool_lock:
            if _pool is None or _pool_pid != pid:
                url = os.environ.get("DATABASE_URL")
                if not url:
                    raise RuntimeError("DATABASE_URL is not set. See README.md.")
                _pool = ConnectionPool(
                    url,
                    min_size=1,
                    max_size=concurrency.plan()["pool"],   # 8 per worker (see concurrency.py)
                    timeout=30,              # wait up to 30s for a free connection
                    max_idle=300,
                    check=ConnectionPool.check_connection,  # drop dead connections
                    kwargs={"autocommit": True},
                    open=True,
                )
                _pool_pid = pid
    return _pool
```

Every worker process creates its own pool of open database connections the first time it needs one; the process ID check makes sure processes never share a pool. A request borrows a connection, uses it, and gives it back, so 8 threads per worker can talk to PostgreSQL at the same time without opening a new connection for each request. The lock (`_pool_lock`) stops two threads of the same worker from creating the pool twice.

---

## 6. Remote access through the HTTPS tunnel

### 6a. Waitress trusts the tunnel only from 127.0.0.1

**File:** `serve.py`, lines 47–55

```python
        # HTTPS tunnel on this laptop (cloudflared --url http://127.0.0.1:<port>): its requests come from 127.0.0.1,
        # so ONLY from there Waitress trusts X-Forwarded-For (the real visitor, exactly 1 proxy hop) and
        # X-Forwarded-Proto (https). From anyone else (http://<laptop IP>:<port>) those headers are still removed.
        server = create_server(botoko.app, sockets=[sock], threads=threads, ident="BotoKo",
                               channel_timeout=60, connection_limit=1000, backlog=1024,
                               trusted_proxy="127.0.0.1", trusted_proxy_count=1,
                               trusted_proxy_headers={"x-forwarded-for", "x-forwarded-proto"},
                               clear_untrusted_proxy_headers=True,
                               _start=False)          # Waitress does NOT watch the listening socket
```

People on other networks reach the laptop through a Cloudflare HTTPS tunnel. The tunnel program runs on the laptop itself, so its requests arrive from `127.0.0.1` with two extra headers: the visitor's real IP (`X-Forwarded-For`) and the original protocol (`X-Forwarded-Proto: https`). Waitress keeps those headers only from `127.0.0.1` and only for one proxy hop, and removes them from everyone else, so nobody can fake them.

### 6b. ProxyFix for loopback requests and the Secure cookie rule

**File:** `app.py`, lines 246–271

```python
class LocalTunnelProxyFix:
    LOOPBACK = {"127.0.0.1", "::1"}

    def __init__(self, wsgi_app):
        self.plain = wsgi_app
        self.proxied = ProxyFix(wsgi_app, x_for=1, x_proto=1, x_host=1)

    def __call__(self, environ, start_response):
        if environ.get("REMOTE_ADDR") in self.LOOPBACK and environ.get("HTTP_X_FORWARDED_FOR"):
            return self.proxied(environ, start_response)
        return self.plain(environ, start_response)


if os.environ.get("TRUST_LOCAL_PROXY", "1") == "1":
    app.wsgi_app = LocalTunnelProxyFix(app.wsgi_app)


class BotoKoSessionInterface(SecureCookieSessionInterface):
    """The login cookie is "Secure" (sent over HTTPS only) on HTTPS requests, e.g. through the tunnel, and as
    configured (COOKIE_SECURE) otherwise, so http://localhost:5000 and http://<laptop IP>:5000 keep working."""

    def get_cookie_secure(self, app):
        return bool(app.config["SESSION_COOKIE_SECURE"] or (has_request_context() and request.is_secure))


app.session_interface = BotoKoSessionInterface()
```

Inside the app, Werkzeug's `ProxyFix` (one hop) is applied **only** to requests from the laptop itself that carry `X-Forwarded-For`, i.e. requests from the tunnel. For those requests the app sees the visitor's real IP (used by the rate limits and the admin lockout) and knows the connection was HTTPS.

The session class then marks the login cookie **Secure** on HTTPS requests, so the browser never sends it over plain HTTP. Direct visitors on `http://localhost:5000` or the laptop's Wi-Fi address keep working exactly as before.

---

## 7. Live updates (polling)

**File:** `static/results.js`, line 15 and lines 597–627 (lines 605–611 skipped)

```javascript
  var INTERVAL = 2000;
  ...
  Board.prototype.tick = function () {
    var self = this;
    clearTimeout(this.timer);
    if (this.stopped || document.hidden) return;     // resumes on start() / visibilitychange
    // Remote procedure call: rpc("get_results") (static/rpc.js). Plain /api/results only as a fallback.
    var load = window.rpc ? window.rpc('get_results', this.event ? { event_id: this.event } : {})
      : fetch(this.src, { cache: 'no-store', credentials: 'same-origin' })
          .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); });
    ...
    Promise.all([load, loadWorkers])
      .then(function (r) {
        if (r[1]) self.workers = r[1].processed_by;
        self.failures = 0; self.setOnline(true); self.render(r[0]);
      })
      .catch(function (err) {
        // Voting ended and the results are NOT published (yet): show the "announced soon" page instead of the last
        // live numbers (the server sends that page now). Student / projector pages only, never the admin's.
        if (err && err.code === -32021 && !self.root.hasAttribute('data-manual')) { self.stopped = true; location.reload(); return; }
        if (window.console) console.warn('results:', err); self.failures++; self.setOnline(false);
      })
      .then(function () {
        // 2 s normally; back off up to 10 s while the server can't be reached.
        self.timer = setTimeout(function () { self.tick(); }, Math.min(10000, INTERVAL * (1 + self.failures)));
      });
  };
```

Every 2 seconds the results page calls the remote procedure `get_results` and redraws the bars with the new counts. Each poll may be answered by a different worker process, and that's fine because every worker reads the same shared database. If the server can't be reached, the page waits longer between tries (up to 10 seconds) and shows that it's offline. It pauses while the browser tab is hidden. If voting has ended and the results aren't published, the server answers `-32021` and the page switches to "results will be announced soon".
