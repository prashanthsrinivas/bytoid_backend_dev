import re
import os


def extract_filename(path_or_url: str) -> str:
    """Extract only the filename (basename) from a full path or URL."""
    if not path_or_url:
        return None
    return os.path.basename(path_or_url.split("?")[0])


def extract_transcript_filename(url: str) -> str:
    match = re.search(r"/([^/?]+)(?:\?|$)", url)
    return match.group(1) if match else None
