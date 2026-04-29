import base64
import tempfile
import os
import json
import re
import html
from utils.base_logger import get_logger
from utils.app_configs import IS_DEV
from utils.docu_extensions import extension_loader_map
logger = get_logger(__name__, log_level="DEBUG" if IS_DEV else "INFO")


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


def normalize_text(text):
    if not text:
        return ""
    return " ".join(text.strip().split())


_ARCHIVE_EXTENSIONS = {".tar.gz", ".tgz", ".tar", ".zip"}


def _extract_archive_files(filename, file_bytes):
    """Extract archive contents and return list of {filename, data, content_type} dicts."""
    import tarfile
    import zipfile
    import mimetypes as _mimetypes

    fname_lower = filename.lower()
    extracted = []

    with tempfile.TemporaryDirectory() as tmpdir:
        if fname_lower.endswith(".tar.gz") or fname_lower.endswith(".tgz") or fname_lower.endswith(".tar"):
            mode = "r:gz" if (fname_lower.endswith(".tar.gz") or fname_lower.endswith(".tgz")) else "r:"
            with tempfile.NamedTemporaryFile(delete=False, suffix=".tar.gz") as tmp:
                tmp.write(file_bytes)
                tmp_path = tmp.name
            try:
                with tarfile.open(tmp_path, mode) as tar:
                    tar.extractall(tmpdir)
            finally:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

        elif fname_lower.endswith(".zip"):
            with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as tmp:
                tmp.write(file_bytes)
                tmp_path = tmp.name
            try:
                with zipfile.ZipFile(tmp_path, "r") as zf:
                    zf.extractall(tmpdir)
            finally:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass

        for root, _, files_in_dir in os.walk(tmpdir):
            for inner_name in files_in_dir:
                inner_path = os.path.join(root, inner_name)
                ext = os.path.splitext(inner_name)[1].lower()
                if ext not in extension_loader_map:
                    continue
                try:
                    with open(inner_path, "rb") as fh:
                        data = fh.read()
                    content_type = _mimetypes.guess_type(inner_name)[0] or "application/octet-stream"
                    extracted.append({"filename": inner_name, "data": data, "content_type": content_type})
                except Exception as e:
                    logger.warning("Could not read archive member %s: %s", inner_name, e)

    return extracted


def extract_files_content(files):
    import mimetypes as _mimetypes
    all_file_data = []

    for f in files:
        filename = f.get("filename", "uploaded_file")
        content_type = f.get("content_type")
        fname_lower = filename.lower()

        # ── Archive: explode and recurse ──────────────────────────────
        if any(fname_lower.endswith(ext) for ext in _ARCHIVE_EXTENSIONS):
            raw_data = f.get("data") or b""
            if not raw_data:
                logger.warning("Empty archive: %s", filename)
                continue
            inner_files = _extract_archive_files(filename, raw_data)
            if not inner_files:
                logger.warning("No extractable files in archive: %s", filename)
                continue
            nested = extract_files_content(inner_files)
            all_file_data.extend(nested)
            continue

        # ── Normal file ───────────────────────────────────────────────
        name, ext = os.path.splitext(filename)
        ext = ext.lower()

        if not ext and content_type:
            guessed_ext = _mimetypes.guess_extension(content_type)
            if guessed_ext:
                ext = guessed_ext.lower()
                filename = name + ext

        if ext not in extension_loader_map:
            logger.debug("Skipping unsupported file: %s", filename)
            continue

        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            tmp.write(f["data"])
            tmp_path = tmp.name

        try:
            loader = extension_loader_map[ext](tmp_path)
            docs = loader.load()

            # print(f"DEBUG: docs length = {len(docs)}")

            if not docs:
                logger.warning("No content extracted from %s", filename)
                continue

            # 🔥 JUST COMBINE EVERYTHING
            combined_text = []

            for d in docs:
                text = normalize_text(d.page_content)

                if text:  # only skip fully empty
                    combined_text.append(text)

            final_text = "\n".join(combined_text)

            all_file_data.append(
                {
                    "filename": filename,
                    "type": ext,
                    "content": final_text,  # 🔥 STRING ONLY
                }
            )

        except Exception as e:
            logger.error("Extraction failed for %s: %s", filename, e)

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
import mimetypes
from werkzeug.datastructures import FileStorage


