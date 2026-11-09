#!/usr/bin/env python3
"""Muestra los eventos de autokub en una línea por paso. Lee JSON por la entrada estándar."""
import json
import sys

COLORS = {"denied": "31", "rejected": "31", "failed": "31", "escalated": "33", "would_execute": "36",
          "executed": "32", "passed": "32", "closed": "32", "allowed": "32"}

for line in sys.stdin:
    try:
        event = json.loads(line[line.index("{"):])
    except ValueError:
        continue
    if not isinstance(event, dict) or "event" not in event:
        continue
    color = next((c for key, c in COLORS.items() if key in event["event"]), "0")
    summary = str(event.get("summary", "")).replace("\n", " ")[:100]
    print(f"{event['timestamp'][11:19]}  \033[{color}m{event['event']:<22}\033[0m {event.get('component', ''):<9} {summary}",
          flush=True)
