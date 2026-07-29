from urllib.parse import urlsplit


def normalize_domain(value: str) -> str:
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
