"""In-app login service: email / SMS code sign-up and sign-in for app-managed Matrix accounts.

Runs as its own process (``python -m smsgw.login``), separate from the bridge, so the bridge never
holds MAS admin credentials. See docs/LOGIN.md.
"""
