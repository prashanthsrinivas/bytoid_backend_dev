import base64
import tempfile
from langchain_community.document_loaders import (
    TextLoader,
    PyMuPDFLoader,
    UnstructuredWordDocumentLoader,
    UnstructuredPowerPointLoader,
    UnstructuredExcelLoader,
)
import os
import json
import re
import html
from utils.base_logger import get_logger

logger = get_logger(__name__)


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


def _safe_json_parse2(value):
    if value is None:
        return []

    # Already parsed
    if isinstance(value, (dict, list)):
        return value

    if not isinstance(value, str):
        return []

    s = value.strip()

    # Remove any escaped quotes wrapping
    if s.startswith('"') and s.endswith('"'):
        inner = s[1:-1].replace('\\"', '"')
        s = inner.strip()

    # Extract all JSON objects/arrays from string
    json_objects = []
    decoder = json.JSONDecoder()
    idx = 0
    while idx < len(s):
        s = s[idx:].lstrip()
        if not s:
            break
        try:
            obj, idx2 = decoder.raw_decode(s)
            json_objects.append(obj)
            idx += idx2
        except json.JSONDecodeError:
            # Skip invalid prefix until next '{' or '['
            match = re.search(r"[\{\[]", s)
            if not match:
                break
            idx += match.start()
    return json_objects


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


import base64
from werkzeug.datastructures import FileStorage


