#!/usr/bin/env python3
"""Generate sample log files for testing LogViper."""

import random
import time
from datetime import datetime, timedelta
import os

SERVICES = ["auth", "api", "worker", "database"]
LEVELS = ["INFO", "INFO", "INFO", "DEBUG", "WARN", "ERROR", "FATAL"]
MESSAGES = {
    "INFO": [
        "Request processed successfully in {n}ms",
        "User {user} logged in from {ip}",
        "Cache hit for key user:{id}",
        "Connection pool: {n}/{m} connections active",
        "Scheduled task completed: backup_{n}",
        "Health check passed [uptime={n}s]",
    ],
    "DEBUG": [
        "SQL query executed in {n}μs: SELECT * FROM sessions WHERE id={id}",
        "Thread {n} started processing queue item",
        "Memory usage: {n}MB heap, {m}MB stack",
    ],
    "WARN": [
        "Slow query detected ({n}ms): SELECT * FROM logs WHERE created > '2024-01-01'",
        "Connection pool near capacity: {n}/{m}",
        "Retry attempt {n}/3 for request to upstream service",
        "Disk usage at {n}% on /var/log",
    ],
    "ERROR": [
        "Failed to connect to database: Connection refused (attempt {n})",
        "Unhandled exception in worker thread {n}: NullPointerException",
        "Request timeout after {n}ms for endpoint /api/v2/users",
        "Authentication failed for user {user}: Invalid credentials",
    ],
    "FATAL": [
        "Out of memory: Cannot allocate {n}MB",
        "Disk full: /var/log has 0 bytes remaining",
    ],
}

def rand_ip(): return f"{random.randint(10,192)}.{random.randint(0,255)}.{random.randint(0,255)}.{random.randint(1,254)}"
def rand_user(): return random.choice(["alice", "bob", "charlie", "dave", "eve", "frank"])

def gen_line(ts: datetime, service: str) -> str:
    level = random.choice(LEVELS)
    template = random.choice(MESSAGES[level])
    msg = template.format(
        n=random.randint(1, 9999),
        m=random.randint(10, 100),
        id=random.randint(1000, 9999),
        ip=rand_ip(),
        user=rand_user(),
    )
    return f"{ts.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} [{level:5s}] [{service}] {msg}"

def generate(output_dir: str = ".", days: int = 0, count: int = 200):
    os.makedirs(output_dir, exist_ok=True)
    
    now = datetime.now()
    start = now - timedelta(days=days, hours=1)
    
    for service in SERVICES:
        lines = []
        ts = start
        for _ in range(count):
            ts += timedelta(seconds=random.uniform(0.1, 5))
            lines.append(gen_line(ts, service))
        
        fpath = os.path.join(output_dir, f"{service}.log")
        with open(fpath, 'w') as f:
            f.write('\n'.join(lines) + '\n')
        print(f"✅ Generated {fpath} ({count} lines)")
    
    # Also generate a rolled-over version
    service = SERVICES[0]
    for suffix in [2, 1]:
        lines = []
        ts = start - timedelta(hours=suffix * 2)
        for _ in range(50):
            ts += timedelta(seconds=random.uniform(1, 10))
            lines.append(gen_line(ts, service))
        fpath = os.path.join(output_dir, f"{service}.log.{suffix}")
        with open(fpath, 'w') as f:
            f.write('\n'.join(lines) + '\n')
        print(f"✅ Generated rolled file {fpath}")

if __name__ == "__main__":
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else "sample_logs"
    generate(out)
    print(f"\nNow run: python3 logviper.py {out}/auth.log {out}/api.log {out}/worker.log {out}/database.log")
