import io
import json
import urllib.error
import urllib.request
import zipfile

tok = open(r"C:\Users\260010196\AppData\Local\Temp\gh_tok.txt", encoding="utf-8").read().strip()
H = {"Authorization": f"Bearer {tok}", "User-Agent": "db", "Accept": "application/vnd.github+json"}


def api(p):
    return json.loads(urllib.request.urlopen(
        urllib.request.Request("https://api.github.com" + p, headers=H), timeout=40).read())


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def job_log(job_id):
    url = f"https://api.github.com/repos/Hahayashi77/test/actions/jobs/{job_id}/logs"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}", "User-Agent": "db"})
    try:
        urllib.request.build_opener(NoRedirect).open(req, timeout=60)
        raw = b""
    except urllib.error.HTTPError as he:
        with urllib.request.urlopen(urllib.request.Request(he.headers["Location"],
                                                           headers={"User-Agent": "db"}), timeout=90) as r2:
            raw = r2.read()
    if raw[:2] == b"PK":
        zf = zipfile.ZipFile(io.BytesIO(raw))
        return zf.read(zf.namelist()[0]).decode("utf-8", "replace")
    return raw.decode("utf-8", "replace")


# 找 #7
runs = api("/repos/Hahayashi77/test/actions/runs?per_page=10")["workflow_runs"]
r7 = [r for r in runs if r["run_number"] == 7][0]
jobs = api(f"/repos/Hahayashi77/test/actions/runs/{r7['id']}/jobs")["jobs"]
build = [j for j in jobs if j["name"] == "build"][0]

log = job_log(build["id"])
lines = []
for l in log.splitlines():
    if not l.strip():
        continue
    if l[:4].isdigit() and "Z " in l[:40]:
        l = l.split("Z ", 1)[1]
    lines.append(l)

print("=== #7 从冲突到结束 ===")
start = next((i for i, l in enumerate(lines) if "CONFLICT" in l), 0)
for l in lines[max(0, start - 3):start + 40]:
    print("  ", l[:175])

print("\n=== 构建产物里是否含冲突标记？看这步之后还有没有校验 ===")
for l in lines:
    if "verify" in l or "校验" in l or "upload-pages" in l:
        print("  ", l[:175])