"""Example trusted real subprocess. No network, files, credentials, or shell."""
import json
import sys

payload = json.load(sys.stdin)
print(json.dumps({"status": "succeeded", "output": {"echo": payload, "worker": "structured-example"}}))
