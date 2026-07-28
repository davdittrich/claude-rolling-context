"""Does ROLLING_CONTEXT_UPSTREAM in the environment beat the same key in settings.json?

The spec's tier order (section 7) and the upstream-pinned-by-env guard (section 6) both assume it does.
Nothing measured it. This probe does.

Method: run the proxy with ROLLING_CONTEXT_UPSTREAM set in the environment to listener A, and the same
key set in a fake HOME's settings.json to listener B. Send one request through the proxy. Whichever
listener receives it is the winner.

No API contact: both listeners answer locally.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import http.server
import urllib.request

A, B, PROXY = 5941, 5942, 5943
hits = {A: 0, B: 0}


def make(port):
    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            hits[port] += 1
            body = b'{"type":"message","content":[]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    return H


def main():
    home = tempfile.mkdtemp(prefix="precedence-")
    os.makedirs(os.path.join(home, ".claude"))
    with open(os.path.join(home, ".claude", "settings.json"), "w") as f:
        json.dump({"env": {"ROLLING_CONTEXT_UPSTREAM": f"http://127.0.0.1:{B}"}}, f)

    servers = []
    for port in (A, B):
        s = http.server.HTTPServer(("127.0.0.1", port), make(port))
        threading.Thread(target=s.serve_forever, daemon=True).start()
        servers.append(s)

    env = dict(os.environ)
    env["HOME"] = home
    env["ROLLING_CONTEXT_PORT"] = str(PROXY)
    env["ROLLING_CONTEXT_UPSTREAM"] = f"http://127.0.0.1:{A}"
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    proc = subprocess.Popen([sys.executable, "server.py"],
                            cwd=os.path.join(repo, "proxy"), env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(2)
        req = urllib.request.Request(
            f"http://127.0.0.1:{PROXY}/v1/messages",
            data=json.dumps({"model": "claude-opus-5", "messages": [],
                             "max_tokens": 1}).encode(),
            headers={"Content-Type": "application/json", "x-api-key": "probe"})
        try:
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as e:
            print("request error (may still be conclusive):", e)
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        for s in servers:
            s.shutdown()
            s.server_close()
        shutil.rmtree(home, ignore_errors=True)

    print(json.dumps({"env_listener_A": hits[A], "settings_listener_B": hits[B]}))
    if hits[A] and not hits[B]:
        print("VERDICT: environment beats settings -- tier order as assumed")
    elif hits[B] and not hits[A]:
        print("VERDICT: settings beats environment -- SPEC IS WRONG, tier order must change")
    else:
        print("VERDICT: INCONCLUSIVE")


if __name__ == "__main__":
    main()
