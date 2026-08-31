import re
from urllib.parse import urlparse
from typing import Optional, Dict, List, Tuple

# Structure: filename -> list of tuples (component, compiled_regex, score)
# component in {"host","path","any"}; higher score = higher priority
PLATFORM_PATTERNS: Dict[str, List[Tuple[str, re.Pattern, int]]] = {
    "cpanel.txt": [
        ("host", re.compile(r":2083\b", re.I), 90),
        ("path", re.compile(r"(^|/)(cpanel)(/|$)", re.I), 80),
    ],
    "drupal.txt": [
        ("path", re.compile(r"(^|/)(user|user/login)(/|$)", re.I), 80),
        ("any", re.compile(r"\bdrupal\b", re.I), 50),
    ],
    "ftp.txt": [
        ("any", re.compile(r"^ftp://", re.I), 120),
        ("host", re.compile(r":21\b", re.I), 90),
    ],
    "joomla.txt": [
        ("path", re.compile(r"(^|/)(administrator)(/|$)", re.I), 100),
        ("any", re.compile(r"\bjoomla\b", re.I), 60),
    ],
    "moodle.txt": [
        ("any", re.compile(r"\bmoodle\b", re.I), 80),
        ("path", re.compile(r"/login/index\.php", re.I), 70),
    ],
    "ojs_journal.txt": [
        ("path", re.compile(r"(/index/(login|user)|/user/login|/journal/index\.php)", re.I), 90),
        ("any", re.compile(r"\b(ojs|jurnal|ejournal|e-?journal|journal)\b", re.I), 70),
    ],
    "phpmyadmin.txt": [
        ("any", re.compile(r"\bphpmyadmin\b", re.I), 120),
        ("path", re.compile(r"(^|/)(phpmyadmin)(/|$)", re.I), 100),
    ],
    "plesk.txt": [
        ("host", re.compile(r":8443\b", re.I), 90),
        ("any", re.compile(r"\bplesk\b", re.I), 100),
    ],
    "prestashop.txt": [
        ("path", re.compile(r"(^|/)(admin[-_a-z0-9]{0,20}|admin-dev)(/|/index\.php|$)", re.I), 120),
        ("any", re.compile(r"\bprestashop\b", re.I), 100),
    ],
    "ssh.txt": [
        ("any", re.compile(r"^ssh://", re.I), 120),
        ("host", re.compile(r":22\b", re.I), 90),
    ],
    "whm.txt": [
        ("host", re.compile(r":2087\b", re.I), 90),
        ("path", re.compile(r"(^|/)(whm)(/|$)", re.I), 100),
    ],
    "wordpress.txt": [
        ("path", re.compile(r"(^|/)(wp-login\.php|wp-admin)(/|$)", re.I), 120),
        ("any", re.compile(r"\bwordpress\b|\bwp-admin\b|\bwp-login\b", re.I), 100),
    ],
}


def identify_platform(url_extracted: str) -> Optional[str]:
    """
    Return the platform filename (e.g. 'wordpress.txt') for the
    provided URL/string, or None if no pattern matched.

    The function normalizes the input and uses urllib.parse.urlparse to
    inspect the host/netloc and path components. It then runs the
    compiled regex patterns and returns the highest-scoring match.
    """
    if not url_extracted:
        return None

    s = url_extracted.strip()
    # Ensure urlparse can extract netloc for host-only strings
    parse_target = s if re.match(r'^[a-zA-Z][a-zA-Z0-9+\-.]*://', s) else ("http://" + s)

    try:
        p = urlparse(parse_target)
    except Exception:
        p = None

    host = (p.netloc or "").lower() if p else ""
    path = (p.path or "").lower() if p else ""
    whole = s.lower()

    best_match = (None, -1)  # (filename, score)

    # Prestashop special-case: adminXYZ/index.php style admin folders
    if re.search(r"(?:/|^)(?:admin|adm|administrator)[-_a-z0-9]{0,20}/index\.php", path, re.I):
        return "prestashop.txt"

    for filename, patterns in PLATFORM_PATTERNS.items():
        for component, regex, score in patterns:
            matched = False
            if component == "host" and host:
                if regex.search(host):
                    matched = True
            elif component == "path" and path:
                if regex.search(path):
                    matched = True
            elif component == "any":
                if regex.search(whole):
                    matched = True

            if matched and score > best_match[1]:
                best_match = (filename, score)

    return best_match[0]
