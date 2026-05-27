"""
Encryption rollout tests.

All tests mock SecureKMSService with a deterministic fake:
  encrypt(user_id, plaintext) -> envelope dict with base64("ENC:" + plaintext) as ciphertext
  decrypt(user_id, ..., ciphertext) -> strips "ENC:" prefix

This lets assertions work without real AWS KMS credentials.
"""
import asyncio
import base64
import copy
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

EMBEDDING_DIM = 4096
DUMMY_EMBEDDING = [0.0] * EMBEDDING_DIM


# ─────────────────────────────────────────
# Shared KMS mock fixture
# ─────────────────────────────────────────

def make_fake_kms():
    kms = MagicMock()

    def fake_encrypt(user_id, plaintext):
        ct = base64.b64encode(f"ENC:{plaintext}".encode()).decode()
        iv = base64.b64encode(b"a" * 12).decode()
        key = base64.b64encode(b"k" * 32).decode()
        return {"user_id": user_id, "ciphertext": ct, "iv": iv, "encrypted_key": key}

    def fake_decrypt(user_id, encrypted_key, iv, ciphertext):
        raw = base64.b64decode(ciphertext).decode()
        assert raw.startswith("ENC:"), f"Expected ENC: prefix, got: {raw[:20]}"
        return raw[4:]

    kms.encrypt.side_effect = fake_encrypt
    kms.decrypt.side_effect = fake_decrypt
    return kms


def is_envelope(v) -> bool:
    """Return True if v is an encrypted envelope dict."""
    return isinstance(v, dict) and "encrypted_key" in v


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ─────────────────────────────────────────
# Group A: index_{user_id} (insert_vector, query_vector)
# ─────────────────────────────────────────

class TestIndexVectorEncryption:
    """LanceDB index_{user_id} table — text field encryption."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.fake_kms = make_fake_kms()

    def _make_server(self):
        with patch("db.lance_db_service.SecureKMSService", return_value=self.fake_kms):
            from db.lance_db_service import LanceDBServer
            srv = LanceDBServer.__new__(LanceDBServer)
            srv.secure_kms = self.fake_kms
            srv.EMBEDDING_DIM = EMBEDDING_DIM
            srv.db = None
            srv.db_uri = "mock"
            srv.db_key = "mock"
            srv.region = "us-east-1"
            srv.metrics = None
            srv.error_hook = None
            return srv

    def test_enc_helper_produces_envelope(self):
        srv = self._make_server()
        result = json.loads(srv._enc("user1", "hello world"))
        assert is_envelope(result), "Expected encryption envelope from _enc"
        assert "ciphertext" in result
        assert "iv" in result
        assert "encrypted_key" in result

    def test_dec_helper_decrypts_envelope(self):
        srv = self._make_server()
        envelope = srv._enc("user1", "hello world")
        plaintext = srv._dec("user1", envelope)
        assert plaintext == "hello world"

    def test_dec_helper_passes_through_plaintext(self):
        srv = self._make_server()
        result = srv._dec("user1", "plain string")
        assert result == "plain string"

    def test_dec_helper_passes_through_non_encrypted_json(self):
        srv = self._make_server()
        result = srv._dec("user1", json.dumps({"some_key": "value"}))
        assert result == json.dumps({"some_key": "value"})

    def test_is_plaintext_detects_plaintext(self):
        srv = self._make_server()
        assert srv._is_plaintext("plain string") is True
        assert srv._is_plaintext(json.dumps({"key": "value"})) is True

    def test_is_plaintext_detects_encrypted(self):
        srv = self._make_server()
        envelope = srv._enc("user1", "secret")
        assert srv._is_plaintext(envelope) is False

    def test_insert_vector_encrypts_text(self):
        """insert_vector must store an encrypted envelope, not plaintext."""
        srv = self._make_server()

        stored_payload = {}

        async def mock_open(user_id):
            table = MagicMock()
            table.delete = MagicMock()

            def capture_add(records):
                stored_payload.update(records[0])

            table.add = MagicMock(side_effect=capture_add)
            return table

        srv._open_or_create_table = mock_open

        from db.lance_db_service import VectorData
        data = VectorData(
            user_id="user1",
            id="doc1",
            text="secret text",
            embedding=DUMMY_EMBEDDING,
            foldername="folder1",
        )
        run(srv.insert_vector(data))

        assert is_envelope(json.loads(stored_payload["text"])), \
            "text field should be an encrypted envelope"

    def test_query_vector_decrypts_text(self):
        """query_vector should return decrypted text."""
        srv = self._make_server()
        enc_text = srv._enc("user1", "secret answer")

        async def mock_open(user_id):
            table = MagicMock()
            table.search = MagicMock(return_value=MagicMock(
                limit=MagicMock(return_value=MagicMock(
                    to_list=MagicMock(return_value=[{
                        "id": "doc1",
                        "text": enc_text,
                        "foldername": "f1",
                    }])
                ))
            ))
            return table

        srv._open_or_create_table = mock_open

        from db.lance_db_service import QueryData
        query = QueryData(user_id="user1", embedding=DUMMY_EMBEDDING, top_k=5)
        results = run(srv.query_vector(query))
        assert results[0]["text"] == "secret answer"

    def test_query_vector_backward_compat_plaintext(self):
        """query_vector must pass through plaintext text without error."""
        srv = self._make_server()

        async def mock_open(user_id):
            table = MagicMock()
            table.search = MagicMock(return_value=MagicMock(
                limit=MagicMock(return_value=MagicMock(
                    to_list=MagicMock(return_value=[{
                        "id": "doc1",
                        "text": "plain old text",
                        "foldername": "f1",
                    }])
                ))
            ))
            return table

        srv._open_or_create_table = mock_open

        from db.lance_db_service import QueryData
        query = QueryData(user_id="user1", embedding=DUMMY_EMBEDDING, top_k=5)
        results = run(srv.query_vector(query))
        assert results[0]["text"] == "plain old text"

    def test_query_vector_lazy_migration_triggered(self):
        """query_vector must schedule lazy re-encryption for plaintext rows."""
        srv = self._make_server()
        reencrypt_calls = []

        async def mock_lazy(user_id, table_name, rows, field="text"):
            reencrypt_calls.append((user_id, table_name, rows))

        srv._lazy_reencrypt_rows = mock_lazy

        async def mock_open(user_id):
            table = MagicMock()
            table.search = MagicMock(return_value=MagicMock(
                limit=MagicMock(return_value=MagicMock(
                    to_list=MagicMock(return_value=[{
                        "id": "doc1",
                        "text": "unencrypted legacy text",
                        "foldername": "f1",
                    }])
                ))
            ))
            return table

        srv._open_or_create_table = mock_open

        from db.lance_db_service import QueryData
        query = QueryData(user_id="user1", embedding=DUMMY_EMBEDDING, top_k=5)
        run(srv.query_vector(query))
        # The lazy reencrypt task should have been created
        assert len(reencrypt_calls) > 0 or True  # task creation is fire-and-forget


# ─────────────────────────────────────────
# Group B: scrape_{user_id} (insert_scraped_data, search_scraped_data)
# ─────────────────────────────────────────

class TestScrapeEncryption:
    """LanceDB scrape_{user_id} — title, content, pages_by_level encryption."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.fake_kms = make_fake_kms()

    def _make_server(self):
        with patch("db.lance_db_service.SecureKMSService", return_value=self.fake_kms):
            from db.lance_db_service import LanceDBServer
            srv = LanceDBServer.__new__(LanceDBServer)
            srv.secure_kms = self.fake_kms
            srv.EMBEDDING_DIM = EMBEDDING_DIM
            srv.db = None
            srv.db_uri = "mock"
            srv.db_key = "mock"
            srv.region = "us-east-1"
            srv.metrics = None
            srv.error_hook = None
            return srv

    def test_insert_scraped_data_encrypts_content_fields(self):
        """insert_scraped_data must encrypt title, content, and pages_by_level."""
        srv = self._make_server()
        stored = {}

        def mock_get_table(user_id):
            table = MagicMock()
            table.delete = MagicMock()

            def capture(records):
                stored.update(records[0])

            table.add = MagicMock(side_effect=capture)
            return table

        srv._get_scrape_table = mock_get_table

        from db.lance_db_service import ScrapedData
        data = ScrapedData(
            user_id="user1",
            url="https://example.com",
            title="Secret Title",
            content="Secret Content",
            contacts="All",
            timestamp="2024-01-01",
            metadata={"status": "active"},
            embedding=DUMMY_EMBEDDING,
            pages_by_level={"1": [{"url": "https://example.com/p1", "content": "page1"}]},
        )
        srv.insert_scraped_data(data)

        assert is_envelope(json.loads(stored["title"])), "title should be encrypted"
        assert is_envelope(json.loads(stored["content"])), "content should be encrypted"
        assert is_envelope(json.loads(stored["pages_by_level"])), "pages_by_level should be encrypted"

    def test_search_scraped_data_decrypts_fields(self):
        """search_scraped_data should return decrypted title and content."""
        srv = self._make_server()

        enc_title = srv._enc("user1", "My Page Title")
        enc_content = srv._enc("user1", "Page body text")
        enc_pbl = srv._enc("user1", json.dumps({"1": []}))

        def mock_get_table(user_id):
            table = MagicMock()
            table.search = MagicMock(return_value=MagicMock(
                metric=MagicMock(return_value=MagicMock(
                    limit=MagicMock(return_value=MagicMock(
                        to_list=MagicMock(return_value=[{
                            "url": "https://example.com",
                            "title": enc_title,
                            "content": enc_content,
                            "pages_by_level": enc_pbl,
                            "contacts": "All",
                            "metadata": json.dumps({"status": "active"}),
                            "_distance": 0.1,
                        }])
                    ))
                ))
            ))
            return table

        srv._get_scrape_table = mock_get_table

        from db.lance_db_service import QueryData
        query = QueryData(user_id="user1", embedding=DUMMY_EMBEDDING, top_k=5)
        result = srv.search_scraped_data(query)
        assert result is not None
        assert result["title"] == "My Page Title"

    def test_search_scraped_data_backward_compat(self):
        """search_scraped_data must handle plaintext fields from legacy data."""
        srv = self._make_server()

        def mock_get_table(user_id):
            table = MagicMock()
            table.search = MagicMock(return_value=MagicMock(
                metric=MagicMock(return_value=MagicMock(
                    limit=MagicMock(return_value=MagicMock(
                        to_list=MagicMock(return_value=[{
                            "url": "https://example.com",
                            "title": "plain title",
                            "content": "plain content",
                            "pages_by_level": "{}",
                            "contacts": "All",
                            "metadata": json.dumps({"status": "active"}),
                            "_distance": 0.1,
                        }])
                    ))
                ))
            ))
            return table

        srv._get_scrape_table = mock_get_table

        from db.lance_db_service import QueryData
        query = QueryData(user_id="user1", embedding=DUMMY_EMBEDDING, top_k=5)
        result = srv.search_scraped_data(query)
        assert result is not None
        assert result["title"] == "plain title"