def extract_file_payload(file_item, default_filename: str = "file"):
    """
    Normalize any file input into JSON-safe payload:

    Returns:
    {
        "filename": str,
        "content_type": str,
        "data_base64": str
    }
    """

    if not file_item:
        return None

    try:
        # =========================
        # Case A: multipart upload
        # =========================
        if isinstance(file_item, FileStorage):

            raw_bytes = file_item.read()
            if not raw_bytes:
                return None

            filename = file_item.filename or default_filename
            content_type = file_item.content_type or "application/octet-stream"

            data_base64 = base64.b64encode(raw_bytes).decode("utf-8")

        # =========================
        # Case B: data URL (frontend)
        # =========================
        elif isinstance(file_item, str) and file_item.startswith("data:"):

            header, b64data = file_item.split(",", 1)

            content_type = header.split(";")[0].replace("data:", "")

            # ✅ CLEAN base64 (important)
            b64data = b64data.strip().replace("\n", "").replace("\r", "")

            # ✅ Fix padding
            missing_padding = len(b64data) % 4
            if missing_padding:
                b64data += "=" * (4 - missing_padding)

            data_base64 = b64data

            filename = default_filename

        # =========================
        # Case C: structured JSON
        # =========================
        elif isinstance(file_item, dict):

            filename = (
                file_item.get("filename") or file_item.get("name") or default_filename
            )

            content_type = (
                file_item.get("content_type")
                or file_item.get("type")
                or "application/octet-stream"
            )

            if "data_base64" in file_item:
                data_base64 = file_item["data_base64"]

            elif "data" in file_item:
                # assume raw bytes OR base64
                if isinstance(file_item["data"], bytes):
                    data_base64 = base64.b64encode(file_item["data"]).decode("utf-8")
                else:
                    data_base64 = file_item["data"]

            else:
                return None

        else:
            return None

        # =========================
        # ✅ Ensure extension exists
        # =========================
        name, ext = filename.rsplit(".", 1) if "." in filename else (filename, "")

        if not ext:
            guessed_ext = mimetypes.guess_extension(content_type)

            # 🔥 special fix for docx (VERY IMPORTANT)
            if not guessed_ext and "wordprocessingml" in content_type:
                guessed_ext = ".docx"

            if guessed_ext:
                filename = filename + guessed_ext

        # =========================
        # ✅ Final output
        # =========================
        return {
            "filename": filename,
            "content_type": content_type,
            "data_base64": data_base64,
        }

    except Exception as e:
        logger.error("extract_file_payload error: %s", e)
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

        # =========================
        # ✅ NEW: normalize FileStorage → dict (FIRST)
        # =========================
        if hasattr(f, "read"):  # form-data file
            try:
                file_bytes = f.read()

                if not file_bytes:
                    logger.warning("Empty uploaded file")
                    continue

                file_data_b64 = base64.b64encode(file_bytes).decode("utf-8")

                f = {
                    "filename": getattr(f, "filename", "file"),
                    "content_type": getattr(
                        f, "content_type", "application/octet-stream"
                    ),
                    "data_base64": file_data_b64,
                }

            except Exception:
                logger.exception("Failed to process uploaded file")
                continue

        # =========================
        # ✅ EXISTING LOGIC (unchanged)
        # =========================
        file_data_b64 = f.get("data_base64")

        if file_data_b64 and file_data_b64.startswith("data:"):
            file_data_b64 = file_data_b64.split(",", 1)[1]

        if not file_data_b64:
            logger.warning("Missing data_base64 for file: %s", f.get("filename"))
            continue

        # ✅ decode base64
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
                        "data": file_data,
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
                logger.warning("No content extracted from %s", filename)

        except Exception:
            logger.exception("Extraction failed for file: %s", filename)




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
