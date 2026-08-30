"""Unit tests for Telegram notification dispatch."""

from unittest.mock import patch, MagicMock
from gdrive_sync_master import notify, NotificationConfig


def test_notify_disabled():
    cfg = NotificationConfig(enabled=False, bot_token="token", chat_id="123")
    assert notify("Hello", cfg) is False


def test_notify_missing_token_or_chat():
    cfg1 = NotificationConfig(enabled=True, bot_token="", chat_id="123")
    assert notify("Hello", cfg1) is False

    cfg2 = NotificationConfig(enabled=True, bot_token="token", chat_id="")
    assert notify("Hello", cfg2) is False


@patch("requests.post")
def test_notify_success(mock_post: MagicMock):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_post.return_value = mock_resp

    cfg = NotificationConfig(enabled=True, bot_token="bot12345", chat_id="chat999")
    result = notify("<b>Test Message</b>", cfg)

    assert result is True
    mock_post.assert_called_once()
    args, kwargs = mock_post.call_args
    assert args[0] == "https://api.telegram.org/botbot12345/sendMessage"
    assert kwargs["json"]["chat_id"] == "chat999"
    assert kwargs["json"]["text"] == "<b>Test Message</b>"
    assert kwargs["json"]["parse_mode"] == "HTML"


@patch("requests.post")
def test_notify_http_failure_does_not_raise(mock_post: MagicMock):
    mock_resp = MagicMock()
    mock_resp.status_code = 400
    mock_resp.text = "Bad Request: chat not found"
    mock_post.return_value = mock_resp

    cfg = NotificationConfig(enabled=True, bot_token="bot12345", chat_id="invalid")
    result = notify("Test", cfg)
    assert result is False


@patch("requests.post", side_effect=Exception("Network connection error"))
def test_notify_exception_does_not_raise(mock_post: MagicMock):
    cfg = NotificationConfig(enabled=True, bot_token="bot12345", chat_id="chat999")
    result = notify("Test", cfg)
    assert result is False