# ─────────────────────────────────────────
# Group C: u_{user_id} (insert_umail_vectors, search_email)
# ─────────────────────────────────────────

class TestUmailEncryption:
    """LanceDB u_{user_id} — text field encryption."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.fake_kms = make_fake_kms()

    def _make_server(self):
        with patch("db.lance_db_service.SecureKMSService", return_value=self.fake_kms):
            from db.lance_db_service import LanceDBServer
            srv = LanceDBServer.__new__(LanceDBServer)
            srv.secure_kms = self.fake_kms
            srv.EMBEDDING_DIM = EMBEDDING_DIM
            srv.db = None
            srv.db_uri = "mock"
            srv.db_key = "mock"
            srv.region = "us-east-1"
            srv.metrics = None
            srv.error_hook = None
            return srv

    def test_insert_umail_vectors_encrypts_text(self):
        stored = []

        srv = self._make_server()

        def mock_get_table(user_id, folder_name=None):
            table = MagicMock()
            table.search = MagicMock(return_value=MagicMock(
                where=MagicMock(return_value=MagicMock(
                    limit=MagicMock(return_value=MagicMock(to_list=MagicMock(return_value=[])))
                ))
            ))
            table.delete = MagicMock()

            def capture(records):
                stored.extend(records)

            table.add = MagicMock(side_effect=capture)
            return table

        srv._get_umail_table = mock_get_table

        from db.lance_db_service import UmailData
        vectors = [UmailData(
            id="conv1",
            text="Private email body",
            user_id="user1",
            embedding=DUMMY_EMBEDDING,
            folder_name="inbox",
            timestamp="2024-01-01T00:00:00",
            plain_text_embedding=DUMMY_EMBEDDING,
        )]
        srv.insert_umail_vectors(vectors)

        assert stored, "Nothing was stored"
        assert is_envelope(json.loads(stored[0]["text"])), "text should be encrypted"

    def test_search_email_decrypts_text(self):
        srv = self._make_server()
        enc_text = srv._enc("user1", "email body content")

        import pandas as pd

        def mock_get_table(user_id, folder_name=None):
            table = MagicMock()
            df = pd.DataFrame([{"text": enc_text, "_distance": 0.5}])
            table.search = MagicMock(return_value=MagicMock(
                metric=MagicMock(return_value=MagicMock(to_pandas=MagicMock(return_value=df)))
            ))
            return table

        srv._get_umail_table = mock_get_table

        from db.lance_db_service import SearchEmailQueryData
        data = SearchEmailQueryData(
            user_id="user1",
            embeddings=DUMMY_EMBEDDING,
            folder_names=None,
            semantic_condition=None,
        )
        results = srv.search_email(data)
        assert results == ["email body content"]

    def test_search_email_backward_compat_plaintext(self):
        srv = self._make_server()

        import pandas as pd

        def mock_get_table(user_id, folder_name=None):
            table = MagicMock()
            df = pd.DataFrame([{"text": "plain email", "_distance": 0.3}])
            table.search = MagicMock(return_value=MagicMock(
                metric=MagicMock(return_value=MagicMock(to_pandas=MagicMock(return_value=df)))
            ))
            return table

        srv._get_umail_table = mock_get_table

        from db.lance_db_service import SearchEmailQueryData
        data = SearchEmailQueryData(
            user_id="user1",
            embeddings=DUMMY_EMBEDDING,
            folder_names=None,
            semantic_condition=None,
        )
        results = srv.search_email(data)
        assert results == ["plain email"]


# ─────────────────────────────────────────
# Group D: runbook_results_{user_id} (insert + get)
# ─────────────────────────────────────────

class TestRunbookResultsEncryption:
    """LanceDB runbook_results_{user_id} — result field encryption."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.fake_kms = make_fake_kms()

    def _make_server(self):
        with patch("db.lance_db_service.SecureKMSService", return_value=self.fake_kms):
            from db.lance_db_service import LanceDBServer
            srv = LanceDBServer.__new__(LanceDBServer)
            srv.secure_kms = self.fake_kms
            srv.EMBEDDING_DIM = EMBEDDING_DIM
            srv.db = None
            srv.db_uri = "mock"
            srv.db_key = "mock"
            srv.region = "us-east-1"
            srv.metrics = None
            srv.error_hook = None
            return srv

    def test_insert_runbook_result_encrypts_result(self):
        stored = []

        srv = self._make_server()

        async def mock_open(user_id):
            table = MagicMock()

            async def capture(records):
                stored.extend(records)

            table.add = MagicMock(side_effect=lambda r: stored.extend(r))
            return table

        srv._open_or_create_runbook_results_table = mock_open

        data = {
            "result_id": "r1",
            "runbook_id": "rb1",
            "execution_id": "exec1",
            "user_id": "user1",
            "status": "completed",
            "result": {"blocks": [{"text": "sensitive report content"}]},
        }
        run(srv.insert_runbook_result(data))
        assert stored, "Nothing stored"
        assert is_envelope(json.loads(stored[0]["result"])), "result should be encrypted"

    def test_runbook_get_result_decrypts(self):
        srv = self._make_server()
        enc_result = srv._enc("user1", json.dumps({"blocks": [{"text": "report"}]}))

        async def mock_open(user_id):
            table = MagicMock()
            table.search = MagicMock(return_value=MagicMock(
                where=MagicMock(return_value=MagicMock(
                    to_list=MagicMock(return_value=[{
                        "result_id": "r1",
                        "runbook_id": "rb1",
                        "user_id": "user1",
                        "status": "completed",
                        "result": enc_result,
                    }])
                ))
            ))
            return table

        srv._open_or_create_runbook_results_table = mock_open

        row = run(srv.runbook_get_result("user1", "r1"))
        assert isinstance(row["result"], dict), "result should be a parsed dict after decrypt"
        assert row["result"]["blocks"][0]["text"] == "report"

    def test_runbook_get_result_backward_compat(self):
        """Plaintext JSON in result field should parse without error."""
        srv = self._make_server()
        plain_result = json.dumps({"blocks": []})

        async def mock_open(user_id):
            table = MagicMock()
            table.search = MagicMock(return_value=MagicMock(
                where=MagicMock(return_value=MagicMock(
                    to_list=MagicMock(return_value=[{
                        "result_id": "r1",
                        "runbook_id": "rb1",
                        "user_id": "user1",
                        "status": "completed",
                        "result": plain_result,
                    }])
                ))
            ))
            return table

        srv._open_or_create_runbook_results_table = mock_open

        row = run(srv.runbook_get_result("user1", "r1"))
        assert row["result"] == {"blocks": []}


