# -*- coding: utf-8 -*-
"""
飞书 发送提醒服务

职责：
1. 通过 webhook 发送飞书消息
2. 通过飞书应用机器人（App Bot）发送消息（lark-oapi SDK）
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import tempfile
import threading
import time
import uuid as uuid_mod
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import requests

from src.config import Config
from src.formatters import (
    MIN_MAX_BYTES,
    PAGE_MARKER_SAFE_BYTES,
    chunk_content_by_max_bytes,
    format_feishu_markdown,
    strip_hidden_markdown_metadata,
)
from src.notification_sender.message_text import get_message_text, resolve_message_language

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# lark-oapi SDK availability
# ---------------------------------------------------------------------------

FEISHU_SDK_AVAILABLE = False
_lark: Any = None  # type: ignore[assignment]
FEISHU_DOMAIN = "feishu"
LARK_DOMAIN = "lark"
try:
    import lark_oapi as _lark
    from lark_oapi.api.im.v1 import (
        CreateMessageRequest,
        CreateMessageRequestBody,
    )
    from lark_oapi.core.const import FEISHU_DOMAIN as _SDK_FEISHU_DOMAIN
    from lark_oapi.core.const import LARK_DOMAIN as _SDK_LARK_DOMAIN

    FEISHU_DOMAIN = _SDK_FEISHU_DOMAIN
    LARK_DOMAIN = _SDK_LARK_DOMAIN
    FEISHU_SDK_AVAILABLE = True
except ImportError:
    pass

# File-upload SDK classes (isolated from the core messaging SDK availability
# so that an older lark-oapi without file support doesn't break App Bot text).
FEISHU_FILE_SDK_AVAILABLE = False
_CreateFileRequest: Any = None
_CreateFileRequestBody: Any = None
try:
    from lark_oapi.api.im.v1 import (
        CreateFileRequest as _CreateFileRequest,
        CreateFileRequestBody as _CreateFileRequestBody,
    )
    FEISHU_FILE_SDK_AVAILABLE = True
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_APP_SEND_RETRIES = 3
_APP_SEND_BACKOFF_SECONDS = (1.0, 2.0, 4.0)
_WEBHOOK_SEND_TIMEOUT_SECONDS = 30

# Sentinel for "client not yet initialised".
_NO_CLIENT = object()


class FeishuSender:

    def __init__(self, config: Config):
        """
        Initialise Feishu sender.

        Two mutually exclusive routing modes are supported:
          1. **Webhook** – configured via ``feishu_webhook_url`` (legacy).
          2. **App Bot** – configured via ``feishu_app_id`` + ``feishu_app_secret``
             + ``feishu_chat_id``, sends through the ``lark-oapi`` SDK.

        Webhook mode takes precedence when both are configured.
        """
        # -- Webhook mode --
        self._feishu_url = getattr(config, "feishu_webhook_url", None)
        self._feishu_secret = (getattr(config, "feishu_webhook_secret", None) or "").strip()
        self._feishu_keyword = (getattr(config, "feishu_webhook_keyword", None) or "").strip()
        self._feishu_max_bytes = getattr(config, "feishu_max_bytes", 20000)
        self._feishu_send_as_file = getattr(config, "feishu_send_as_file", False)
        self._webhook_verify_ssl = getattr(config, "webhook_verify_ssl", True)

        # -- App Bot mode --
        self._feishu_app_id = (getattr(config, "feishu_app_id", None) or "").strip()
        self._feishu_app_secret = (getattr(config, "feishu_app_secret", None) or "").strip()
        self._feishu_chat_id = (getattr(config, "feishu_chat_id", None) or "").strip()
        self._feishu_receive_id_type = (
            getattr(config, "feishu_receive_id_type", None) or "chat_id"
        ).strip().lower()
        if self._feishu_receive_id_type not in ("chat_id", "open_id"):
            logger.warning(
                "Invalid FEISHU_RECEIVE_ID_TYPE=%s, falling back to chat_id",
                self._feishu_receive_id_type,
            )
            self._feishu_receive_id_type = "chat_id"
        # domain_name must be "feishu" or "lark"; anything else defaulted to feishu.
        raw_domain = (
            getattr(config, "feishu_domain", None) or os.getenv("FEISHU_DOMAIN", "feishu")
        ).strip().lower()
        if raw_domain not in ("feishu", "lark"):
            logger.warning(
                "Invalid FEISHU_DOMAIN=%s, falling back to feishu", raw_domain
            )
            raw_domain = "feishu"
        self._feishu_domain = FEISHU_DOMAIN if raw_domain == "feishu" else LARK_DOMAIN

        self._app_client: Any = _NO_CLIENT
        self._app_client_lock = threading.Lock()
        self._language = resolve_message_language(config)

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _build_card_body(self, content: str) -> dict:
        """Build a Feishu interactive-card body (without the ``msg_type`` wrapper)."""
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": get_message_text("report_title_full", self._language),
                },
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": content},
                }
            ],
        }

    # ------------------------------------------------------------------
    # Webhook helpers (unchanged legacy path)
    # ------------------------------------------------------------------

    def _get_keyword_prefix(self) -> str:
        if not self._feishu_keyword:
            return ""
        return f"{self._feishu_keyword}\n"

    def _apply_keyword_prefix(self, content: str) -> str:
        prefix = self._get_keyword_prefix()
        if not prefix:
            return content
        return f"{prefix}{content}" if content else self._feishu_keyword

    def _build_security_fields(self) -> Dict[str, str]:
        if not self._feishu_secret:
            return {}
        timestamp = str(int(time.time()))
        string_to_sign = f"{timestamp}\n{self._feishu_secret}"
        sign = base64.b64encode(
            hmac.new(
                string_to_sign.encode("utf-8"),
                digestmod=hashlib.sha256,
            ).digest()
        ).decode("utf-8")
        return {"timestamp": timestamp, "sign": sign}

    # ------------------------------------------------------------------
    # App Bot client (lazy, thread-safe)
    # ------------------------------------------------------------------

    def _ensure_app_client(self) -> Any:
        """Lazily initialise the ``lark-oapi`` client for App Bot mode."""
        if self._app_client is not _NO_CLIENT:
            return self._app_client
        with self._app_client_lock:
            if self._app_client is not _NO_CLIENT:
                return self._app_client
            if not FEISHU_SDK_AVAILABLE:
                logger.warning(
                    "Feishu App Bot requires the lark-oapi library; standard install: pip install -r requirements.txt"
                )
                self._app_client = None
                return None
            if not self._feishu_app_id or not self._feishu_app_secret:
                missing = []
                if not self._feishu_app_id:
                    missing.append("FEISHU_APP_ID")
                if not self._feishu_app_secret:
                    missing.append("FEISHU_APP_SECRET")
                logger.warning("Feishu App Bot credentials incomplete, missing: %s", ", ".join(missing))
                self._app_client = None
                return None
            try:
                self._app_client = (
                    _lark.Client.builder()
                    .app_id(self._feishu_app_id)
                    .app_secret(self._feishu_app_secret)
                    .domain(self._feishu_domain)
                    .log_level(_lark.LogLevel.WARNING)
                    .build()
                )
                logger.info("Feishu App Bot client initialized successfully (domain=%s)", self._feishu_domain)
            except Exception as e:
                logger.error("Feishu App Bot client initialization failed: %s", e)
                self._app_client = None
            return self._app_client

    # ------------------------------------------------------------------
    # App Bot send helpers
    # ------------------------------------------------------------------

    def _send_via_app_bot(self, content: str) -> bool:
        """Send message through the Feishu App Bot, chunking if necessary."""
        if not self._feishu_chat_id:
            logger.warning("FEISHU_CHAT_ID not configured, skipping App Bot push")
            return False

        client = self._ensure_app_client()
        if client is None:
            return False

        formatted = format_feishu_markdown(content)
        content_bytes = len(formatted.encode("utf-8"))

        if content_bytes > self._feishu_max_bytes:
            logger.info(
                "App Bot message too long (%d bytes), sending in batches", content_bytes
            )
            return self._app_send_chunked(client, formatted)

        return self._app_send_once(client, formatted)

    def _app_send_chunked(self, client: Any, content: str) -> bool:
        """Chunk and send long content through App Bot."""
        try:
            chunks = chunk_content_by_max_bytes(
                content, self._feishu_max_bytes, add_page_marker=True
            )
        except (ValueError, TypeError, Exception) as e:
            logger.error("App Bot chunking failed: %s", e)
            return False

        success = True
        for i, chunk in enumerate(chunks):
            ok = self._app_send_once(client, chunk)
            if not ok:
                logger.error("App Bot batch %d/%d send failed", i + 1, len(chunks))
                success = False
            if i < len(chunks) - 1:
                time.sleep(1)
        return success

    def _app_send_once(self, client: Any, content: str) -> bool:
        """Single-shot send via App Bot with card-first / text-fallback.

        Content received here has already been through ``format_feishu_markdown``
        which converts all Markdown constructs to ``lark_md``-compatible format.
        The interactive card uses ``tag: lark_md`` for rendering.
        """
        card_payload = json.dumps(self._build_card_body(content), ensure_ascii=False)

        if self._app_send_raw(client, "interactive", card_payload):
            return True

        # Fallback to plain text.
        text_payload = json.dumps({"text": content}, ensure_ascii=False)
        return self._app_send_raw(client, "text", text_payload)

    def _app_send_raw(self, client: Any, msg_type: str, content_json: str) -> bool:
        """Low-level send via lark-oapi SDK with retry and idempotency UUID.

        Request construction is done once outside the retry loop; it is
        deterministic and a construction error is a programming error, not
        a transient failure.
        """
        if client is None:
            return False

        send_uuid = str(uuid_mod.uuid4())
        try:
            req = (
                CreateMessageRequest.builder()
                .receive_id_type(self._feishu_receive_id_type)
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(self._feishu_chat_id)
                    .content(content_json)
                    .msg_type(msg_type)
                    .uuid(send_uuid)
                    .build()
                )
                .build()
            )
        except Exception as e:
            logger.error("App Bot request construction failed: %s: %s", type(e).__name__, e)
            return False

        last_status: Optional[str] = None

        for attempt in range(_APP_SEND_RETRIES):
            try:
                resp = client.im.v1.message.create(req)
            except Exception as e:
                logger.warning(
                    "App Bot send exception (attempt=%d/%d): %s: %s",
                    attempt + 1, _APP_SEND_RETRIES, type(e).__name__, e,
                )
                if attempt < _APP_SEND_RETRIES - 1:
                    time.sleep(
                        _APP_SEND_BACKOFF_SECONDS[
                            min(attempt, len(_APP_SEND_BACKOFF_SECONDS) - 1)
                        ]
                    )
                continue

            if resp.success():
                logger.info("App Bot message sent successfully (type=%s)", msg_type)
                return True

            try:
                log_id = resp.get_log_id()
            except (AttributeError, Exception):
                log_id = "N/A"
            status = "code=%s, msg=%s, log_id=%s" % (
                resp.code, resp.msg, log_id,
            )
            last_status = status
            logger.warning(
                "App Bot send failed (attempt=%d/%d): %s",
                attempt + 1, _APP_SEND_RETRIES, status,
            )

            if attempt < _APP_SEND_RETRIES - 1:
                time.sleep(
                    _APP_SEND_BACKOFF_SECONDS[
                        min(attempt, len(_APP_SEND_BACKOFF_SECONDS) - 1)
                    ]
                )

        if last_status:
            logger.error("App Bot send ultimately failed: %s", last_status)
        return False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def send_to_feishu(self, content: str, *, timeout_seconds: Optional[float] = None) -> bool:
        """
        Push a message to Feishu.

        Routing priority:
          1. **Webhook** – when ``feishu_webhook_url`` is configured.
          2. **App Bot** – when ``feishu_app_id`` + ``feishu_app_secret``
             + ``feishu_chat_id`` are all configured and webhook is absent.

        Returns:
            Whether the send succeeded.
        """
        if content is None:
            logger.error("send_to_feishu: content must not be None")
            return False
        sanitized_content = strip_hidden_markdown_metadata(content).strip()
        if self._feishu_url:
            return self._send_via_webhook(sanitized_content, timeout_seconds=timeout_seconds)
        return self._send_via_app_bot(sanitized_content)

    def send_feishu_file(self, file_path: str) -> bool:
        """
        Upload and send a file to the Feishu chat.

        .. note::

           * **App Bot mode** – uploads the file via the lark-oapi SDK and
             sends it as a file message.  This is the recommended path.
           * **Webhook mode** – reads the file content and sends it as a
             regular text/card message (webhooks do not support file upload).

        Args:
            file_path: Absolute or relative path to the local file.

        Returns:
            Whether the send succeeded.
        """
        path = Path(file_path)
        if not path.is_file():
            logger.error("send_feishu_file: file does not exist: %s", file_path)
            return False

        upload_path, display_name = self._prepare_sanitized_file(path)
        try:
            if self._feishu_url:
                # Webhook mode: send file content as a message (best-effort).
                return self._send_file_via_webhook(upload_path, display_name=display_name)

            # App Bot mode: upload file via SDK.
            return self._send_file_via_app_bot(upload_path, display_name=display_name)
        finally:
            if upload_path != path:
                try:
                    upload_path.unlink()
                except OSError:
                    logger.warning("Failed to clean up Feishu temp file: %s", upload_path)

    @staticmethod
    def _prepare_sanitized_file(path: Path) -> Tuple[Path, str]:
        display_name = path.name
        if path.suffix.lower() not in {".md", ".markdown", ".txt"}:
            return path, display_name

        try:
            raw_text = path.read_text("utf-8")
        except (OSError, UnicodeDecodeError):
            return path, display_name
        if not isinstance(raw_text, str):
            return path, display_name

        sanitized_text = strip_hidden_markdown_metadata(raw_text).strip()
        if sanitized_text == raw_text:
            return path, display_name

        temp_file = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=path.suffix,
            prefix="feishu_sanitized_",
            delete=False,
        )
        with temp_file:
            temp_file.write(sanitized_text)
        return Path(temp_file.name), display_name

    def _send_file_via_app_bot(self, path: Path, *, display_name: Optional[str] = None) -> bool:
        """Upload *path* to Feishu via App Bot SDK and send as file message."""
        if not FEISHU_FILE_SDK_AVAILABLE:
            logger.warning("lark-oapi SDK does not support file upload; upgrade lark-oapi")
            return False

        if not self._feishu_chat_id:
            logger.warning("FEISHU_CHAT_ID not configured, skipping App Bot file push")
            return False

        client = self._ensure_app_client()
        if client is None:
            return False

        file_name = display_name or path.name
        # Determine file_type from extension; fall back to "stream" for unknown types.
        feishu_file_types = {
            ".opus": "opus", ".aac": "aac", ".amr": "amr", ".mp3": "mp3",
            ".wma": "wma", ".pcm": "pcm", ".wav": "wav",
            ".mp4": "mp4", ".gif": "gif",
            ".pdf": "pdf",
            ".doc": "doc", ".docx": "docx",
            ".xls": "xls", ".xlsx": "xlsx",
            ".ppt": "ppt", ".pptx": "pptx",
        }
        file_type = feishu_file_types.get(path.suffix.lower(), "stream")

        try:
            with path.open("rb") as f:
                body = (
                    _CreateFileRequestBody.builder()
                    .file_type(file_type)
                    .file_name(file_name)
                    .file(f)  # type: ignore[arg-type]
                    .build()
                )
                req = (
                    _CreateFileRequest.builder()
                    .request_body(body)
                    .build()
                )
                resp = client.im.v1.file.create(req)
        except Exception as e:
            logger.error("App Bot file upload exception: %s: %s", type(e).__name__, e)
            return False

        if not resp.success():
            try:
                log_id = resp.get_log_id()
            except (AttributeError, Exception):
                log_id = "N/A"
            logger.error(
                "App Bot file upload failed: code=%s, msg=%s, log_id=%s",
                resp.code, resp.msg, log_id,
            )
            return False

        file_key = resp.data.file_key if resp.data else None
        if not file_key:
            logger.error("App Bot file upload succeeded but no file_key returned")
            return False

        logger.info("App Bot file upload succeeded: file_key=%s, file_name=%s", file_key, file_name)

        # Send a file message with the uploaded file_key.
        content_json = json.dumps({"file_key": file_key})
        return self._app_send_raw(client, "file", content_json)

    def _guess_mime_for_webhook(self, path: Path) -> str:
        """Determine a human-readable label for webhook fallback."""
        suffix = path.suffix.lower()
        labels = {
            ".md": "Markdown",
            ".txt": get_message_text("file_label_text", self._language),
            ".pdf": "PDF",
            ".csv": "CSV",
        }
        return labels.get(
            suffix,
            suffix.lstrip(".").upper() or get_message_text("file_label_generic", self._language),
        )

    def _send_file_via_webhook(self, path: Path, *, display_name: Optional[str] = None) -> bool:
        """Send file *content* as a Feishu message (webhook fallback)."""
        try:
            text = path.read_text("utf-8")
        except (OSError, UnicodeDecodeError) as e:
            logger.error("Failed to read file content (webhook fallback): %s: %s", type(e).__name__, e)
            return False

        file_label = self._guess_mime_for_webhook(path)
        header_text = get_message_text(
            "file_content_prefix", self._language, label=file_label, name=display_name or path.name
        )
        header = f"**📄 {header_text}**\n\n"
        content = header + strip_hidden_markdown_metadata(text).strip()
        # Delegate to the existing webhook send path.
        return self._send_via_webhook(content)

    # ------------------------------------------------------------------
    # Webhook path (legacy, unchanged)
    # ------------------------------------------------------------------

    def _send_via_webhook(self, content: str, *, timeout_seconds: Optional[float] = None) -> bool:
        """Legacy webhook send path."""
        formatted_content = format_feishu_markdown(content)

        max_bytes = self._feishu_max_bytes
        keyword_overhead = len(self._get_keyword_prefix().encode("utf-8"))
        effective_max_bytes = max_bytes - keyword_overhead

        if effective_max_bytes <= 0:
            logger.error("Feishu keyword too long, exceeds the max bytes allowed per message, cannot send")
            return False

        content_bytes = len(formatted_content.encode("utf-8")) + keyword_overhead
        if content_bytes > max_bytes:
            min_chunk_bytes = MIN_MAX_BYTES + PAGE_MARKER_SAFE_BYTES
            if effective_max_bytes < min_chunk_bytes:
                logger.error(
                    "Feishu keyword too long, remaining chunk budget (%s bytes) is insufficient "
                    "to safely paginate; at least %s bytes required",
                    effective_max_bytes,
                    min_chunk_bytes,
                )
                return False
            logger.info("Feishu message content too long (%d bytes/%d chars), sending in batches", content_bytes, len(content))
            return self._send_feishu_chunked(formatted_content, effective_max_bytes)

        try:
            return self._send_feishu_message(formatted_content, timeout_seconds=timeout_seconds)
        except Exception as e:
            logger.error("Failed to send Feishu message: %s", e)
            return False

    def _send_feishu_chunked(self, content: str, max_bytes: int) -> bool:
        try:
            chunks = chunk_content_by_max_bytes(content, max_bytes, add_page_marker=True)
        except ValueError as e:
            logger.error("Feishu message chunking failed, per-chunk budget insufficient to safely paginate (keyword too long or max_bytes too small): %s", e)
            return False

        total_chunks = len(chunks)
        success_count = 0
        logger.info("Feishu batch send: %d batches total", total_chunks)
        for i, chunk in enumerate(chunks):
            try:
                if self._send_feishu_message(chunk):
                    success_count += 1
                    logger.info("Feishu batch %d/%d sent successfully", i + 1, total_chunks)
                else:
                    logger.error("Feishu batch %d/%d send failed", i + 1, total_chunks)
            except Exception as e:
                logger.error("Feishu batch %d/%d send exception: %s", i + 1, total_chunks, e)
            if i < total_chunks - 1:
                time.sleep(1)
        return success_count == total_chunks

    def _send_feishu_message(self, content: str, *, timeout_seconds: Optional[float] = None) -> bool:
        """Send a single Feishu webhook message (interactive card, fallback text)."""
        prepared_content = self._apply_keyword_prefix(content)
        security_fields = self._build_security_fields()

        def _post_payload(payload: Dict[str, Any]) -> bool:
            request_payload = dict(payload)
            request_payload.update(security_fields)
            try:
                response = requests.post(
                    self._feishu_url,
                    json=request_payload,
                    timeout=timeout_seconds or _WEBHOOK_SEND_TIMEOUT_SECONDS,
                    verify=self._webhook_verify_ssl,
                )
            except (requests.exceptions.ConnectionError,
                     requests.exceptions.Timeout,
                     requests.exceptions.RequestException) as e:
                logger.error("Feishu webhook network request exception: %s", e)
                return False
            if response.status_code == 200:
                try:
                    result = response.json()
                except (ValueError, AttributeError):
                    logger.error("Feishu webhook returned a non-JSON response: %s", response.text[:200])
                    return False
                if not isinstance(result, dict):
                    logger.error("Feishu webhook returned an unexpected format: %s", type(result).__name__)
                    return False
                code = result.get("code") if "code" in result else result.get("StatusCode")
                if code == 0:
                    logger.info("Feishu webhook message sent successfully")
                    return True
                logger.error(
                    "Feishu webhook returned an error [code=%s]: %s",
                    code,
                    result.get("msg") or result.get("StatusMessage", "Unknown error"),
                )
                return False
            logger.error("Feishu webhook request failed: HTTP %d", response.status_code)
            return False

        card_payload = {"msg_type": "interactive", "card": self._build_card_body(prepared_content)}

        if _post_payload(card_payload):
            return True

        text_payload = {
            "msg_type": "text",
            "content": {"text": prepared_content},
        }
        return _post_payload(text_payload)
