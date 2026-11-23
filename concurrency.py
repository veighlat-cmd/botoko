"""How many worker processes and threads BotoKo runs, decided from the CPU of the machine it runs on.

Used by gunicorn.conf.py (Render / Linux), serve.py (Windows, started by run-local.ps1),
app.py (when started with "py app.py") and db.py (connection pool size per worker).

    workers = number of PHYSICAL CPU cores (1 worker process per core)
              - fallback: os.cpu_count() if the physical count is unknown, minimum 1
              - override: WEB_CONCURRENCY=6 (used as given)
              - safety cap: MAX_WORKERS (default 4). On Render, os.cpu_count() / psutil can report
                the HOST machine's cores (e.g. 16) while the server only has 512 MB of RAM, and every
                worker process is a full copy of the app (~60 MB), so too many workers would crash it.
    threads = 8 per worker (THREADS)
    pool    = database connections per worker: DB_POOL_SIZE (default = threads), but never more than
              DB_MAX_CONNECTIONS (default 80) / workers, so workers x pool stays below the database's
              connection limit (PostgreSQL allows 100 by default, Render's free database about the same).
"""
import os

DEFAULT_THREADS = 8
DEFAULT_MAX_WORKERS = 4
DEFAULT_DB_MAX_CONNECTIONS = 80      # leaves room for psql, seed.py, the admin's own tools, ...


def _int_env(name, default, minimum=1):
    try:
        return max(minimum, int(os.environ.get(name, "").strip() or default))
    except ValueError:
        return default


def physical_cores():
    """Physical CPU cores (4 on a laptop with 4 cores / 8 logical). Fallback: os.cpu_count()."""
    try:
        import psutil
        n = psutil.cpu_count(logical=False)
    except Exception:            # psutil missing or can't read the CPU info
        n = None
    return n or os.cpu_count() or 1


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


def banner(p=None):
    """The one startup line, e.g.
    BotoKo: 4 CPU cores detected -> 4 worker processes x 8 threads = 32 requests at the same time"""
    p = p or plan()
    w = p["workers"]
    line = (f"BotoKo: {p['cores']} CPU cores detected -> {w} worker process{'es' if w != 1 else ''} "
            f"x {p['threads']} threads = {w * p['threads']} requests at the same time")
    if p["reason"] != "1 per core":
        line += f" ({p['reason']})"
    return line
