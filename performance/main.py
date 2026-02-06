#!/usr/bin/env python3
import time
import json

def simple_function(n):
    tmp = []
    for i in range(n):
        tmp.append(i + tmp[-1] if len(tmp) > 0 else 0)
    return sum(tmp)


if __name__ == "__main__":
    times = []
    for i in range(100):
        t1 = time.time()
        simple_function(i)
        t2 = time.time()
        times.append(t2 - t1)

    # export as json
    with open("perf.json", "w") as f:
        json.dump({"times": times, "db_size": 0}, f)
