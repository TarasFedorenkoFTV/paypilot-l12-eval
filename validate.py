import json
import os
from pathlib import Path

from loader import load, set_hash, summarise

path = Path(__file__).resolve().parent / "sets" / f"{os.environ.get('SET', 'l05')}.jsonl"
print("set", path.name, "set_hash", set_hash(path))
print(json.dumps(summarise(load(path)), indent=2))