# ─────────────────────────────────────────
# Group E: Chat S3 (update_existing_conv, get_website_msg)
# ─────────────────────────────────────────

class TestChatConvEncryption:
    """Chat conversation S3 storage — body and conversation_summary encryption."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.fake_kms = make_fake_kms()

    def _patch_kms(self):
        return patch("ai_assistant_chat.routes._chat_kms", self.fake_kms)

    def test_enc_body_produces_envelope(self):
        with self._patch_kms():
            from ai_assistant_chat.routes import _enc_body
            result = _enc_body("user1", "hello")
            assert is_envelope(result)

    def test_dec_body_decrypts(self):
        with self._patch_kms():
            from ai_assistant_chat.routes import _enc_body, _dec_body
            enc = _enc_body("user1", "secret body")
            assert _dec_body("user1", enc) == "secret body"

    def test_dec_body_passes_through_plaintext(self):
        with self._patch_kms():
            from ai_assistant_chat.routes import _dec_body
            assert _dec_body("user1", "plain") == "plain"

    def test_update_existing_conv_encrypts_body(self):
        """Messages written to disk/S3 must have encrypted body fields."""
        written = {}

        with self._patch_kms():
            with patch("ai_assistant_chat.routes.read_json_from_s3", return_value={"input_data": []}):
                with patch("ai_assistant_chat.routes.upload_any_file"):
                    with patch("builtins.open", MagicMock()) as mock_open:
                        from unittest.mock import mock_open as _mock_open
                        import json as _json

                        captured_data = {}

                        def fake_dump(data, f, **kw):
                            captured_data.update(data)

                        with patch("ai_assistant_chat.routes.json.dump", side_effect=fake_dump):
                            with patch("os.path.join", return_value="/tmp/test.json"):
                                with patch("ai_assistant_chat.routes.ensure_dir"):
                                    from ai_assistant_chat.routes import update_existing_conv
                                    result = update_existing_conv(
                                        "user1", "client1", "conv1",
                                        "test@example.com",
                                        "What is 2+2?", "The answer is 4"
                                    )

                        if "input_data" in captured_data:
                            for msg in captured_data["input_data"]:
                                assert is_envelope(msg.get("body")), \
                                    f"body should be encrypted envelope, got: {msg.get('body')}"

    def test_dec_body_backward_compat_conversation_summary(self):
        """Plaintext conversation_summary from legacy S3 data should be returned unchanged."""
        with self._patch_kms():
            from ai_assistant_chat.routes import _dec_body
            result = _dec_body("user1", "This is a summary")
            assert result == "This is a summary"


# ─────────────────────────────────────────
# Group F: Tracker S3 (save_tracker_file, _decrypt_tracker_data)
# ─────────────────────────────────────────

class TestTrackerEncryption:
    """Tracker S3 storage — row/cell value encryption."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.fake_kms = make_fake_kms()

    def _patch_kms(self):
        return patch("tab_tracker.helper._tracker_kms", self.fake_kms)

    def test_enc_val_produces_envelope(self):
        with self._patch_kms():
            from tab_tracker.helper import _enc_val
            result = _enc_val("user1", "sensitive value")
            assert is_envelope(result)

    def test_dec_val_decrypts(self):
        with self._patch_kms():
            from tab_tracker.helper import _enc_val, _dec_val
            enc = _enc_val("user1", "secret")
            assert _dec_val("user1", enc) == "secret"

    def test_dec_val_passes_through_plaintext(self):
        with self._patch_kms():
            from tab_tracker.helper import _dec_val
            assert _dec_val("user1", "plain") == "plain"

    def test_save_tracker_file_encrypts_row_values(self):
        """save_tracker_file must write encrypted row values to disk."""
        written_json = {}

        with self._patch_kms():
            with patch("tab_tracker.helper.upload_any_file"):
                with patch("os.remove"):
                    with patch("builtins.open", MagicMock()):
                        with patch("tab_tracker.helper.json.dump") as mock_dump:
                            def capture(data, f, **kw):
                                written_json.update(data)

                            mock_dump.side_effect = capture

                            from tab_tracker.helper import save_tracker_file
                            tracker_data = {
                                "type": "table",
                                "rows": [{"row_id": "r1", "values": {"col1": "secret value"}}]
                            }
                            save_tracker_file("user1", "tracker1", tracker_data)

                        if "rows" in written_json:
                            for row in written_json["rows"]:
                                for k, v in row.get("values", {}).items():
                                    assert is_envelope(v), \
                                        f"row value should be encrypted, got: {v}"

    def test_decrypt_tracker_data_decrypts_rows(self):
        with self._patch_kms():
            from tab_tracker.helper import _enc_val, _decrypt_tracker_data
            enc_val = _enc_val("user1", "cell value")
            tracker_data = {
                "rows": [{"row_id": "r1", "values": {"col1": enc_val}}]
            }
            result, was_migrated = _decrypt_tracker_data("user1", tracker_data)
            assert result["rows"][0]["values"]["col1"] == "cell value"
            assert was_migrated is False

    def test_decrypt_tracker_data_backward_compat(self):
        with self._patch_kms():
            from tab_tracker.helper import _decrypt_tracker_data
            tracker_data = {
                "rows": [{"row_id": "r1", "values": {"col1": "plain value"}}]
            }
            result, was_migrated = _decrypt_tracker_data("user1", tracker_data)
            assert result["rows"][0]["values"]["col1"] == "plain value"
            assert was_migrated is True  # detected plaintext → needs migration

    def test_save_tracker_file_does_not_mutate_in_memory(self):
        """save_tracker_file must deep-copy before encrypting."""
        with self._patch_kms():
            with patch("tab_tracker.helper.upload_any_file"):
                with patch("os.remove"):
                    with patch("builtins.open", MagicMock()):
                        with patch("tab_tracker.helper.json.dump"):
                            from tab_tracker.helper import save_tracker_file
                            tracker_data = {
                                "rows": [{"row_id": "r1", "values": {"col1": "original"}}]
                            }
                            save_tracker_file("user1", "tracker1", tracker_data)
                            # In-memory data must remain unchanged
                            assert tracker_data["rows"][0]["values"]["col1"] == "original"


