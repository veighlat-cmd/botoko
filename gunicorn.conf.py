# Production server settings (used on Render: gunicorn -c gunicorn.conf.py app:app)
#
# PARALLEL: 1 worker PROCESS per physical CPU core (auto-detected, see concurrency.py),
# each with 8 THREADS. Example: 4 cores -> 4 workers x 8 threads = 32 requests at the same time.
#   - WEB_CONCURRENCY=3 overrides the number of workers.
#   - MAX_WORKERS (default 4) caps the auto-detected number: on Render the container can report
#     the host's cores (e.g. 16) but only has 512 MB RAM, and each worker is a full copy of the app.
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # so "import concurrency" works
from concurrency import banner, plan  # noqa: E402

_plan = plan()
os.environ.setdefault("BOTOKO_SERVER", "gunicorn")   # shown at /admin/api/system (inherited by the workers)

bind = f"0.0.0.0:{os.environ.get('PORT', '10000')}"
workers = _plan["workers"]
worker_class = "gthread"
threads = _plan["threads"]
timeout = 60
graceful_timeout = 30
keepalive = 5
accesslog = "-"
errorlog = "-"
loglevel = "info"


def when_ready(server):
    """Printed once, when gunicorn is ready to take requests."""
    print(banner(_plan), flush=True)
    try:
        from app import warn_if_weak_admin_password     # "Change the admin password before the real election"
        warn_if_weak_admin_password()
    except Exception as e:                              # never stop the server because of this warning
        print(f"(admin password check skipped: {e})", flush=True)
