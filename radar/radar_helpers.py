import base64
import io
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

from utils.s3_utils import upload_any_file_and_get_url


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


def extract_file_payload(file_item, default_filename: str):
    """
    Extracts a file payload into a normalized dict:
    {
        filename,
        content_type,
        data (bytes)
    }
    Returns None if invalid.
    """

    if not file_item:
        return None

    # Case A: data URL string
    if isinstance(file_item, str) and file_item.startswith("data:"):
        header, b64data = file_item.split(",", 1)
        content_type = header.split(";")[0].replace("data:", "")

        return {
            "filename": default_filename,
            "content_type": content_type,
            "data": base64.b64decode(b64data),
        }

    # Case B: structured JSON object
    if isinstance(file_item, dict) and "data" in file_item:
        return {
            "filename": file_item.get("filename", default_filename),
            "content_type": file_item.get("content_type"),
            "data": base64.b64decode(file_item["data"]),
        }

    return None


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def process_file_payloads(
    *,
    user_id,
    files,
    inp_links,
    extracted_payload,
):
    """
    files: list[dict] or None
    inp_links: list (mutated)
    extracted_payload: list (mutated)
    """

    if not files:
        return

    for f in files:
        if not f or "data" not in f:
            continue

        filename = f.get("filename", "file")
        ext = os.path.splitext(filename)[1].lower()

        # ---- IMAGE → upload & link ----
        # if ext in IMAGE_EXTENSIONS:
        #     file_obj = io.BytesIO(f["data"])
        #     file_obj.name = filename

        #     url = upload_any_file_and_get_url(
        #         user_id=user_id,
        #         file_obj=file_obj,
        #         filename=filename,
        #         content_type=f.get("content_type") or "application/octet-stream",
        #     )
        #     inp_links.append(url)
        #     continue
        if ext in IMAGE_EXTENSIONS:
            # Convert binary → inline base64
            encoded = base64.b64encode(f["data"]).decode("utf-8")
            content_type = f.get("content_type") or "image/png"

            inline_data = f"data:{content_type};base64,{encoded}"

            inp_links.append(inline_data)
            continue

        # ---- DOCUMENT → extract ----
        extracted = extract_files_content([f])
        if extracted:
            extracted_payload.extend(extracted)