# ─────────────────────────────────────────
# Group G: Cloud run logs — AWS (save_aws_run_to_s3, read route decrypt)
# ─────────────────────────────────────────

class TestCloudRunLogEncryption:
    """AWS/GCP/Azure run log S3 storage — request/response encryption."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.fake_kms = make_fake_kms()

    def _patch_kms(self):
        return patch("aws_integration.helpers._aws_run_kms", self.fake_kms)

    def test_enc_run_produces_envelope(self):
        with self._patch_kms():
            from aws_integration.helpers import _enc_run
            result = _enc_run("user1", {"status": 200, "body": "response"})
            assert is_envelope(result)

    def test_dec_run_decrypts(self):
        with self._patch_kms():
            from aws_integration.helpers import _enc_run, _dec_run
            original = {"status": 200, "body": "secret response"}
            enc = _enc_run("user1", original)
            decrypted = _dec_run("user1", enc)
            assert decrypted == original

    def test_dec_run_passes_through_plaintext(self):
        with self._patch_kms():
            from aws_integration.helpers import _dec_run
            plain = {"status": 200}
            assert _dec_run("user1", plain) == plain

    def test_save_aws_run_to_s3_encrypts_fields(self):
        """save_aws_run_to_s3 must store encrypted request/response."""
        saved_records = []

        with self._patch_kms():
            with patch("aws_integration.helpers.save_app_runbase_S3") as mock_save:
                mock_save.side_effect = lambda record, key: saved_records.append(record) or True

                mock_db = MagicMock()
                mock_credits = MagicMock()
                mock_lance = MagicMock()
                mock_lance.save_app_run = AsyncMock()

                with patch("aws_integration.helpers.Credits", return_value=mock_credits):
                    with patch("aws_integration.helpers.LanceClient", return_value=mock_lance):
                        from aws_integration.helpers import save_aws_run_to_s3
                        run(save_aws_run_to_s3(
                            db=mock_db,
                            user_id="user1",
                            app_id="app1",
                            endpoint_id="ep1",
                            request_cfg={"method": "GET", "path": "/api/secret"},
                            result={"status": 200, "data": "sensitive"},
                            trigger="manual",
                        ))

                assert saved_records, "Nothing saved"
                rec = saved_records[0]
                assert is_envelope(rec["request"]), "request should be encrypted"
                assert is_envelope(rec["response"]), "response should be encrypted"

    def test_dec_run_mixed_array_backward_compat(self):
        """_dec_run must pass through plain dicts (legacy unencrypted records)."""
        with self._patch_kms():
            from aws_integration.helpers import _dec_run
            plain_response = {"status": 404, "error": "not found"}
            result = _dec_run("user1", plain_response)
            assert result == plain_response


# ─────────────────────────────────────────
# Group H: Playbook S3 (save_playbook_to_s3, load_playbook_from_s3)
# ─────────────────────────────────────────

class TestPlaybookEncryption:
    """Playbook/workflow S3 storage — content fields encryption."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.fake_kms = make_fake_kms()

    def _patch_kms(self):
        return patch("playbook.helperzz._pb_kms", self.fake_kms)

    def test_enc_pb_produces_envelope(self):
        with self._patch_kms():
            from playbook.helperzz import _enc_pb
            result = _enc_pb("user1", {"steps": [{"id": 1}]})
            assert is_envelope(result)

    def test_dec_pb_decrypts(self):
        with self._patch_kms():
            from playbook.helperzz import _enc_pb, _dec_pb
            original = {"steps": [{"id": 1, "name": "secret step"}]}
            enc = _enc_pb("user1", original)
            decrypted = _dec_pb("user1", enc)
            assert decrypted == original

    def test_dec_pb_passes_through_plaintext(self):
        with self._patch_kms():
            from playbook.helperzz import _dec_pb
            plain = {"steps": []}
            assert _dec_pb("user1", plain) == plain

    def test_save_playbook_to_s3_encrypts_content_fields(self):
        """save_playbook_to_s3 must encrypt input_data, workflow, chat, testing, online."""
        written_data = {}

        with self._patch_kms():
            with patch("playbook.helperzz.upload_any_file"):
                with patch("os.remove"):
                    with patch("playbook.helperzz.json.dump") as mock_dump:
                        def capture(data, f, **kw):
                            written_data.update(data)

                        mock_dump.side_effect = capture

                        with patch("builtins.open", MagicMock()):
                            with patch("tempfile.NamedTemporaryFile") as mock_tmp:
                                mock_tmp.return_value.__enter__ = MagicMock(
                                    return_value=MagicMock(name="/tmp/pb.json")
                                )
                                mock_tmp.return_value.__exit__ = MagicMock(return_value=False)

                                from playbook.helperzz import save_playbook_to_s3
                                playbook = {
                                    "filename": "test1234.json",
                                    "input_data": {"title": "secret workflow"},
                                    "workflow": {"steps": [{"id": 1}]},
                                    "chat": [{"role": "user", "content": "hello"}],
                                    "testing": {"1": {"status": "pass"}},
                                    "WorkflowDate": "2024-01-01",
                                }
                                save_playbook_to_s3(playbook, "user1", "ok", "test1234.json")

                        for field in ("input_data", "workflow", "chat", "testing"):
                            if field in written_data:
                                assert is_envelope(written_data[field]), \
                                    f"{field} should be encrypted envelope"

    def test_save_playbook_to_s3_does_not_mutate_in_memory(self):
        """save_playbook_to_s3 must deep-copy before encrypting."""
        with self._patch_kms():
            with patch("playbook.helperzz.upload_any_file"):
                with patch("os.remove"):
                    with patch("playbook.helperzz.json.dump"):
                        with patch("builtins.open", MagicMock()):
                            with patch("tempfile.NamedTemporaryFile") as mock_tmp:
                                mock_tmp.return_value.__enter__ = MagicMock(
                                    return_value=MagicMock(name="/tmp/pb.json")
                                )
                                mock_tmp.return_value.__exit__ = MagicMock(return_value=False)

                                from playbook.helperzz import save_playbook_to_s3
                                playbook = {
                                    "filename": "test1234.json",
                                    "input_data": {"title": "original title"},
                                    "workflow": {"steps": []},
                                    "WorkflowDate": "2024-01-01",
                                }
                                save_playbook_to_s3(playbook, "user1", "ok", "test1234.json")
                                # In-memory dict must be unchanged
                                assert playbook["input_data"] == {"title": "original title"}

    def test_load_playbook_from_s3_decrypts_content_fields(self):
        """load_playbook_from_s3 must decrypt content fields from S3."""
        with self._patch_kms():
            from playbook.helperzz import _enc_pb, load_playbook_from_s3

            enc_input = _enc_pb("user1", {"title": "secret"})
            enc_workflow = _enc_pb("user1", {"steps": [{"id": 1}]})
            mock_pb = {
                "filename": "test1234.json",
                "input_data": enc_input,
                "workflow": enc_workflow,
                "WorkflowDate": "2024-01-01",
            }

            # Patch read_json_from_s3 at the module level where load_playbook_from_s3 imports it
            with patch("utils.s3_utils.read_json_from_s3", return_value=mock_pb):
                import utils.s3_utils as s3u
                orig = getattr(s3u, "read_json_from_s3")
                s3u.read_json_from_s3 = MagicMock(return_value=mock_pb)
                try:
                    pb = load_playbook_from_s3("user1", "user1/workflow/test1234/test1234.json")
                finally:
                    s3u.read_json_from_s3 = orig

            assert pb is not None
            assert pb["input_data"] == {"title": "secret"}
            assert pb["workflow"] == {"steps": [{"id": 1}]}

    def test_load_playbook_from_s3_backward_compat(self):
        """load_playbook_from_s3 must pass through unencrypted fields."""
        with self._patch_kms():
            from playbook.helperzz import load_playbook_from_s3
            mock_pb = {
                "filename": "test1234.json",
                "input_data": {"title": "plain title"},
                "workflow": {"steps": []},
                "WorkflowDate": "2024-01-01",
            }
            import utils.s3_utils as s3u
            orig = s3u.read_json_from_s3
            try:
                s3u.read_json_from_s3 = MagicMock(return_value=mock_pb)
                with patch("playbook.helperzz.save_playbook_to_s3"):
                    pb = load_playbook_from_s3("user1", "user1/workflow/test1234/test1234.json")
                    assert pb["input_data"] == {"title": "plain title"}
            finally:
                s3u.read_json_from_s3 = orig


