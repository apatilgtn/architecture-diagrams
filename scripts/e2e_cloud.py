"""End-to-end check of a running arch-diagram-mcp HTTP server whose storage is a real cloud bucket.

usage: python scripts/e2e_cloud.py <mcp_url> <bearer_token> [--wait-expiry SECONDS]

Optional environment:
  E2E_EXTRA_HEADERS  JSON object of headers added to every MCP request, e.g. the Cloud Run IAM header
                     {"X-Serverless-Authorization": "Bearer <identity token>"}
  E2E_ALT_URL        a URL for the same service under a hostname NOT in ARCH_DIAGRAM_ALLOWED_HOSTS (expects 421)

See docs/CLOUD_TESTING.md.
"""
import asyncio
import json
import os
import sys
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

URL, TOKEN = sys.argv[1], sys.argv[2]
WAIT = int(sys.argv[sys.argv.index("--wait-expiry") + 1]) if "--wait-expiry" in sys.argv else 0
EXTRA = json.loads(os.environ.get("E2E_EXTRA_HEADERS", "{}"))
ALT_URL = os.environ.get("E2E_ALT_URL")
MAGIC = {"png": b"\x89PNG", "svg": b"<?xml", "dot": b"digraph"}
failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


def text_of(result):
    return next(c.text for c in result.content if getattr(c, "type", "") == "text")


def tamper(url):
    parts = urlsplit(url)
    q = parse_qsl(parts.query, keep_blank_values=True)
    q = [(k, (v[:-2] + ("00" if not v.endswith("00") else "11")) if k.lower() in {"x-goog-signature", "sig"} else v) for k, v in q]
    return urlunsplit(parts._replace(query=urlencode(q)))


async def main():
    accept = {"Accept": "application/json, text/event-stream"}
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    if EXTRA:  # behind Cloud Run IAM: the front door must refuse callers without a Google identity
        r = httpx.post(URL, json=ping, headers=accept, timeout=60)
        check(r.status_code in (401, 403), f"Cloud Run IAM rejects anonymous caller ({r.status_code})")
    # no app token -> 401 from the server itself
    r = httpx.post(URL, json=ping, headers={**accept, **EXTRA}, timeout=60)
    check(r.status_code == 401, f"no app bearer token rejected by server ({r.status_code})")
    r = httpx.post(URL, json=ping, headers={**accept, **EXTRA, "Authorization": "Bearer wrong-token-0123456789"}, timeout=60)
    check(r.status_code == 401, f"wrong app bearer token rejected ({r.status_code})")
    if ALT_URL:
        r = httpx.post(ALT_URL, json=ping, headers={**accept, **EXTRA, "Authorization": f"Bearer {TOKEN}"}, timeout=60)
        check(r.status_code == 421, f"Host not in allow-list rejected ({r.status_code})")

    headers = {"Authorization": f"Bearer {TOKEN}", **EXTRA}
    async with httpx2.AsyncClient(headers=headers, timeout=120) as hc, streamable_http_client(URL, http_client=hc) as (rd, wr, *_):
        async with ClientSession(rd, wr) as s:
            await s.initialize()
            info = json.loads(text_of(await s.call_tool("server_info", {})))
            print("server_info:", json.dumps({k: info[k] for k in ("version", "graphviz", "storage", "auth_mode", "caller")}))
            check(info["storage"] in {"gcs", "s3", "azure"}, f"cloud storage backend active ({info['storage']})")

            tpl = json.loads(text_of(await s.call_tool("get_template", {"template_id": "gcp_serverless_data"})))
            spec = tpl.get("spec", tpl)
            spec["output_formats"] = ["png", "svg"]
            t0 = time.monotonic()
            res = await s.call_tool("generate_architecture_diagram", {"spec": spec})
            check(not res.is_error, f"render succeeded in {time.monotonic() - t0:.1f}s")
            if res.is_error:
                print(text_of(res))
                return
            out = json.loads(text_of(res))
            print("result:", json.dumps({k: out.get(k) for k in ("storage", "urls_expire_at", "nodes", "edges", "warnings")}))
            check("files" not in out, "no server file paths leaked over HTTP")

    urls = out["urls"]
    check(set(urls) >= {"png", "svg"}, f"signed URLs returned for {sorted(urls)}")
    for fmt, u in urls.items():
        host = urlsplit(u).netloc
        r = httpx.get(u, timeout=60)
        ok = r.status_code == 200 and r.content.lstrip()[:8].startswith(MAGIC.get(fmt, b""))
        check(ok, f"{fmt}: download {r.status_code}, {len(r.content):,} bytes, {r.headers.get('content-type')} from {host}")
        r = httpx.get(tamper(u), timeout=60)
        check(r.status_code in (400, 401, 403), f"{fmt}: tampered signature rejected ({r.status_code})")
        bare = urlunsplit(urlsplit(u)._replace(query=""))
        r = httpx.get(bare, timeout=60)
        check(r.status_code in (401, 403), f"{fmt}: unsigned object URL is private ({r.status_code})")

    if WAIT:
        print(f"waiting {WAIT}s for links to expire ...")
        time.sleep(WAIT)
        r = httpx.get(urls["png"], timeout=60)
        check(r.status_code in (400, 401, 403), f"expired link rejected ({r.status_code})")

    print(json.dumps({"urls": urls}))
    print("\nRESULT:", "ALL PASSED" if not failures else f"{len(failures)} FAILED")
    sys.exit(1 if failures else 0)


asyncio.run(main())
