"""Unit tests for polly_service.PollyService — boto3 fully mocked."""

import sys
from unittest.mock import MagicMock, patch

import pytest

sys.modules.setdefault("boto3", MagicMock())

from polly_service import PollyService  # noqa: E402


@pytest.fixture
def polly():
    with patch("polly_service.boto3.client") as client:
        instance = MagicMock()
        client.return_value = instance
        svc = PollyService("fake-key", "fake-secret")
        # expose the mocked client for assertions
        svc._mock_client = instance
        yield svc


# ── Constructor ──────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_constructor_default_region():
    with patch("polly_service.boto3.client") as client:
        PollyService("k", "s")
        kwargs = client.call_args.kwargs
        assert kwargs["region_name"] == "us-east-1"

@pytest.mark.unit
@pytest.mark.parametrize("region", ["us-east-1", "us-west-2", "eu-west-1", "ap-south-1"])
def test_constructor_passes_region(region):
    with patch("polly_service.boto3.client") as client:
        PollyService("k", "s", region_name=region)
        assert client.call_args.kwargs["region_name"] == region

@pytest.mark.unit
def test_constructor_creates_polly_client():
    with patch("polly_service.boto3.client") as client:
        PollyService("k", "s")
        assert client.call_args.args[0] == "polly"


# ── synthesize_speech ────────────────────────────────────────────────────────

@pytest.mark.unit
def test_synthesize_speech_writes_mp3(polly, tmp_path):
    audio = MagicMock()
    audio.read.return_value = b"\xff\xfb"  # MP3 frame magic
    polly._mock_client.synthesize_speech.return_value = {"AudioStream": audio}
    out = polly.synthesize_speech("hello world", str(tmp_path / "out.mp3"))
    assert out == str(tmp_path / "out.mp3")
    with open(out, "rb") as f:
        assert f.read() == b"\xff\xfb"

@pytest.mark.unit
def test_synthesize_speech_no_audio_stream_returns_none(polly, tmp_path):
    polly._mock_client.synthesize_speech.return_value = {}
    out = polly.synthesize_speech("x", str(tmp_path / "out.mp3"))
    assert out is None

@pytest.mark.unit
def test_synthesize_speech_exception_returns_none(polly, tmp_path):
    polly._mock_client.synthesize_speech.side_effect = RuntimeError("boom")
    out = polly.synthesize_speech("x", str(tmp_path / "out.mp3"))
    assert out is None

@pytest.mark.unit
@pytest.mark.parametrize("voice", ["", "Joanna", "Matthew", "Amy", "Brian"])
def test_synthesize_speech_passes_voice_id(polly, tmp_path, voice):
    audio = MagicMock()
    audio.read.return_value = b""
    polly._mock_client.synthesize_speech.return_value = {"AudioStream": audio}
    polly.synthesize_speech("hi", str(tmp_path / "x.mp3"), voice_id=voice)
    kwargs = polly._mock_client.synthesize_speech.call_args.kwargs
    assert kwargs["VoiceId"] == voice

@pytest.mark.unit
@pytest.mark.parametrize("engine", ["neural", "standard"])
def test_synthesize_speech_passes_engine(polly, tmp_path, engine):
    audio = MagicMock()
    audio.read.return_value = b""
    polly._mock_client.synthesize_speech.return_value = {"AudioStream": audio}
    polly.synthesize_speech("hi", str(tmp_path / "x.mp3"), engine=engine)
    kwargs = polly._mock_client.synthesize_speech.call_args.kwargs
    assert kwargs["Engine"] == engine

@pytest.mark.unit
@pytest.mark.parametrize("lang", ["en-US", "en-GB", "es-ES", "de-DE", "fr-FR"])
def test_synthesize_speech_passes_language(polly, tmp_path, lang):
    audio = MagicMock()
    audio.read.return_value = b""
    polly._mock_client.synthesize_speech.return_value = {"AudioStream": audio}
    polly.synthesize_speech("hi", str(tmp_path / "x.mp3"), language_code=lang)
    kwargs = polly._mock_client.synthesize_speech.call_args.kwargs
    assert kwargs["LanguageCode"] == lang

@pytest.mark.unit
def test_synthesize_speech_format_is_mp3(polly, tmp_path):
    audio = MagicMock()
    audio.read.return_value = b""
    polly._mock_client.synthesize_speech.return_value = {"AudioStream": audio}
    polly.synthesize_speech("hi", str(tmp_path / "x.mp3"))
    assert polly._mock_client.synthesize_speech.call_args.kwargs["OutputFormat"] == "mp3"


# ── list_voices ──────────────────────────────────────────────────────────────

@pytest.mark.unit
def test_list_voices_no_language(polly):
    polly._mock_client.describe_voices.return_value = {
        "Voices": [{"Id": "Joanna"}, {"Id": "Matthew"}],
    }
    out = polly.list_voices()
    assert len(out) == 2
    assert out[0]["Id"] == "Joanna"

@pytest.mark.unit
@pytest.mark.parametrize("lang", ["en-US", "es-ES", "ja-JP"])
def test_list_voices_with_language(polly, lang):
    polly._mock_client.describe_voices.return_value = {"Voices": []}
    polly.list_voices(language_code=lang)
    kwargs = polly._mock_client.describe_voices.call_args.kwargs
    assert kwargs == {"LanguageCode": lang}

@pytest.mark.unit
def test_list_voices_handles_exception(polly):
    polly._mock_client.describe_voices.side_effect = RuntimeError("aws down")
    out = polly.list_voices()
    assert out == []

@pytest.mark.unit
def test_list_voices_missing_key_returns_empty(polly):
    polly._mock_client.describe_voices.return_value = {}
    assert polly.list_voices() == []
