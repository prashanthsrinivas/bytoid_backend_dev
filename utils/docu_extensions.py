from langchain_community.document_loaders import (
    TextLoader,
    UnstructuredWordDocumentLoader,
    UnstructuredPowerPointLoader,
    UnstructuredExcelLoader,
    UnstructuredPDFLoader,
    UnstructuredCSVLoader,
    UnstructuredXMLLoader,
    UnstructuredMarkdownLoader,
    UnstructuredRTFLoader,
    UnstructuredODTLoader,
)

extension_loader_map = {
    ".txt": lambda p: TextLoader(p, autodetect_encoding=True),
    ".pdf": lambda p: UnstructuredPDFLoader(
        p, mode="elements", strategy="fast", infer_table_structure=True
    ),
    ".docx": lambda p: UnstructuredWordDocumentLoader(p, mode="elements"),
    ".pptx": lambda p: UnstructuredPowerPointLoader(p, mode="elements"),
    ".xlsx": lambda p: UnstructuredExcelLoader(p, mode="elements"),
    ".doc": lambda p: UnstructuredWordDocumentLoader(p, mode="elements"),
    ".xls": lambda p: UnstructuredExcelLoader(p, mode="elements"),
    ".csv": lambda p: UnstructuredCSVLoader(p),
    ".json": lambda p: TextLoader(p, autodetect_encoding=True),
    ".xml": lambda p: UnstructuredXMLLoader(p),
    ".yaml": lambda p: TextLoader(p, autodetect_encoding=True),
    ".yml": lambda p: TextLoader(p, autodetect_encoding=True),
    ".md": lambda p: UnstructuredMarkdownLoader(p),
    ".rtf": lambda p: UnstructuredRTFLoader(p),
    ".odt": lambda p: UnstructuredODTLoader(p),
    ".ods": lambda p: UnstructuredExcelLoader(p, mode="elements"),
}
