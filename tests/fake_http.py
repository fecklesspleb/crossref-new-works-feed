"""A stand-in for new_works.urllib_http that serves saved responses by URL and records every request."""
import json
from pathlib import Path

FIX = Path(__file__).parent / "fixtures"


def fixture(name: str) -> bytes:
    return (FIX / name).read_bytes()


def fixture_json(name: str) -> dict:
    return json.loads(fixture(name))


class FakeHTTP:
    """routes: url -> (status, headers, body) or a list of those, served in order (the last one repeats).
    robots.txt answers 404 (no rules) unless routed. Any other URL raises, so tests never touch the network."""

    def __init__(self, routes=None):
        self.routes = {k: (v if isinstance(v, list) else [v]) for k, v in (routes or {}).items()}
        self.calls = []

    def __call__(self, url, headers, timeout=60):
        self.calls.append((url, dict(headers)))
        if url not in self.routes:
            if url.endswith("/robots.txt"):
                return 404, {}, b"not found"
            raise AssertionError(f"unexpected request: {url}")
        queue = self.routes[url]
        status, hdrs, body = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        return status, hdrs, body

    def data_calls(self):
        return [u for u, _ in self.calls if not u.endswith("/robots.txt")]


class FakeClock:
    """time.monotonic and time.sleep replacement: sleeping moves the clock forward."""

    def __init__(self):
        self.t = 1000.0
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s
