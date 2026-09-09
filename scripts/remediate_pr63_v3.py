from pathlib import Path

path = Path("oauth_auth.py")
text = path.read_text(encoding="utf-8")
old = '''        if not (\n            isinstance(item, dict)\n            and item.get("expires_at", 0) > time.time()\n            and item.get("resource") == self.config.resource\n            and item.get("client_id") == self.config.client_id\n        ):\n'''
new = '''        if not (\n            isinstance(item, dict)\n            and item.get("expires_at", 0) > time.time()\n            and item.get("resource") == self.config.resource\n        ):\n'''
count = text.count(old)
if count != 1:
    raise RuntimeError(f"oauth_auth.py: expected one durable-token predicate, found {count}")
path.write_text(text.replace(old, new), encoding="utf-8")

print("PR #63 OAuth compatibility pass applied")
