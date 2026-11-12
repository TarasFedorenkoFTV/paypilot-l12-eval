"""compare_judges.py: порівняти два звіти calibrate_run (дві моделі судді).
  python tools/compare_judges.py reports/calibration-A.json reports/calibration-B.json
Правило: судді відрізняються, лише якщо інтервали Вілсона узгодженості не перекриваються.
Додатково: частка PASS у більшості вердиктів (зсув у бік PASS) і кейси, де судді розійшлися.
"""
import json, sys
from math import sqrt

def wilson(k, n, z=1.96):
    p = k / n; d = 1 + z*z/n; c = (p + z*z/(2*n))/d
    h = z*sqrt(p*(1-p)/n + z*z/(4*n*n))/d
    return p, c-h, c+h, h

a, b = (json.load(open(p, encoding="utf-8")) for p in sys.argv[1:3])
for r in (a, b):
    k, n = r["agreement"]; p, lo, hi, h = wilson(k, n)
    sc = r["self_consistency"]
    passes = sum(c["majority"] == "PASS" for c in r["cases"])
    print(f'{r["judge_model"]:<28} узгодженість {k}/{n} = {p:.2f} ({lo:.2f}–{hi:.2f}, ±{h:.2f}); '
          f'самоузгодженість {sc[0]}/{sc[1]}; більшість PASS: {passes}/{n}; temp={r["temperature"]}')
(pa, loa, hia, _), (pb, lob, hib, _) = (wilson(*r["agreement"]) for r in (a, b))
overlap = max(loa, lob) <= min(hia, hib)
print("інтервали перекриваються: судді за цим набором НЕ розрізняються" if overlap
      else "інтервали не перекриваються: різниця між суддями статистично помітна")
ca = {c["id"]: c for c in a["cases"]}
diff = [i for i, c in ((c["id"], c) for c in b["cases"]) if i in ca and ca[i]["majority"] != c["majority"]]
print("кейси з різним вердиктом:", diff or "немає")