def extract_file_payload(file_item, default_filename: str):
    """
    Normalizes file payload into JSON-safe format:

    {
        filename: str,
        content_type: str,
        data_base64: str
    }
    """

    if not file_item:
        return None

    # -------------------------
    # Case A: multipart upload
    # -------------------------
    if isinstance(file_item, FileStorage):

        raw_bytes = file_item.read()

        return {
            "filename": file_item.filename or default_filename,
            "content_type": file_item.content_type,
            "data_base64": base64.b64encode(raw_bytes).decode("utf-8"),
        }

    # -------------------------
    # Case B: data URL
    # -------------------------
    if isinstance(file_item, str) and file_item.startswith("data:"):

        header, b64data = file_item.split(",", 1)

        content_type = header.split(";")[0].replace("data:", "")

        return {
            "filename": default_filename,
            "content_type": content_type,
            "data_base64": b64data,  # already base64
        }

    # -------------------------
    # Case C: structured JSON
    # -------------------------
    if isinstance(file_item, dict) and "data" in file_item:

        return {
            "filename": file_item.get("filename", default_filename),
            "content_type": file_item.get("content_type"),
            "data_base64": file_item["data"],  # assume base64
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

    if not files:
        return

    for f in files:

        if not f:
            continue

        # ✅ FIX 1: read correct field
        file_data_b64 = f.get("data_base64")

        if not file_data_b64:
            logger.warning("Missing data_base64 for file: %s", f.get("filename"))
            continue

        # ✅ FIX 2: decode base64 safely
        try:
            file_data = base64.b64decode(file_data_b64)
        except Exception:
            logger.exception(
                "Failed to decode base64 for file: %s",
                f.get("filename"),
            )
            continue

        if not file_data:
            logger.warning("Empty decoded file data: %s", f.get("filename"))
            continue

        filename = f.get("filename", "file")
        content_type = f.get("content_type") or "application/octet-stream"
        ext = os.path.splitext(filename)[1].lower()

        logger.info(
            "Processing file: %s size=%d bytes ext=%s",
            filename,
            len(file_data),
            ext,
        )

        # -------------------------
        # IMAGE → inline base64 link
        # -------------------------
        if ext in IMAGE_EXTENSIONS:

            inline_data = f"data:{content_type};base64,{file_data_b64}"

            inp_links.append(inline_data)

            logger.info("Image converted to inline link: %s", filename)

            continue

        # -------------------------
        # DOCUMENT → extract text
        # -------------------------
        try:

            extracted = extract_files_content(
                [
                    {
                        "filename": filename,
                        "data": file_data,  # ✅ proper bytes
                        "content_type": content_type,
                    }
                ]
            )

            if extracted:

                extracted_payload.extend(extracted)

                logger.info(
                    "Extracted content from %s (chars=%d)",
                    filename,
                    len(str(extracted)),
                )

            else:

                logger.warning(
                    "No content extracted from %s",
                    filename,
                )

        except Exception:

            logger.exception(
                "Extraction failed for file: %s",
                filename,
            )


import asyncio


async def merge_radar(raw_chunks, user_id, credits, output_language="english"):

    if not raw_chunks:
        return {}

    prompt = f"""
        You are a deterministic JSON merger.

        Combine ALL input JSON objects into ONE valid JSON.

        STRICT REQUIREMENTS:

        1. Keep ALL blocks from ALL chunks
        2. NEVER delete anything
        3. NEVER summarize
        4. NEVER rewrite
        5. ONLY merge
        6. Preserve exact content
        7. make sure that output language is {output_language}

        Final format:

        {{
        "document_meta": {{...}},
        "structure_rationale": "...",
        "blocks": [...],
        "estimated_word_count": number
        }}

        INPUT JSON:

        {json.dumps(raw_chunks, ensure_ascii=False)}

        OUTPUT ONLY VALID JSON.
        """

    response = await get_think_fire_response2_og(
        user_id=user_id,
        user_message=prompt,
        credits=credits,
        total_input_chars=len(prompt),
    )
    return json.loads(response)


import copy


def merge_radar_chunks_deterministic(raw_chunks, output_language="english"):

    if not raw_chunks:
        return {}

    merged = {
        "document_meta": {},
        "structure_rationale": "",
        "analysis_depth": None,
        "analysis_depth_rationale": None,
        "recommendation_depth": None,
        "recommendation_depth_rationale": None,
        "recommendation_intent": [],
        "confidence_level": None,
        "core_objective": None,
        "intent_type": None,
        "blocks": [],
        "estimated_word_count": 0,
    }

    block_index = {}

    for chunk in raw_chunks:

        # document_meta merge safely
        if "document_meta" in chunk:
            merged["document_meta"].update(chunk["document_meta"])

        # structure rationale
        if chunk.get("structure_rationale") and not merged["structure_rationale"]:
            merged["structure_rationale"] = chunk["structure_rationale"]

        # analysis
        merged["analysis_depth"] = chunk.get("analysis_depth", merged["analysis_depth"])

        merged["analysis_depth_rationale"] = chunk.get(
            "analysis_depth_rationale", merged["analysis_depth_rationale"]
        )

        # recommendation
        merged["recommendation_depth"] = chunk.get(
            "recommendation_depth", merged["recommendation_depth"]
        )

        merged["recommendation_depth_rationale"] = chunk.get(
            "recommendation_depth_rationale",
            merged["recommendation_depth_rationale"],
        )

        # recommendation intent
        for intent in chunk.get("recommendation_intent", []):
            if intent not in merged["recommendation_intent"]:
                merged["recommendation_intent"].append(intent)

        # intent/core/confidence
        merged["intent_type"] = chunk.get("intent_type", merged["intent_type"])

        merged["core_objective"] = chunk.get("core_objective", merged["core_objective"])

        merged["confidence_level"] = (
            chunk.get("confidence_level")
            or chunk.get("document_meta", {}).get("confidence_level")
            or merged["confidence_level"]
        )

        # word count fix
        if "estimated_word_count" in chunk:
            merged["estimated_word_count"] += chunk["estimated_word_count"]
        elif "document_meta" in chunk:
            merged["estimated_word_count"] += chunk["document_meta"].get(
                "estimated_word_count", 0
            )

        # blocks merge
        for block in chunk.get("blocks", []):

            block_id = block.get("block_id")

            if not block_id:
                continue

            block.setdefault("micro_blocks", [])

            if block_id not in block_index:

                new_block = copy.deepcopy(block)

                merged["blocks"].append(new_block)

                block_index[block_id] = new_block

            else:

                existing_block = block_index[block_id]

                existing_block.setdefault("micro_blocks", [])

                existing_micro_ids = {
                    mb.get("micro_id") for mb in existing_block["micro_blocks"]
                }

                for micro in block["micro_blocks"]:

                    micro_id = micro.get("micro_id")

                    if micro_id not in existing_micro_ids:
                        existing_block["micro_blocks"].append(copy.deepcopy(micro))

    # cleanup
    merged = {k: v for k, v in merged.items() if v not in [None, "", [], {}]}

    return merged
