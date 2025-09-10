import re
import os


def extract_filename(path_or_url) -> str:
    """Extract only the filename (basename) from a full path or URL."""
    # unwrap list/tuple if needed
    if isinstance(path_or_url, (list, tuple)):
        path_or_url = path_or_url[0]
    if not path_or_url:
        return None
    return os.path.basename(str(path_or_url).split("?")[0])


def extract_transcript_filename(url: str) -> str:
    match = re.search(r"/([^/?]+)(?:\?|$)", url)
    return match.group(1) if match else None
