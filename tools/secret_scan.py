"""
Pre-commit scan for secrets and personal data (CLAUDE.md rule 12).

    git add -A && python tools/secret_scan.py

Exits 1 and prints every hit if anything looks like a credential or personal
detail. Run it BEFORE committing, never after: a key in git history survives
deleting the file, and the only real fix at that point is rotating it.

Deliberately noisy. A false positive costs you ten seconds; a real one costs
you a rotated credential and a rewritten history. It has already caught three
real businesses' phone numbers on their way into a public repo.
"""
import re, subprocess, sys

PATTERNS = [
    ("private key",        r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    ("OpenAI key",         r"sk-[A-Za-z0-9_\-]{20,}"),
    ("AWS key",            r"AKIA[0-9A-Z]{16}"),
    ("Google API key",     r"AIza[0-9A-Za-z_\-]{35}"),
    ("slack/github token", r"(xox[baprs]-|ghp_|gho_|ghs_|github_pat_)[A-Za-z0-9_]{10,}"),
    ("service-account",    r'"type"\s*:\s*"service_account"'),
    ("client_secret",      r"client_secret\W{1,4}[A-Za-z0-9_\-]{10,}"),
    ("password literal",   r"(?i)(password|passwd|app_password)\s*[=:]\s*['\"][^'\"]{6,}"),
    ("bearer literal",     r"(?i)bearer\s+[A-Za-z0-9\-_\.]{20,}"),
    ("email address",      r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"),
    ("UK phone",           r"\+44\s?\d[\d\s\-]{8,}"),
    ("US phone",           r"\+1\s?\d{3}[\s\-]\d{3}[\s\-]\d{4}"),
    ("windows user path",  r"[A-Za-z]:\+Users\+[A-Za-z0-9._\-]+"),
    ("spreadsheet id",     r"docs\.google\.com/spreadsheets/d/[A-Za-z0-9_\-]{20,}"),
]
# Placeholders and examples that are meant to be in the repo.
ALLOW = re.compile(
    r"you@|your@|example\.com|example\.org|yourbusiness|@x\.com|a@x\.co\.uk|"
    r"someone@|test@|noreply@|hello@yourcompany|user@|name@|"
    r"\.iam\.gserviceaccount\.com|youremail@|bugreport@moatable|"
    r"webform\.boxly|indiantypefoundry|info@acme|a@nhs\.net|"
    r"placeholder|<[^>]*@[^>]*>|@anthropic\.com|"
    # Fiction ranges reserved for exactly this: Ofcom 0161 496 0xxx / 07700
    # 900xxx, and the North American 555-01xx block.
    r"161\s?496\s?0|7700\s?900|555[\s\-]?01|"
    r"800\s?000\s?0000|333\s?000\s?0000", re.I)

diff = subprocess.run(["git", "diff", "--cached", "-U0"], capture_output=True,
                      text=True, encoding="utf-8", errors="replace").stdout
findings = []
path = "?"
for line in diff.splitlines():
    if line.startswith("+++ b/"):
        path = line[6:]
        continue
    if not line.startswith("+") or line.startswith("+++"):
        continue
    body = line[1:]
    for label, pat in PATTERNS:
        for hit in re.findall(pat, body):
            text = hit if isinstance(hit, str) else hit[0]
            snippet = body.strip()[:100]
            if ALLOW.search(snippet):
                continue
            findings.append((label, path, snippet))

if findings:
    print(f"  {len(findings)} POSSIBLE LEAK(S) — commit blocked\n")
    for label, path, snippet in findings:
        print(f"   [{label}] {path}")
        print(f"       {snippet}")
    sys.exit(1)
print("  clean: no secrets or personal data found in the staged diff")
