import tempfile
from langchain_community.document_loaders import (
    TextLoader,
    PyMuPDFLoader,
    UnstructuredWordDocumentLoader,
    UnstructuredPowerPointLoader,
    UnstructuredExcelLoader,
)
import asyncio
import os
import json
import time
import re
import html


def build_documents(sources):
    if not sources:
        return "No evidence found."

    docs = []
    for i, s in enumerate(sources, 1):
        src = s.get("source") or s.get("endpoint_id") or s.get("note_id") or "unknown"
        raw = s.get("data", s)

        docs.append(
            f"""
            DOCUMENT {i}
            Source: {src}
            Type: {s.get("type","unknown")}
            Content:
            {normalize_text(raw)}
            """
        )
    return "\n".join(docs)

def normalize_text(x):
    try:
        if x is None:
            return ""
        if isinstance(x, str):
            s = x
        else:
            s = json.dumps(x, ensure_ascii=False, indent=2)
    except:
        s = str(x)

    s = html.unescape(s)
    s = re.sub(r"<script.*?>.*?</script>", "", s, flags=re.S)
    s = re.sub(r"<style.*?>.*?</style>", "", s, flags=re.S)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:120000]


def extract_json(text: str) -> str:
    """
    Extract the first valid JSON object from LLM output.
    """
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        raise ValueError("No JSON object found in LLM output")

    json_text = match.group(0)

    # Remove trailing commas (very common LLM issue)
    json_text = re.sub(r",\s*}", "}", json_text)
    json_text = re.sub(r",\s*]", "]", json_text)

    return json_text.strip()


def _safe_json_parse(value):
    if value is None:
        return {}

    # Already parsed
    if isinstance(value, (dict, list)):
        return value

    if not isinstance(value, str):
        return {}

    s = value.strip()

    # 🔑 ONLY CASE WE HANDLE:
    # "\"{ ... }\""  or "\"[ ... ]\""
    if s.startswith('"') and s.endswith('"'):
        inner = s[1:-1]

        # unescape quotes
        inner = inner.replace('\\"', '"')

        # must now be valid JSON
        if inner.startswith("{") or inner.startswith("["):
            try:
                return json.loads(inner)
            except Exception:
                return {}

    # If string already starts with JSON directly
    if s.startswith("{") or s.startswith("["):
        try:
            return json.loads(s)
        except Exception:
            return {}

    return {}

import mimetypes
def extract_files_content(files):
    all_file_data = []

    extension_loader_map = {
        ".txt": lambda p: TextLoader(p, autodetect_encoding=True),
        ".pdf": lambda p: PyMuPDFLoader(p),
        ".docx": lambda p: UnstructuredWordDocumentLoader(p),
        ".pptx": lambda p: UnstructuredPowerPointLoader(p),
        ".xlsx": lambda p: UnstructuredExcelLoader(p),
    }

    for f in files:
        filename = f.get("filename", "uploaded_file")
        content_type = f.get("content_type")

        # ---- 1️⃣ Ensure extension exists ----
        name, ext = os.path.splitext(filename)
        ext = ext.lower()

        if not ext and content_type:
            guessed_ext = mimetypes.guess_extension(content_type)
            if guessed_ext:
                ext = guessed_ext.lower()
                filename = name + ext

        if ext not in extension_loader_map:
            print(f"⚠️ Skipping unsupported file: {filename} (ext='{ext}')")
            continue

        # ---- 2️⃣ Write temp file ----
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            tmp.write(f["data"])
            tmp_path = tmp.name

        try:
            loader = extension_loader_map[ext](tmp_path)
            docs = loader.load()

            if not docs:
                print(f"⚠️ No content extracted from {filename}")
                continue

            for d in docs:
                all_file_data.append(
                    {
                        "filename": filename,
                        "type": ext,
                        "content": normalize_text(d.page_content),
                    }
                )

        except Exception as e:
            print(f"❌ Extraction failed for {filename}: {str(e)}")

        finally:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    return all_file_data
def build_file_data_payload(file_data):
    if not file_data:
        return ""

    blocks = []
    for i, f in enumerate(file_data, 1):
        blocks.append(
            f"""
            FILE {i}
            Name: {f['filename']}
            Type: {f['type']}
            Extracted Content:
            {f['content']}
            """
        )
    return "\n".join(blocks)
