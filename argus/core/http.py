"""One shared HTTP client with retries, timeouts and a per-host concurrency cap."""

from __future__ import annotations

import threading
from collections import defaultdict
from contextlib import contextmanager
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import config

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

_local = threading.local()
_local_probe = threading.local()
_host_locks: dict[str, threading.Semaphore] = defaultdict(
    lambda: threading.Semaphore(config.PER_HOST_CONCURRENCY)
)
_host_locks_guard = threading.Lock()


def session() -> requests.Session:
    """Thread-local session: connection pooling without cross-thread sharing."""
    s = getattr(_local, "session", None)
    if s is None:
        s = requests.Session()
        retry = Retry(
            total=3,
            backoff_factor=1.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "HEAD"]),
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_maxsize=config.WORKERS * 2)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        s.headers.update({"User-Agent": config.USER_AGENT, "Accept": "application/json"})
        _local.session = s
    return s


def probe_session() -> requests.Session:
    """Session for speculative probes, where a miss is the expected outcome.

    Deliberately retry-free: the shared session retries 3x with backoff, which
    turns a dead domain into a 9-second stall. Across thousands of company
    domains that dominates the entire sweep.
    """
    s = getattr(_local_probe, "session", None)
    if s is None:
        s = requests.Session()
        adapter = HTTPAdapter(
            max_retries=Retry(total=0, connect=0, read=0, redirect=2),
            pool_maxsize=config.WORKERS * 2,
        )
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        s.headers.update({"User-Agent": BROWSER_UA})
        _local_probe.session = s
    return s


@contextmanager
def host_slot(url: str):
    host = urlsplit(url).netloc
    with _host_locks_guard:
        sem = _host_locks[host]
    sem.acquire()
    try:
        yield
    finally:
        sem.release()


def get_json(url: str, **kwargs):
    from .models import FetchError

    timeout = kwargs.pop("timeout", config.HTTP_TIMEOUT)
    with host_slot(url):
        try:
            r = session().get(url, timeout=timeout, **kwargs)
        except requests.RequestException as exc:
            raise FetchError(f"{type(exc).__name__}: {exc}") from exc
    if r.status_code in (404, 410):
        raise FetchError(
            f"board not found ({r.status_code})", permanent=True, status=r.status_code
        )
    if r.status_code >= 400:
        raise FetchError(f"HTTP {r.status_code}", status=r.status_code)
    try:
        return r.json()
    except ValueError as exc:
        raise FetchError(f"non-JSON response from {url}") from exc


def get_lines(url: str, **kwargs):
    """Stream a large text response line by line (Common Crawl index pages)."""
    from .models import FetchError

    timeout = kwargs.pop("timeout", config.HTTP_TIMEOUT)
    with host_slot(url):
        try:
            r = session().get(url, timeout=timeout, stream=True, **kwargs)
        except requests.RequestException as exc:
            raise FetchError(f"{type(exc).__name__}: {exc}") from exc
        if r.status_code in (404, 410):
            raise FetchError(
                f"no captures ({r.status_code})", permanent=True, status=r.status_code
            )
        if r.status_code >= 400:
            raise FetchError(f"HTTP {r.status_code}", status=r.status_code)
        """
        decode_unicode silently yields bytes when the response declares no
        charset (SEC's .idx files, among others), so pin an encoding first.
        """
        r.encoding = r.encoding or "utf-8"
        """
        Streaming failures surface here, not at request time -- a truncated
        response must fail this one page, not abort the whole source.
        """
        try:
            for line in r.iter_lines(decode_unicode=True):
                if line:
                    yield line
        except requests.RequestException as exc:
            raise FetchError(f"stream interrupted: {type(exc).__name__}: {exc}") from exc


def get_text(url: str, *, probe: bool = False, max_bytes: int | None = None, **kwargs) -> str:
    """Fetch a page as text (HTML sources), with the same error contract.

    probe=True uses the retry-free session -- for speculative fetches where a
    miss is normal. max_bytes stops reading early: careers pages routinely run
    to several MB, and the ATS link is in the markup long before the end.
    """
    from .models import FetchError

    timeout = kwargs.pop("timeout", config.HTTP_TIMEOUT)
    headers = {"Accept": "text/html,application/xhtml+xml,*/*", "User-Agent": BROWSER_UA}
    headers.update(kwargs.pop("headers", None) or {})
    sess = probe_session() if probe else session()
    with host_slot(url):
        try:
            r = sess.get(
                url, timeout=timeout, headers=headers, stream=max_bytes is not None, **kwargs
            )
            if r.status_code in (404, 410):
                raise FetchError(
                    f"not found ({r.status_code})", permanent=True, status=r.status_code
                )
            if r.status_code >= 400:
                raise FetchError(f"HTTP {r.status_code}", status=r.status_code)
            if max_bytes is None:
                return r.text
            buf = bytearray()
            for chunk in r.iter_content(65536):
                buf.extend(chunk)
                if len(buf) >= max_bytes:
                    break
            r.close()
            return buf.decode(r.encoding or "utf-8", errors="ignore")
        except requests.RequestException as exc:
            raise FetchError(f"{type(exc).__name__}: {exc}") from exc


def post_nobody(url: str, *, json: dict | None = None, **kwargs) -> int:
    """POST where success carries no body. Returns the status code.

    Webhooks answer 204 No Content, so post_json's closing r.json() turns
    every successful delivery into a FetchError -- the watermark never
    advances and the same jobs resend every hour. Separate function rather
    than a flag on post_json: the two have different definitions of success.

    429 is honoured here rather than retried blindly. Discord's rate limit
    is per-webhook and it tells you how long to wait; at one message an hour
    we should never see it, but a flood-guard message and a manual run in
    the same minute could.
    """
    import time as _time

    from .models import FetchError

    timeout = kwargs.pop("timeout", config.HTTP_TIMEOUT)
    for attempt in range(3):
        with host_slot(url):
            try:
                r = session().post(url, json=json, timeout=timeout, **kwargs)
            except requests.RequestException as exc:
                raise FetchError(f"{type(exc).__name__}: {exc}") from exc
        if r.status_code == 429 and attempt < 2:
            try:
                wait = float(r.headers.get("Retry-After", "1"))
            except ValueError:
                wait = 1.0
            _time.sleep(min(wait, 30.0))
            continue
        if r.status_code >= 400:
            raise FetchError(f"HTTP {r.status_code}: {r.text[:200]}", status=r.status_code)
        return r.status_code
    raise FetchError("rate limited after retries", status=429)


def post_json(url: str, *, json: dict | None = None, **kwargs):
    """POST returning JSON. Workday is the only ATS so far that needs it."""
    from .models import FetchError

    timeout = kwargs.pop("timeout", config.HTTP_TIMEOUT)
    with host_slot(url):
        try:
            r = session().post(url, json=json, timeout=timeout, **kwargs)
        except requests.RequestException as exc:
            raise FetchError(f"{type(exc).__name__}: {exc}") from exc
    """
    404 = no such site, 422 = no such tenant. Both are permanent.
    """
    if r.status_code in (404, 410, 422):
        raise FetchError(
            f"board not found ({r.status_code})", permanent=True, status=r.status_code
        )
    if r.status_code >= 400:
        raise FetchError(f"HTTP {r.status_code}", status=r.status_code)
    try:
        return r.json()
    except ValueError as exc:
        raise FetchError(f"non-JSON response from {url}") from exc
