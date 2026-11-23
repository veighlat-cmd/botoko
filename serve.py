"""Run BotoKo with several worker PROCESSES on Windows (started by run-local.ps1).

Why this file: gunicorn (used on Render) needs Unix fork() and doesn't run on Windows, and Flask's
development server ("py app.py") is a single process, i.e. it uses only 1 CPU core.

How it works (the same model as gunicorn):
  1. This launcher opens ONE listening socket, e.g. port 5000.
  2. It starts N worker processes, N = concurrency.plan() = 1 per physical CPU core (4 on a laptop
     with 4 cores). Each worker is a separate Python process with its own PID, memory and database pool.
  3. The listening socket is handed to every worker with socket.share() / socket.fromshare()
     (Windows' WSADuplicateSocket). All workers accept connections from the same port, and Windows
     gives each new connection to one of them.
  4. Every worker serves requests with Waitress, a production WSGI server for Windows, using
     8 threads, so 4 workers x 8 threads = 32 requests at the same time.
  5. If a worker process dies, the launcher starts a new one. Ctrl+C stops everything.

Usage:  py serve.py              (port from $env:PORT, default 5000)
"""
import multiprocessing as mp
import os
import secrets
import signal
import socket
import sys
import time

import concurrency

HOST = "0.0.0.0"


def worker_main(index, pipe, threads):
    """One worker process: receive the shared listening socket, then serve the app with Waitress.

    New connections are taken with a plain BLOCKING accept() in one thread per worker, not with
    Waitress' own select() on the listening socket. With one socket shared by several processes,
    Windows' select() could occasionally wake no worker for a new connection, so about 1 request in
    1,500 hung during bursts of 100 at once (e.g. 50 students pressing Submit together). A blocking
    accept() gives every connection to exactly one waiting worker. The connection is then handed to
    Waitress' event loop through its trigger, and Waitress serves it exactly as before."""
    try:
        sock = socket.fromshare(pipe.recv())     # the launcher's listening socket, duplicated for us
        pipe.close()
        import threading
        from waitress.server import create_server     # imported here: only the workers need it
        import app as botoko
        # HTTPS tunnel on this laptop (cloudflared --url http://127.0.0.1:<port>): its requests come from 127.0.0.1,
        # so ONLY from there Waitress trusts X-Forwarded-For (the real visitor, exactly 1 proxy hop) and
        # X-Forwarded-Proto (https). From anyone else (http://<laptop IP>:<port>) those headers are still removed.
        server = create_server(botoko.app, sockets=[sock], threads=threads, ident="BotoKo",
                               channel_timeout=60, connection_limit=1000, backlog=1024,
                               trusted_proxy="127.0.0.1", trusted_proxy_count=1,
                               trusted_proxy_headers={"x-forwarded-for", "x-forwarded-proto"},
                               clear_untrusted_proxy_headers=True,
                               _start=False)          # Waitress does NOT watch the listening socket
        sock.setblocking(True)                         # ... this worker's accept thread does

        def accept_loop():
            while True:
                try:
                    conn, addr = sock.accept()
                except OSError:
                    time.sleep(0.05)                   # e.g. the client gave up before we accepted
                    continue
                while len(server._map) >= server.adj.connection_limit:
                    time.sleep(0.01)                   # too many open connections: wait a moment

                def add(conn=conn, addr=addr):         # runs inside Waitress' event loop thread
                    try:
                        server.set_socket_options(conn)
                        server.channel_class(server, conn, server.fix_addr(addr), server.adj, map=server._map)
                    except OSError:
                        conn.close()
                server.trigger.pull_trigger(add)

        threading.Thread(target=accept_loop, name="accept", daemon=True).start()
        server.run()
    except KeyboardInterrupt:
        pass


def start_worker(index, sock, threads):
    parent_end, child_end = mp.Pipe()
    p = mp.Process(target=worker_main, args=(index, child_end, threads), name=f"botoko-worker-{index}", daemon=True)
    p.start()
    child_end.close()
    parent_end.send(sock.share(p.pid))           # duplicate the socket into that process
    parent_end.close()
    return p


def main():
    if not hasattr(socket.socket, "share"):
        raise SystemExit("serve.py is for Windows. On Linux/macOS use: gunicorn -c gunicorn.conf.py app:app")
    try:
        import waitress  # noqa: F401
    except ImportError:
        raise SystemExit("Waitress is not installed. Run:  py -m pip install waitress")

    # All workers must sign logins with the SAME key, otherwise a student logged in by worker A
    # would look logged out to worker B; and it must stay the same across restarts, so open pages
    # and logins keep working. SECRET_KEY, else the .secret_key file (made once; see secret_key.py).
    # Set BEFORE importing the app, and the worker processes inherit it.
    from secret_key import load_secret_key
    os.environ["SECRET_KEY"] = load_secret_key()[0]
    os.environ.setdefault("COOKIE_SECURE", "0")      # plain http on this laptop
    os.environ["BOTOKO_SERVER"] = "waitress"         # shown at /admin/api/system (inherited by the workers)

    import app as botoko
    botoko.check_database_or_exit()

    port = int(os.environ.get("PORT", "5000"))
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # Exclusive: fail clearly if another BotoKo (or anything else) already uses the port,
    # instead of two servers silently sharing it.
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        sock.bind((HOST, port))
    except OSError:
        raise SystemExit(f"Port {port} is already in use. Is BotoKo already running in another window? "
                         f"Stop it first (Ctrl+C there), or use another port: $env:PORT = 5001")
    sock.listen(1024)

    plan = concurrency.plan()
    print(concurrency.banner(plan), flush=True)
    print(f"Serving on http://localhost:{port}  (Waitress, {plan['workers']} processes sharing one socket; "
          f"{plan['pool']} database connections per worker)  Stop with Ctrl+C", flush=True)

    workers = {i: start_worker(i, sock, plan["threads"]) for i in range(plan["workers"])}
    print("Worker PIDs: " + ", ".join(str(p.pid) for p in workers.values()), flush=True)

    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, stop)

    try:
        while not stopping:
            time.sleep(0.5)
            for i, p in list(workers.items()):
                if not p.is_alive() and not stopping:            # a worker crashed: replace it
                    print(f"Worker {i} (PID {p.pid}) stopped (exit code {p.exitcode}); starting a new one.", flush=True)
                    workers[i] = start_worker(i, sock, plan["threads"])
    except KeyboardInterrupt:
        pass
    finally:
        print("Stopping BotoKo...", flush=True)
        for p in workers.values():
            if p.is_alive():
                p.terminate()
        for p in workers.values():
            p.join(timeout=5)
        sock.close()


if __name__ == "__main__":
    mp.freeze_support()
    sys.exit(main())
