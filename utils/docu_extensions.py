from langchain_community.document_loaders import (
    TextLoader,
    PyMuPDFLoader,
    UnstructuredWordDocumentLoader,
    UnstructuredPowerPointLoader,
    UnstructuredExcelLoader,
    UnstructuredPDFLoader,
)

extension_loader_map = {
    ".txt": lambda p: TextLoader(p, autodetect_encoding=True),
    ".pdf": lambda p: UnstructuredPDFLoader(
        p,
        mode="elements",
        strategy="fast",
        infer_table_structure=True,
    ),
    ".docx": lambda p: UnstructuredWordDocumentLoader(p, mode="elements"),
    ".pptx": lambda p: UnstructuredPowerPointLoader(p, mode="elements"),
    ".xlsx": lambda p: UnstructuredExcelLoader(p, mode="elements"),
}
