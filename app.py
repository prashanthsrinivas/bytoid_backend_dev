from flask import Flask
from google_route.routes import google_bp
from facebook_route.routes import facebook_bp
from agent_route.routes import agent_bp
from gmail_route.routes import gmail_bp
from session_manager_route.routes import session_bp
from microsoft_route.routes import microsoft_bp
from users_routes.routes import users_bp
from webhooks.routes import twilio_bp
from contacts_route.route import contacts_bp
from playbook.routes import playbook_bp
from zoho_routes.routes import zoho_bp
from credits_route.route import credits_bp
from umail.routes import umail_bp
import os
from dotenv import load_dotenv
from flask_cors import CORS
import tempfile

load_dotenv()
app = Flask(__name__)
app.secret_key = os.getenv(
    "SECRETKEY"
)  # set a secret key as an enviornmental variable later
app.config.update(SESSION_COOKIE_SAMESITE="None", SESSION_COOKIE_SECURE=True)

CORS(
    app,
    supports_credentials=True,
    origins=["http://172.31.12.212", "https://www.bytoid.ai", "https://bytoid.ai"],
)
CORS(
    google_bp,
    supports_credentials=True,
    origins=["http://172.31.12.212", "https://www.bytoid.ai", "https://bytoid.ai"],
)
CORS(
    facebook_bp,
    supports_credentials=True,
    origins=["http://172.31.12.212", "https://www.bytoid.ai", "https://bytoid.ai"],
)
CORS(
    agent_bp,
    supports_credentials=True,
    origins=["http://172.31.12.212", "https://www.bytoid.ai", "https://bytoid.ai"],
)
CORS(
    playbook_bp,
    supports_credentials=True,
    origins=["http://172.31.12.212", "https://www.bytoid.ai", "https://bytoid.ai"],
)
# CORS(
#     agent_bp,
#     supports_credentials=True,
#     origins=["*"],
# )
CORS(
    gmail_bp,
    supports_credentials=True,
    origins=["http://172.31.12.212", "https://www.bytoid.ai", "https://bytoid.ai"],
)
CORS(
    session_bp,
    supports_credentials=True,
    origins=["http://172.31.12.212", "https://www.bytoid.ai", "https://bytoid.ai"],
)
CORS(
    microsoft_bp,
    supports_credentials=True,
    origins=["http://172.31.12.212", "https://www.bytoid.ai", "https://bytoid.ai"],
)
CORS(
    twilio_bp,
    supports_credentials=True,
    origins=["http://172.31.12.212", "https://www.bytoid.ai", "https://bytoid.ai"],
)
CORS(
    users_bp,
    supports_credentials=True,
    origins=["http://172.31.12.212", "https://www.bytoid.ai", "https://bytoid.ai"],
)
CORS(
    contacts_bp,
    supports_credentials=True,
    origins=["http://172.31.12.212", "https://www.bytoid.ai", "https://bytoid.ai"],
)
CORS(
    zoho_bp,
    supports_credentials=True,
    origins=["http://172.31.12.212", "https://www.bytoid.ai", "https://bytoid.ai"],
)
CORS(
    umail_bp,
    supports_credentials=True,
    origins=["http://172.31.12.212", "https://www.bytoid.ai", "https://bytoid.ai"],
)
# Environment variables
PINECONE_API_KEY = os.getenv("PINECONE_API_KEY")
openai_api_key = os.getenv("OPENAI_API_KEY")
assistant_id = os.getenv("ASSISTANT_ID")
PINECONE_ENV = os.getenv("PINECONE_ENV") or "us-east-1"
PINECONE_INDEX_NAME = os.getenv("PINECONE_INDEX_NAME") or "sbdev"


# # Initialize Pinecone client
# pc = PineconeClient(api_key=PINECONE_API_KEY)

# # Create index if it doesn't exist
# if PINECONE_INDEX_NAME not in pc.list_indexes().names():
#     pc.delete_index(PINECONE_INDEX_NAME)
#     pc.create_index(
#         name=PINECONE_INDEX_NAME,
#         dimension=3072,
#         metric="cosine",
#         spec=ServerlessSpec(
#             cloud="aws",
#             region="ap-east-1"  # Update if needed
#         )
#     )

# Connect to index
# index = pc.Index(PINECONE_INDEX_NAME)

# Prepare embedding model (used later in vectorstore)
# embedding = OpenAIEmbeddings(
#     model="text-embedding-3-large",
#     openai_api_key=openai_api_key,
#     dimensions=3072,  # 🚨 explicitly reinforce this to match your Pinecone index
# )

# vector = embedding.embed_query("hello world")

app.config["SESSION_FILE_DIR"] = os.path.join(tempfile.gettempdir(), "flask_sessions")
os.makedirs(app.config["SESSION_FILE_DIR"], exist_ok=True)

# Create data directory if not exists
os.makedirs("data", exist_ok=True)

# Load documents
# all_documents = []

# txt_loader = DirectoryLoader("data", glob="**/*.txt", loader_cls=TextLoader)
# all_documents.extend(txt_loader.load())

# pdf_loader = DirectoryLoader("data", glob="**/*.pdf", loader_cls=PyMuPDFLoader)
# all_documents.extend(pdf_loader.load())

# doc_loader = DirectoryLoader("data", glob="**/*.docx", loader_cls=UnstructuredWordDocumentLoader)
# all_documents.extend(doc_loader.load())

# # Split documents into chunks
# text_splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200)
# docs = text_splitter.split_documents(all_documents)

# Create embedding function
# vectordb = PineconeVectorStore.from_documents(
#     documents=docs,
#     embedding=embedding,
#     index_name=PINECONE_INDEX_NAME,
#     text_key="text"
# )


# Setup retriever and QA chain
# retriever = vectordb.as_retriever(search_kwargs={"k": 5})
# llm = ChatOpenAI(temperature=0)
# qa_chain = RetrievalQA.from_chain_type(llm=llm, retriever=retriever, return_source_documents=True)


# Register Blueprints
app.register_blueprint(google_bp)
app.register_blueprint(facebook_bp)
app.register_blueprint(agent_bp)
app.register_blueprint(gmail_bp)
app.register_blueprint(session_bp)
app.register_blueprint(microsoft_bp)
app.register_blueprint(twilio_bp)
app.register_blueprint(users_bp)
app.register_blueprint(contacts_bp)
app.register_blueprint(playbook_bp)
app.register_blueprint(zoho_bp)
app.register_blueprint(credits_bp)
app.register_blueprint(umail_bp)





if __name__ == "__main__":
    os.makedirs(app.config["SESSION_FILE_DIR"], exist_ok=True)
    app.run(host="0.0.0.0", port=3000, debug=True)
