"""Benchmark the running board: python3 bench.py NAME [warmup_s=900] [duration_s=600]
Samples system CPU, the board process's CPU (% of ONE core, so up to 100 x cores), RSS and network every 5 s,
then saves those plus a /api/stats snapshot and per-source status counts to ~/bench/NAME.json."""
import json, os, statistics as st, subprocess, sys, time, urllib.request, collections, csv, io

name, warmup, duration = sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 900, int(sys.argv[3]) if len(sys.argv) > 3 else 600
time.sleep(warmup)
pid = int(subprocess.check_output(["systemctl", "show", "-p", "MainPID", "--value", "internboard"]).strip())
hz = os.sysconf("SC_CLK_TCK")


def cpu():
    v = list(map(int, open("/proc/stat").readline().split()[1:]))
    return sum(v), v[3] + v[4]


def proc():
    f = open(f"/proc/{pid}/stat").read().split(")")[1].split()
    return int(f[11]) + int(f[12])


def net():
    for line in open("/proc/net/dev"):
        if "enp" in line:
            p = line.split(":")[1].split()
            return int(p[0]), int(p[8])


rows, t0, (a0, i0), p0, (r0, x0) = [], time.time(), cpu(), proc(), net()
for _ in range(duration // 5):
    time.sleep(5)
    t1, (a1, i1), p1, (r1, x1) = time.time(), cpu(), proc(), net()
    rss = int(open(f"/proc/{pid}/status").read().split("VmRSS:")[1].split()[0]) // 1024
    rows.append((100 * (1 - (i1 - i0) / (a1 - a0)), 100 * (p1 - p0) / hz / (t1 - t0), rss, (r1 - r0) / (t1 - t0), (x1 - x0) / (t1 - t0)))
    t0, a0, i0, p0, r0, x0 = t1, a1, i1, p1, r1, x1
summary = {}
for i, key in enumerate(["system_cpu_pct", "board_cpu_pct_of_one_core", "rss_mb", "rx_bytes_s", "tx_bytes_s"]):
    xs = sorted(r[i] for r in rows)
    summary[key] = {"avg": round(st.mean(xs), 1), "p95": round(xs[int(len(xs) * .95)], 1), "max": round(xs[-1], 1)}
stats = json.load(urllib.request.urlopen("http://127.0.0.1:8000/api/stats", timeout=60))
src = list(csv.DictReader(io.StringIO(urllib.request.urlopen("http://127.0.0.1:8000/api/sources", timeout=60).read().decode())))
status = collections.defaultdict(collections.Counter)
for r in src:
    status[r["kind"]][r["status"]] += 1
os.makedirs(os.path.expanduser("~/bench"), exist_ok=True)
json.dump({"name": name, "cpus": os.cpu_count(), "sampled": summary, "stats": stats, "source_status": status},
          open(os.path.expanduser(f"~/bench/{name}.json"), "w"), indent=1)
print("saved", name)
