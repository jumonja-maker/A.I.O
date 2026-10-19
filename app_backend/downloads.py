"""Pure native-save policy shared by Qt and tests; no Qt import required."""
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit


def download_filter(url: str, name: str, port: int) -> str | None:
    suffix = Path(name).suffix.lower()
    origin = f'http://127.0.0.1:{port}'
    if url.startswith('blob:' + origin + '/') and suffix == '.xlsx':
        return 'Excel (*.xlsx)'
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.port != port
                or parsed.username is not None or parsed.password is not None
                or parsed.path != '/api/converter/result' or parsed.fragment):
            return None
    except ValueError:
        return None
    query = parse_qs(parsed.query, keep_blank_values=True)
    if set(query) - {'ticket', 'aioSave'} or len(query.get('ticket', [])) != 1:
        return None
    if not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', query['ticket'][0]):
        return None
    if 'aioSave' in query and (len(query['aioSave']) != 1 or
                              not re.fullmatch(r'[1-9][0-9]{0,12}', query['aioSave'][0])):
        return None
    if not suffix or not re.fullmatch(r'\.[a-zA-Z0-9_-]+', suffix):
        return None
    return suffix[1:].upper() + ' (*' + suffix + ')'