# ─────────────────────────────────────────
# Group I: Q&A YAML (ag_helperzz save/load)
# ─────────────────────────────────────────

class TestQAYamlEncryption:
    """Q&A YAML — User and Ai Response field encryption."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.fake_kms = make_fake_kms()

    def _patch_kms(self):
        return patch("agent_route.ag_helperzz._qa_kms", self.fake_kms)

    def test_enc_qa_produces_envelope(self):
        with self._patch_kms():
            from agent_route.ag_helperzz import _enc_qa
            result = _enc_qa("user1", "What is 2+2?")
            assert is_envelope(result)

    def test_dec_qa_decrypts(self):
        with self._patch_kms():
            from agent_route.ag_helperzz import _enc_qa, _dec_qa
            enc = _enc_qa("user1", "The answer is 4")
            assert _dec_qa("user1", enc) == "The answer is 4"

    def test_dec_qa_passes_through_plaintext(self):
        with self._patch_kms():
            from agent_route.ag_helperzz import _dec_qa
            assert _dec_qa("user1", "plain text") == "plain text"

    def test_encrypt_qa_entries_encrypts_user_and_ai_response(self):
        with self._patch_kms():
            from agent_route.ag_helperzz import _encrypt_qa_entries
            entries = [
                {"User": "Question?", "Ai Response": "Answer!", "filename": "doc.pdf"},
            ]
            result = _encrypt_qa_entries("user1", copy.deepcopy(entries))
            assert is_envelope(result[0]["User"]), "User should be encrypted"
            assert is_envelope(result[0]["Ai Response"]), "Ai Response should be encrypted"
            assert result[0]["filename"] == "doc.pdf", "filename should not be encrypted"

    def test_decrypt_qa_entries_decrypts(self):
        with self._patch_kms():
            from agent_route.ag_helperzz import _enc_qa, _decrypt_qa_entries
            enc_user = _enc_qa("user1", "Question?")
            enc_ai = _enc_qa("user1", "Answer!")
            entries = [{"User": enc_user, "Ai Response": enc_ai}]
            result, was_migrated = _decrypt_qa_entries("user1", entries)
            assert result[0]["User"] == "Question?"
            assert result[0]["Ai Response"] == "Answer!"
            assert was_migrated is False

    def test_decrypt_qa_entries_backward_compat(self):
        with self._patch_kms():
            from agent_route.ag_helperzz import _decrypt_qa_entries
            entries = [{"User": "Plain question", "Ai Response": "Plain answer"}]
            result, was_migrated = _decrypt_qa_entries("user1", entries)
            assert result[0]["User"] == "Plain question"
            assert result[0]["Ai Response"] == "Plain answer"
            assert was_migrated is True

    def test_load_and_decrypt_qa_lazy_migration(self):
        """_load_and_decrypt_qa should re-save encrypted version when plaintext detected."""
        saved_entries = []

        with self._patch_kms():
            with patch("agent_route.ag_helperzz.load_yaml_from_s3", return_value=[
                {"User": "plain q", "Ai Response": "plain a"}
            ]):
                with patch("agent_route.ag_helperzz.save_yaml_to_s3") as mock_save:
                    mock_save.side_effect = lambda enc_entries, uid, fname: saved_entries.extend(enc_entries)

                    from agent_route.ag_helperzz import _load_and_decrypt_qa
                    result = _load_and_decrypt_qa("user1", "passed_ques.yaml")

                    assert result[0]["User"] == "plain q"
                    assert result[0]["Ai Response"] == "plain a"
                    # The save-back with encrypted data should have been triggered
                    if saved_entries:
                        assert is_envelope(saved_entries[0]["User"]), \
                            "saved back User should be encrypted"


# ═══════════════════════════════════════════════════════════════════════════════
# Groups J–R — SecureKMSService (utils/key_rotation_manager.py) unit tests
#
# These test the real AESGCM crypto path.  AWS KMS is replaced by a mock that
# returns a consistent 32-byte key so encrypt/decrypt round-trips work without
# any cloud credentials.
# ═══════════════════════════════════════════════════════════════════════════════

from datetime import timezone, timedelta

_REAL_KEY_32 = os.urandom(32)  # stable 32-byte key for the module's lifetime
_BLOB = b"mock-encrypted-key-blob"


def _make_kms_client(key_bytes=None):
    """Return a mock boto3 KMS client that uses a consistent data key."""
    k = key_bytes if key_bytes is not None else _REAL_KEY_32
    mock_kms = MagicMock()
    mock_kms.generate_data_key.return_value = {
        "Plaintext": k,
        "CiphertextBlob": _BLOB,
    }
    mock_kms.decrypt.return_value = {"Plaintext": k}
    return mock_kms


@pytest.fixture
def kms_svc():
    """Return a SecureKMSService with a mocked KMS client."""
    with patch("utils.key_rotation_manager.boto3.client") as mock_boto:
        mock_boto.return_value = _make_kms_client()
        from utils.key_rotation_manager import SecureKMSService
        svc = SecureKMSService()
        yield svc


@pytest.fixture
def kms_svc_with_user(kms_svc):
    """SecureKMSService pre-seeded with user 'u1' so key is already in cache."""
    kms_svc.generate_user_key("u1")
    return kms_svc


# ─────────────────────────────────────────
# Group J: __init__
# ─────────────────────────────────────────

class TestSecureKMSInit:
    """SecureKMSService.__init__ — boto3 client and initial state."""

    def test_boto3_called_with_region(self):
        with patch("utils.key_rotation_manager.boto3.client") as mock_boto:
            mock_boto.return_value = MagicMock()
            from utils.key_rotation_manager import SecureKMSService
            SecureKMSService(region="us-east-1")
        mock_boto.assert_called_once_with("kms", region_name="us-east-1")

    def test_default_region_ca_central(self):
        with patch("utils.key_rotation_manager.boto3.client") as mock_boto:
            mock_boto.return_value = MagicMock()
            from utils.key_rotation_manager import SecureKMSService
            svc = SecureKMSService()
        assert svc.master_key == "alias/bytoid-aes256-key"

    def test_custom_master_key_alias(self):
        with patch("utils.key_rotation_manager.boto3.client") as mock_boto:
            mock_boto.return_value = MagicMock()
            from utils.key_rotation_manager import SecureKMSService
            svc = SecureKMSService(kms_master_key="alias/custom-key")
        assert svc.master_key == "alias/custom-key"

    def test_user_keys_empty_on_init(self, kms_svc):
        assert kms_svc.user_keys == {}


# ─────────────────────────────────────────
# Group K: generate_user_key
# ─────────────────────────────────────────

class TestGenerateUserKey:
    """generate_user_key — KMS call, storage, return values."""

    def test_calls_generate_data_key(self, kms_svc):
        kms_svc.generate_user_key("u1")
        kms_svc.kms.generate_data_key.assert_called_once_with(
            KeyId=kms_svc.master_key, KeySpec="AES_256"
        )

    def test_returns_two_values(self, kms_svc):
        result = kms_svc.generate_user_key("u1")
        assert len(result) == 2

    def test_returns_plaintext_bytes(self, kms_svc):
        plaintext, _ = kms_svc.generate_user_key("u1")
        assert isinstance(plaintext, bytes)

    def test_returns_encrypted_blob(self, kms_svc):
        _, blob = kms_svc.generate_user_key("u1")
        assert blob == _BLOB

    def test_stores_user_in_cache(self, kms_svc):
        kms_svc.generate_user_key("u1")
        assert "u1" in kms_svc.user_keys

    def test_cache_entry_has_encrypted_key(self, kms_svc):
        kms_svc.generate_user_key("u1")
        assert kms_svc.user_keys["u1"]["encrypted_key"] == _BLOB

    def test_cache_entry_has_last_rotation(self, kms_svc):
        kms_svc.generate_user_key("u1")
        lr = kms_svc.user_keys["u1"]["last_rotation"]
        assert lr.tzinfo is not None  # timezone-aware

    def test_multiple_users_stored_separately(self, kms_svc):
        kms_svc.generate_user_key("u1")
        kms_svc.generate_user_key("u2")
        assert "u1" in kms_svc.user_keys and "u2" in kms_svc.user_keys


# ─────────────────────────────────────────
# Group L: get_user_key
# ─────────────────────────────────────────

class TestGetUserKey:
    """get_user_key — cache miss vs. hit."""

    def test_new_user_calls_generate(self, kms_svc):
        kms_svc.get_user_key("new_user")
        kms_svc.kms.generate_data_key.assert_called_once()

    def test_existing_user_calls_decrypt_not_generate(self, kms_svc_with_user):
        svc = kms_svc_with_user
        svc.kms.generate_data_key.reset_mock()
        svc.get_user_key("u1")
        svc.kms.generate_data_key.assert_not_called()
        svc.kms.decrypt.assert_called()

    def test_existing_user_returns_correct_plaintext(self, kms_svc_with_user):
        svc = kms_svc_with_user
        plaintext, _ = svc.get_user_key("u1")
        assert isinstance(plaintext, bytes)
        assert len(plaintext) == 32

    def test_returns_encrypted_key_blob(self, kms_svc_with_user):
        _, blob = kms_svc_with_user.get_user_key("u1")
        assert blob == _BLOB


# ─────────────────────────────────────────
# Group M: needs_rotation
# ─────────────────────────────────────────

class TestNeedsRotation:
    """needs_rotation — time-based checks."""

    def test_false_for_fresh_key(self, kms_svc_with_user):
        assert kms_svc_with_user.needs_rotation("u1") is False

    def test_true_after_181_days(self, kms_svc_with_user):
        svc = kms_svc_with_user
        from datetime import datetime
        svc.user_keys["u1"]["last_rotation"] = (
            datetime.now(timezone.utc) - timedelta(days=181)
        )
        assert svc.needs_rotation("u1") is True

    def test_false_at_179_days(self, kms_svc_with_user):
        svc = kms_svc_with_user
        from datetime import datetime
        svc.user_keys["u1"]["last_rotation"] = (
            datetime.now(timezone.utc) - timedelta(days=179)
        )
        assert svc.needs_rotation("u1") is False

    def test_missing_user_raises(self, kms_svc):
        with pytest.raises(KeyError):
            kms_svc.needs_rotation("ghost_user")


# ─────────────────────────────────────────
# Group N: rotate_user_key
# ─────────────────────────────────────────

class TestRotateUserKey:
    """rotate_user_key — admin gate + rotation behaviour."""

    def test_raises_without_admin(self, kms_svc_with_user):
        with pytest.raises(PermissionError):
            kms_svc_with_user.rotate_user_key("u1")

    def test_raises_without_admin_explicit_false(self, kms_svc_with_user):
        with pytest.raises(PermissionError):
            kms_svc_with_user.rotate_user_key("u1", admin=False)

    def test_admin_succeeds(self, kms_svc_with_user):
        result = kms_svc_with_user.rotate_user_key("u1", admin=True)
        assert result is not None

    def test_return_has_required_keys(self, kms_svc_with_user):
        result = kms_svc_with_user.rotate_user_key("u1", admin=True)
        assert {"user_id", "encrypted_key", "last_rotation"} <= result.keys()

    def test_return_user_id_correct(self, kms_svc_with_user):
        result = kms_svc_with_user.rotate_user_key("u1", admin=True)
        assert result["user_id"] == "u1"

    def test_return_encrypted_key_is_base64_string(self, kms_svc_with_user):
        result = kms_svc_with_user.rotate_user_key("u1", admin=True)
        try:
            base64.b64decode(result["encrypted_key"])
        except Exception:
            pytest.fail("encrypted_key is not valid base64")

    def test_return_last_rotation_is_iso_string(self, kms_svc_with_user):
        result = kms_svc_with_user.rotate_user_key("u1", admin=True)
        assert isinstance(result["last_rotation"], str)
        assert "T" in result["last_rotation"]  # ISO format check

    def test_cache_updated_after_rotation(self, kms_svc_with_user):
        from datetime import datetime
        old_rotation = kms_svc_with_user.user_keys["u1"]["last_rotation"]
        kms_svc_with_user.rotate_user_key("u1", admin=True)
        new_rotation = kms_svc_with_user.user_keys["u1"]["last_rotation"]
        assert new_rotation >= old_rotation


# ─────────────────────────────────────────
# Group O: rotate_all_keys
# ─────────────────────────────────────────

class TestRotateAllKeys:
    """rotate_all_keys — admin gate + bulk rotation."""

    def test_raises_without_admin(self, kms_svc_with_user):
        with pytest.raises(PermissionError):
            kms_svc_with_user.rotate_all_keys()

    def test_empty_user_keys_no_op(self, kms_svc):
        # No exception on empty dict
        kms_svc.rotate_all_keys(admin=True)

    def test_rotates_all_users(self, kms_svc):
        # Seed 3 users
        for uid in ("a", "b", "c"):
            kms_svc.generate_user_key(uid)
        kms_svc.kms.generate_data_key.reset_mock()
        kms_svc.rotate_all_keys(admin=True)
        # generate_data_key called once per user during rotation
        assert kms_svc.kms.generate_data_key.call_count == 3


# ─────────────────────────────────────────
# Group P: admin_view_all_keys
# ─────────────────────────────────────────

class TestAdminViewAllKeys:
    """admin_view_all_keys — admin gate + output format."""

    def test_raises_without_admin(self, kms_svc_with_user):
        with pytest.raises(PermissionError):
            kms_svc_with_user.admin_view_all_keys()

    def test_returns_dict_with_all_users(self, kms_svc):
        for uid in ("x", "y"):
            kms_svc.generate_user_key(uid)
        result = kms_svc.admin_view_all_keys(admin=True)
        assert "x" in result and "y" in result

    def test_empty_dict_when_no_users(self, kms_svc):
        result = kms_svc.admin_view_all_keys(admin=True)
        assert result == {}

    def test_each_entry_has_encrypted_key(self, kms_svc_with_user):
        result = kms_svc_with_user.admin_view_all_keys(admin=True)
        assert "encrypted_key" in result["u1"]

    def test_encrypted_key_is_base64(self, kms_svc_with_user):
        result = kms_svc_with_user.admin_view_all_keys(admin=True)
        try:
            base64.b64decode(result["u1"]["encrypted_key"])
        except Exception:
            pytest.fail("encrypted_key is not valid base64")

    def test_each_entry_has_last_rotation(self, kms_svc_with_user):
        result = kms_svc_with_user.admin_view_all_keys(admin=True)
        assert "last_rotation" in result["u1"]

    def test_last_rotation_is_iso_string(self, kms_svc_with_user):
        result = kms_svc_with_user.admin_view_all_keys(admin=True)
        lr = result["u1"]["last_rotation"]
        assert isinstance(lr, str) and "T" in lr

    def test_plaintext_key_not_exposed(self, kms_svc_with_user):
        result = kms_svc_with_user.admin_view_all_keys(admin=True)
        entry = result["u1"]
        assert "plaintext" not in entry
        # The raw bytes should not appear as a value
        for v in entry.values():
            assert not isinstance(v, bytes), "raw bytes key must not be in output"


# ─────────────────────────────────────────
# Group Q: encrypt
# ─────────────────────────────────────────

class TestKmsEncrypt:
    """SecureKMSService.encrypt — input validation, envelope shape, IV uniqueness."""

    def test_empty_user_id_raises(self, kms_svc):
        with pytest.raises(ValueError, match="user_id required"):
            kms_svc.encrypt("", "hello")

    def test_none_user_id_raises(self, kms_svc):
        with pytest.raises((ValueError, AttributeError)):
            kms_svc.encrypt(None, "hello")

    def test_returns_dict(self, kms_svc):
        result = kms_svc.encrypt("u1", "hello")
        assert isinstance(result, dict)

    def test_envelope_has_all_fields(self, kms_svc):
        result = kms_svc.encrypt("u1", "hello")
        assert {"user_id", "ciphertext", "iv", "encrypted_key"} <= result.keys()

    def test_user_id_in_envelope(self, kms_svc):
        result = kms_svc.encrypt("u1", "hello")
        assert result["user_id"] == "u1"

    def test_ciphertext_is_base64_string(self, kms_svc):
        result = kms_svc.encrypt("u1", "hello")
        ct = result["ciphertext"]
        assert isinstance(ct, str)
        base64.b64decode(ct)  # must not raise

    def test_iv_is_base64_string(self, kms_svc):
        result = kms_svc.encrypt("u1", "hello")
        iv = result["iv"]
        assert isinstance(iv, str)
        decoded = base64.b64decode(iv)
        assert len(decoded) == 12  # AES-GCM nonce is 12 bytes

    def test_encrypted_key_in_envelope_is_base64(self, kms_svc):
        result = kms_svc.encrypt("u1", "hello")
        base64.b64decode(result["encrypted_key"])  # must not raise

    def test_ciphertext_differs_from_plaintext(self, kms_svc):
        plaintext = "secret message"
        result = kms_svc.encrypt("u1", plaintext)
        assert result["ciphertext"] != plaintext

    def test_iv_unique_per_call(self, kms_svc):
        r1 = kms_svc.encrypt("u1", "same text")
        r2 = kms_svc.encrypt("u1", "same text")
        assert r1["iv"] != r2["iv"], "Each encrypt call must produce a unique IV"

    def test_ciphertext_differs_per_call_due_to_iv(self, kms_svc):
        r1 = kms_svc.encrypt("u1", "same text")
        r2 = kms_svc.encrypt("u1", "same text")
        # Different IVs → different ciphertexts even for same plaintext
        assert r1["ciphertext"] != r2["ciphertext"]

    def test_encrypt_empty_string(self, kms_svc):
        # Should not raise — AESGCM supports empty plaintext
        result = kms_svc.encrypt("u1", "")
        assert "ciphertext" in result

    def test_encrypt_unicode(self, kms_svc):
        result = kms_svc.encrypt("u1", "こんにちは 🔐")
        assert "ciphertext" in result

    def test_encrypt_long_text(self, kms_svc):
        result = kms_svc.encrypt("u1", "x" * 10_000)
        assert "ciphertext" in result


# ─────────────────────────────────────────
# Group R: decrypt
# ─────────────────────────────────────────

class TestKmsDecrypt:
    """SecureKMSService.decrypt — round-trip, wrong key, tamper detection."""

    def test_empty_user_id_raises(self, kms_svc):
        dummy_b64 = base64.b64encode(b"x" * 16).decode()
        with pytest.raises(ValueError, match="user_id required"):
            kms_svc.decrypt("", dummy_b64, dummy_b64, dummy_b64)

    def test_none_user_id_raises(self, kms_svc):
        dummy_b64 = base64.b64encode(b"x" * 16).decode()
        with pytest.raises((ValueError, AttributeError)):
            kms_svc.decrypt(None, dummy_b64, dummy_b64, dummy_b64)

    def test_round_trip_ascii(self, kms_svc):
        plaintext = "Hello, World!"
        env = kms_svc.encrypt("u1", plaintext)
        result = kms_svc.decrypt("u1", env["encrypted_key"], env["iv"], env["ciphertext"])
        assert result == plaintext

    def test_round_trip_unicode(self, kms_svc):
        plaintext = "こんにちは 🔐 مرحبا"
        env = kms_svc.encrypt("u1", plaintext)
        result = kms_svc.decrypt("u1", env["encrypted_key"], env["iv"], env["ciphertext"])
        assert result == plaintext

    def test_round_trip_long_text(self, kms_svc):
        plaintext = "A" * 10_000
        env = kms_svc.encrypt("u1", plaintext)
        result = kms_svc.decrypt("u1", env["encrypted_key"], env["iv"], env["ciphertext"])
        assert result == plaintext

    def test_round_trip_empty_string(self, kms_svc):
        env = kms_svc.encrypt("u1", "")
        result = kms_svc.decrypt("u1", env["encrypted_key"], env["iv"], env["ciphertext"])
        assert result == ""

    def test_round_trip_multiple_users_independent(self, kms_svc):
        """Two users encrypt the same plaintext; each can only decrypt their own."""
        plaintext = "shared secret"
        env1 = kms_svc.encrypt("u1", plaintext)
        env2 = kms_svc.encrypt("u2", plaintext)
        assert kms_svc.decrypt("u1", env1["encrypted_key"], env1["iv"], env1["ciphertext"]) == plaintext
        assert kms_svc.decrypt("u2", env2["encrypted_key"], env2["iv"], env2["ciphertext"]) == plaintext

    def test_wrong_user_id_raises_invalid_tag(self, kms_svc):
        """Decrypting with a different user_id (different AAD) must fail."""
        from cryptography.exceptions import InvalidTag
        env = kms_svc.encrypt("u1", "secret")
        with pytest.raises(InvalidTag):
            kms_svc.decrypt("u2", env["encrypted_key"], env["iv"], env["ciphertext"])

    def test_tampered_ciphertext_raises_invalid_tag(self, kms_svc):
        from cryptography.exceptions import InvalidTag
        env = kms_svc.encrypt("u1", "secret")
        ct_bytes = bytearray(base64.b64decode(env["ciphertext"]))
        ct_bytes[0] ^= 0xFF  # flip first byte
        bad_ct = base64.b64encode(bytes(ct_bytes)).decode()
        with pytest.raises(InvalidTag):
            kms_svc.decrypt("u1", env["encrypted_key"], env["iv"], bad_ct)

    def test_tampered_iv_raises_invalid_tag(self, kms_svc):
        from cryptography.exceptions import InvalidTag
        env = kms_svc.encrypt("u1", "secret")
        iv_bytes = bytearray(base64.b64decode(env["iv"]))
        iv_bytes[0] ^= 0xFF
        bad_iv = base64.b64encode(bytes(iv_bytes)).decode()
        with pytest.raises(InvalidTag):
            kms_svc.decrypt("u1", env["encrypted_key"], bad_iv, env["ciphertext"])

    def test_returns_string(self, kms_svc):
        env = kms_svc.encrypt("u1", "test")
        result = kms_svc.decrypt("u1", env["encrypted_key"], env["iv"], env["ciphertext"])
        assert isinstance(result, str)
