"""BotoKo RPC client: a separate program that calls BotoKo's remote procedures over the network
(client-server model + RPC, JSON-RPC 2.0). Uses only the Python standard library.

Examples
  py rpc_client.py --url http://localhost:5000 list                       # all remote procedures
  py rpc_client.py --url http://localhost:5000 get_results
  py rpc_client.py --url http://localhost:5000 verify_receipt "{\"verification_code\": \"9EE95D53\"}"

  # as a student (normal Voter ID + password login, then RPC with the same session):
  py rpc_client.py --url http://localhost:5000 get_events                       # ids of the voting events
  py rpc_client.py --url http://localhost:5000 --event 3 --voter V-ABC123 --password secret get_ballot
  py rpc_client.py --url http://localhost:5000 --event 3 --voter V-ABC123 --password secret cast_vote
        (no params: picks the FIRST candidate of every position, after showing the ballot)
  py rpc_client.py --url http://localhost:5000 --event 3 --voter V-ABC123 --password secret cast_vote "{\"choices\": {\"1\": 3, \"2\": null}}"
  py rpc_client.py --url http://localhost:5000 --event 3 --voter V-ABC123 --password secret get_receipt "{\"receipt_code\": \"9EE95D53\"}"

  # create an account (anyone), then find the Voter ID again + the account status:
  py rpc_client.py --url http://localhost:5000 create_account "{\"school_student_no\": \"23-1-00479\", \"full_name\": \"Juan Dela Cruz\", \"course\": \"BSIT\", \"department\": \"CCS\", \"year\": 3, \"section\": \"A\", \"password\": \"my-own-pass-1\"}"
  py rpc_client.py --url http://localhost:5000 get_account_status "{\"school_student_no\": \"23-1-00479\", \"password\": \"my-own-pass-1\"}"
  py rpc_client.py --url http://localhost:5000 --voter V-ABC123 --password my-own-pass-1 register_for_event "{\"event_id\": 3}"

  # as the COMELEC admin:
  py rpc_client.py --url http://localhost:5000 --admin-password admin123 get_turnout
  py rpc_client.py --url http://localhost:5000 --admin-password admin123 close_voting
  py rpc_client.py --url http://localhost:5000 --admin-password admin123 get_registrations "{\"event_id\": 2, \"status\": \"pending\"}"
  py rpc_client.py --url http://localhost:5000 --admin-password admin123 approve_all_pending "{\"event_id\": 2}"
  py rpc_client.py --url http://localhost:5000 --admin-password admin123 verify_all_pending_accounts

(PowerShell: put the JSON in single quotes instead, e.g. '{"verification_code": "9EE95D53"}'.)
"""
import argparse
import http.cookiejar
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

CSRF_FIELD = re.compile(r'name="csrf" value="([^"]+)"')


class RpcError(Exception):
    def __init__(self, error, meta=None):
        super().__init__(f"{error.get('code')}: {error.get('message')}")
        self.code, self.message, self.data, self.meta = error.get("code"), error.get("message"), error.get("data"), meta


