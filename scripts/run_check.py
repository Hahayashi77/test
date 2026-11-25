import io
import json
import sys
import time
import urllib.error
import urllib.request
import zipfile

tok = open(r"C:\Users\260010196\AppData\Local\Temp\gh_tok.txt", encoding="utf-8").read().strip()
H = {"Authorization": f"Bearer {tok}", "User-Agent": "db", "Accept": "application/vnd.github+json"}


def api(p, method="GET", body=None):
    req = urllib.request.Request("https://api.github.com" + p, headers=H, method=method,
                                 data=json.dumps(body).encode() if body else None)
    r = urllib.request.urlopen(req, timeout=40)
    raw = r.read()
    return json.loads(raw) if raw else {}


if "--trigger" in sys.argv:
    api("/repos/Hahayashi77/test/actions/workflows/daily.yml/dispatches",
        "POST", {"ref": "main", "inputs": {"dry_run": False}})
    print("已触发")

print("等待运行完成…")
latest = None
for _ in range(60):
    runs = api("/repos/Hahayashi77/test/actions/runs?per_page=3")["workflow_runs"]
    latest = runs[0]
    if latest["status"] == "completed":
        break
    print(f"  {latest['status']} #{latest['run_number']}")
    time.sleep(15)

print(f"\n#{latest['run_number']}: {latest['status']}/{latest['conclusion']}")
print(latest["html_url"])

jobs = api(f"/repos/Hahayashi77/test/actions/runs/{latest['id']}/jobs")["jobs"]
for j in jobs:
    print(f"\nJOB {j['name']}: {j['conclusion']}")
    for s in j["steps"]:
        mark = {"success": "✓", "failure": "✗", "skipped": "–"}.get(s["conclusion"], "?")
        print(f"   {mark} {s['name']}")

job = [j for j in jobs if j["name"] == "build"][0]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


url = f"https://api.github.com/repos/Hahayashi77/test/actions/jobs/{job['id']}/logs"
req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}", "User-Agent": "db"})
try:
    urllib.request.build_opener(NoRedirect).open(req, timeout=60)
    raw = b""
except urllib.error.HTTPError as he:
    with urllib.request.urlopen(urllib.request.Request(he.headers["Location"],
                                                       headers={"User-Agent": "db"}), timeout=90) as r2:
        raw = r2.read()

content = (zipfile.ZipFile(io.BytesIO(raw)).read(
    zipfile.ZipFile(io.BytesIO(raw)).namelist()[0]) if raw[:2] == b"PK"
    else raw).decode("utf-8", "replace")

print("\n===== 生成日志 =====")
for l in content.splitlines():
    if not l.strip():
        continue
    if l[:4].isdigit() and "Z " in l[:40]:
        l = l.split("Z ", 1)[1]
    if any(k in l for k in ("批次", "完成：", "模型", "配置：", "摘要补全", "⚠", "✗")):
        print("  ", l[:170])