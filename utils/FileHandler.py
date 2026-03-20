import os, re, json, html
import tempfile
import mimetypes
import zipfile
import rarfile
import tarfile
import py7zr
from werkzeug.datastructures import FileStorage

# Your loaders
from langchain_community.document_loaders import (
    TextLoader,
    PyMuPDFLoader,
    UnstructuredWordDocumentLoader,
    UnstructuredPowerPointLoader,
    UnstructuredExcelLoader,
)


class FileProcessor:
    def __init__(self):
        self.extension_loader_map = {
            ".txt": lambda p: TextLoader(p, autodetect_encoding=True),
            ".pdf": lambda p: PyMuPDFLoader(p),
            ".docx": lambda p: UnstructuredWordDocumentLoader(p),
            ".pptx": lambda p: UnstructuredPowerPointLoader(p),
            ".xlsx": lambda p: UnstructuredExcelLoader(p),
        }

    # ================================
    # MAIN ENTRY
    # ================================
    def process_files(self, flask_files):
        # ✅ Normalize input
        if not isinstance(flask_files, list):
            flask_files = [flask_files]
        normalized_files = []

        for file in flask_files:
            extracted_files = self._handle_single_file(file)
            normalized_files.extend(extracted_files)

        # 🔥 Now pass to content extractor
        return self.extract_files_content(normalized_files)

    # ================================
    # HANDLE SINGLE FILE
    # ================================
    def _handle_single_file(self, file):
        filename = file.filename or "uploaded_file"
        ext = os.path.splitext(filename)[1].lower()

        # Save temp file
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            file.save(tmp.name)
            temp_path = tmp.name

        # ---- ZIP ----
        if zipfile.is_zipfile(temp_path):
            return self._extract_zip(temp_path)

        # ---- TAR / GZ / TGZ / BZ2 ----
        elif tarfile.is_tarfile(temp_path):
            return self._extract_tar(temp_path)

        # ---- RAR ----
        elif ext == ".rar":
            return self._extract_rar(temp_path)

        # ---- 7Z (optional) ----
        elif ext == ".7z":
            return self._extract_7z(temp_path)

        # ---- NORMAL FILE ----
        else:
            return self._build_file_dict(filename, file.content_type, temp_path)

    # ================================
    # ZIP EXTRACTION
    # ================================
    def _extract_zip(self, zip_path):
        extracted = []

        with zipfile.ZipFile(zip_path, "r") as z:
            for name in z.namelist():
                if name.endswith("/"):
                    continue

                ext = os.path.splitext(name)[1].lower()
                with z.open(name) as f:
                    data = f.read()

                extracted.append(
                    {
                        "filename": os.path.basename(name),
                        "content_type": mimetypes.guess_type(name)[0],
                        "data": data,
                    }
                )

        return extracted

    # ================================
    # RAR EXTRACTION
    # ================================
    def _extract_rar(self, rar_path):
        extracted = []

        with rarfile.RarFile(rar_path) as rf:
            for member in rf.infolist():
                if member.is_dir():
                    continue

                data = rf.read(member)
                name = member.filename

                extracted.append(
                    {
                        "filename": os.path.basename(name),
                        "content_type": mimetypes.guess_type(name)[0],
                        "data": data,
                    }
                )

        return extracted

    # ================================
    # TAR EXTRACTION
    # ================================
    def _extract_tar(self, tar_path):
        extracted = []

        with tarfile.open(tar_path, "r:*") as tar:
            for member in tar.getmembers():
                if member.isdir():
                    continue

                f = tar.extractfile(member)
                if not f:
                    continue

                data = f.read()
                name = member.name

                extracted.append(
                    {
                        "filename": os.path.basename(name),
                        "content_type": mimetypes.guess_type(name)[0],
                        "data": data,
                    }
                )

        return extracted

    # ================================
    # 7z EXTRACTION
    # ================================
    def _extract_7z(self, path):
        extracted = []

        with tempfile.TemporaryDirectory() as temp_dir:
            with py7zr.SevenZipFile(path, mode="r") as z:
                z.extractall(path=temp_dir)

            # Walk extracted files
            for root, _, files in os.walk(temp_dir):
                for file_name in files:
                    full_path = os.path.join(root, file_name)

                    try:
                        with open(full_path, "rb") as f:
                            data = f.read()

                        extracted.append(
                            {
                                "filename": file_name,
                                "content_type": mimetypes.guess_type(file_name)[0],
                                "data": data,
                            }
                        )

                    except Exception as e:
                        print(f"⚠️ Failed reading {file_name}: {e}")

        return extracted

    # ================================
    # NORMAL FILE DICT
    # ================================
    def _build_file_dict(self, filename, content_type, path):
        with open(path, "rb") as f:
            data = f.read()

        return [
            {
                "filename": filename,
                "content_type": content_type,
                "data": data,
            }
        ]

    # ================================
    # CONTENT EXTRACTION (YOUR LOGIC)
    # ================================
    def extract_files_content(self, files):
        all_file_data = []

        for f in files:
            filename = f.get("filename", "uploaded_file")
            content_type = f.get("content_type")

            name, ext = os.path.splitext(filename)
            ext = ext.lower()

            # ---- Guess extension ----
            if not ext and content_type:
                guessed_ext = mimetypes.guess_extension(content_type)
                if guessed_ext:
                    ext = guessed_ext.lower()
                    filename = name + ext

            if ext not in self.extension_loader_map:
                print(f"⚠️ Skipping unsupported file: {filename}")
                continue

            # ---- Temp write ----
            with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
                tmp.write(f["data"])
                tmp_path = tmp.name

            try:
                loader = self.extension_loader_map[ext](tmp_path)
                docs = loader.load()

                for d in docs:
                    all_file_data.append(
                        {
                            "filename": filename,
                            "type": ext,
                            "content": self.normalize_text(d.page_content),
                        }
                    )

            except Exception as e:
                print(f"❌ Extraction failed for {filename}: {str(e)}")

            finally:
                try:
                    os.unlink(tmp_path)
                except:
                    pass

        return all_file_data

    # ================================
    # TEXT NORMALIZATION
    # ================================

    def normalize_text(self, x):
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
