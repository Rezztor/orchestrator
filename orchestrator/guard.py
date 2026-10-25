"""Guardrails on text: secrets out of everything the model and the ledger see, and instruction-like text in tool
output flagged."""
import re

SECRET = re.compile("|".join([
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)",
    r"\b(?:password|passwd|pwd|passphrase|secret|api[_-]?key|token|community)\b\s*[:=]\s*\S+",
    r"\b(?:enable )?(?:secret|password) [0-9] \S+",      # Cisco-style stored secrets
    r"\$(?:1|5|6|9|y)\$\S+",                              # crypt and Juniper $9$ hashes
    r"\bAKIA[0-9A-Z]{16}\b",                              # AWS access key id
    r"\bgh[pousr]_[A-Za-z0-9]{30,}\b",                    # GitHub tokens
    r"\bxox[abpr]-[A-Za-z0-9-]{10,}\b",                   # Slack tokens
    r"\bsk-[A-Za-z0-9_-]{20,}\b",                         # API secret keys
]), re.IGNORECASE)
SECRET_KEY = re.compile(r"pass|secret|token|private|credential|community|api_?key", re.IGNORECASE)
INSTRUCTION = re.compile("|".join([
    r"ignore (?:all |any )?(?:the )?(?:previous|prior|above|earlier) (?:instructions|prompts|rules)",
    r"disregard (?:the |all |your |any )?(?:previous |prior )?(?:instructions|rules|guidelines)",
    r"you are now\b", r"new instructions\s*:", r"\bsystem prompt\b", r"\bdeveloper mode\b",
    r"\bact as (?:an? )?(?:admin|administrator|root|developer)\b",
    r"forget (?:all |your |the )?(?:previous |prior |earlier )?(?:instructions|rules)",
    r"override (?:all |your |the )?(?:previous |prior )?(?:instructions|rules)", r"ignore the above\b",
    r"from now on,? you (?:must|will|should|are)\b", r"\bdo not tell the user\b",
]), re.IGNORECASE)
# shortcut: pattern lists, not a trained classifier; add a model-based check when real traffic shows misses.


def has_secret(text):
    return bool(SECRET.search(text))


def redact(value):
    """Copy of a JSON-like value with secrets replaced by [redacted]."""
    if isinstance(value, dict):
        return {k: "[redacted]" if SECRET_KEY.search(str(k)) and value[k] not in (None, "", [], {})
                else redact(value[k]) for k in value}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return SECRET.sub("[redacted]", value)
    return value


def instructions_in(value):
    """Instruction-like phrases found anywhere in a JSON-like value."""
    if isinstance(value, dict):
        return [hit for v in value.values() for hit in instructions_in(v)]
    if isinstance(value, (list, tuple)):
        return [hit for v in value for hit in instructions_in(v)]
    return INSTRUCTION.findall(value) if isinstance(value, str) else []
