from urllib.parse import urlsplit


def normalize_domain(value: str) -> str:
    """Reduce any reported location to a bare hostname.

    Deliberately duplicated in agent/website_bridge.py: the desktop agent ships
    separately and must not import from the server package. Keep the two copies
    identical, or the browser extension and the server will disagree about what
    counts as a domain.
    """
    candidate = value.strip().lower()
    if not candidate:
        return ""
    parsed = urlsplit(candidate if "://" in candidate else f"https://{candidate}")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    host = parsed.hostname.rstrip(".").removeprefix("www.")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return ""
    if len(host) > 253 or any(not label for label in host.split(".")):
        return ""
    return host