class BotoKoClient:
    """Client stub: client.call("get_results") sends a JSON-RPC 2.0 request and returns the result."""

    def __init__(self, url, verbose=True):
        self.url = url.rstrip("/")
        self.verbose = verbose
        self.http = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.csrf = None
        self.next_id = 1

    # ---- plain HTTP helpers
    def _get(self, path):
        with self.http.open(self.url + path, timeout=30) as r:
            return r.geturl(), r.read().decode()

    def _post_form(self, path, fields):
        with self.http.open(self.url + path, data=urllib.parse.urlencode(fields).encode(), timeout=30) as r:
            return r.geturl(), r.read().decode()

    # ---- login with the normal BotoKo login pages (the session cookie is the RPC authentication)
    def login_student(self, voter_id, password, event_id=None):
        """Log in on the first page (Voter ID + password), like a student does on the website.
        event_id (optional) = the page to open after logging in, e.g. that event's ballot."""
        _, page = self._get("/")
        fields = {"csrf": CSRF_FIELD.search(page).group(1), "student_no": voter_id, "password": password}
        if event_id:
            fields["next"] = f"/e/{event_id}"
        final_url, page = self._post_form("/", fields)
        path = urllib.parse.urlparse(final_url).path
        if path in ("/", "/login"):
            raise SystemExit(f"Login failed for {voter_id}: wrong Voter ID or password.")
        if path == "/account" and event_id:
            print(f"Note: {voter_id}'s account is not verified yet (pending COMELEC), so it can't vote.")
        self.csrf = None                        # a new session gets a new CSRF token

    def login_admin(self, password):
        """The admin logs in on the normal login page with Voter ID "admin" and the admin password."""
        _, page = self._get("/")
        final_url, _ = self._post_form("/", {"csrf": CSRF_FIELD.search(page).group(1),
                                                  "student_no": "admin", "password": password})
        if urllib.parse.urlparse(final_url).path != "/admin":
            raise SystemExit("Admin login failed: wrong admin password (or too many wrong tries: wait 15 minutes).")
        self.csrf = None

    def describe(self):
        """GET /rpc: the list of remote procedures and a CSRF token for this session."""
        _, body = self._get("/rpc")
        info = json.loads(body)
        self.csrf = info["csrf_token"]
        return info

    # ---- the RPC call itself
    def call(self, method, params=None):
        if self.csrf is None:
            self.describe()
        request = {"jsonrpc": "2.0", "method": method, "params": params or {}, "id": self.next_id}
        self.next_id += 1
        if self.verbose:
            print("--> request  (POST /rpc)\n" + json.dumps(request, indent=2))
        req = urllib.request.Request(self.url + "/rpc", data=json.dumps(request).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "X-CSRF-Token": self.csrf})
        try:
            with self.http.open(req, timeout=60) as r:
                reply = json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            reply = json.loads(e.read().decode() or "{}")
        if self.verbose:
            print("<-- response\n" + json.dumps(reply, indent=2, ensure_ascii=False))
        meta = reply.get("meta") or {}
        if self.verbose and meta:
            print(f"(procedure ran on the server in worker process PID {meta.get('worker_pid')}, "
                  f"thread {meta.get('thread_name')}, {meta.get('duration_ms')} ms)")
        if "error" in reply:
            raise RpcError(reply["error"], meta)
        return reply["result"]


def main():
    ap = argparse.ArgumentParser(description="Call BotoKo's remote procedures (JSON-RPC 2.0) from the command line.",
                                 formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__.split("Examples", 1)[1])
    ap.add_argument("--url", default="http://localhost:5000", help="BotoKo server, default http://localhost:5000")
    ap.add_argument("--event", type=int, help="the voting event (id from get_events): needed for --voter")
    ap.add_argument("--voter", help="log in as this student (Voter ID) first, to --event")
    ap.add_argument("--password", help="the student's password")
    ap.add_argument("--admin-password", help="log in as the COMELEC admin first")
    ap.add_argument("method", help='remote procedure name, or "list"')
    ap.add_argument("params", nargs="?", help="JSON object with the params, e.g. '{\"receipt_code\": \"9EE95D53\"}'")
    args = ap.parse_args()

    client = BotoKoClient(args.url)
    if args.voter:
        if not args.password:
            ap.error("--voter needs --password")
        client.login_student(args.voter, args.password, args.event)
        print(f"Logged in as student {args.voter}" + (f" (event {args.event})" if args.event else ""))
    elif args.admin_password:
        client.login_admin(args.admin_password)
        print("Logged in as COMELEC admin")

    if args.method == "list":
        info = client.describe()
        print(f"{info['service']} at {args.url}{info['endpoint']}  (you are: {info['caller']})\n")
        for m in info["methods"]:
            print(f"  {m['name']:<15} params: {', '.join(m['params']) or '-':<20} who: {m['who']:<8} {m['description']}")
        return 0

    try:
        params = json.loads(args.params) if args.params else None
    except ValueError as e:
        ap.error(f"params is not valid JSON: {e}")

    try:
        if args.event and args.method in ("get_ballot", "cast_vote", "get_results", "open_voting", "close_voting",
                                          "get_turnout", "get_registrations", "approve_all_pending",
                                          "register_for_event") and "event_id" not in (params or {}):
            params = dict(params or {}, event_id=args.event)
        if args.method == "cast_vote" and "choices" not in (params or {}):
            # Demo: show the ballot, then vote for the first candidate(s) of every position
            # (as many as the position allows, e.g. 3 for "Councilor - vote up to 3").
            ballot = client.call("get_ballot", {"event_id": params["event_id"]} if params else None)
            choices = {str(p["id"]): [c["id"] for c in p["candidates"][:p["seats"]]] for p in ballot["positions"]}
            params = dict(params or {}, choices=choices)
            print("\nVoting for: " + "; ".join(f"{p['name']} = " + ", ".join(c["name"] for c in p["candidates"][:p["seats"]])
                                              for p in ballot["positions"]) + "\n")
        result = client.call(args.method, params)
    except RpcError as e:
        print(f"\nRPC error {e.code}: {e.message}")
        return 1
    if args.method == "cast_vote":
        print(f"\nVote recorded. Receipt code: {result['receipt_code']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
