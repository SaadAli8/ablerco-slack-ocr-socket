import io
import logging
import os
import shutil
from datetime import datetime, timezone

import pytesseract
import requests
from dotenv import load_dotenv
from PIL import Image, ImageOps
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler


def _env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "t", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    val = os.getenv(name)
    if val is None:
        return default
    try:
        return int(val.strip())
    except Exception:
        return default


def _truncate(s: str, max_len: int = 3500) -> str:
    if len(s) <= max_len:
        return s
    suffix = "\n\n[truncated]"
    keep = max(0, max_len - len(suffix))
    return s[:keep].rstrip() + suffix


def _download_slack_file(url: str, bot_token: str, timeout_s: int = 60) -> bytes:
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {bot_token}"},
        timeout=timeout_s,
    )
    resp.raise_for_status()
    return resp.content


def _run_ocr(image_bytes: bytes, lang: str = "eng") -> str:
    with Image.open(io.BytesIO(image_bytes)) as img:
        img = ImageOps.exif_transpose(img)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        text = pytesseract.image_to_string(img, lang=lang)
        return (text or "").strip()


def _resolve_tesseract_cmd(logger: logging.Logger) -> bool:
    env_cmd = os.getenv("TESSERACT_CMD", "").strip()
    candidates = []
    if env_cmd:
        candidates.append(env_cmd)

    path_cmd = shutil.which("tesseract")
    if path_cmd:
        candidates.append(path_cmd)

    candidates.extend(
        [
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        ]
    )

    for cmd in candidates:
        if not cmd:
            continue
        try:
            cmd_norm = cmd.strip().strip('"')
            if os.path.isfile(cmd_norm):
                pytesseract.pytesseract.tesseract_cmd = cmd_norm
                logger.info("Using tesseract binary at: %s", cmd_norm)
                _ = pytesseract.get_tesseract_version()
                return True
        except Exception:
            continue

    try:
        _ = pytesseract.get_tesseract_version()
        return True
    except Exception:
        logger.warning(
            "Tesseract not found. OCR will fail until installed. "
            "Install Tesseract and set TESSERACT_CMD in .env."
        )
        return False


def _post_thread_reply(client, channel: str, thread_ts: str, text: str) -> None:
    client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)


def _post_message(client, channel: str, text: str) -> None:
    client.chat_postMessage(channel=channel, text=text)


def _upload_text_file(client, channel: str, filename: str, content: str, title: str) -> None:
    # Requires Slack scope: files:write
    client.files_upload_v2(
        channel=channel,
        title=title,
        filename=filename,
        content=content,
    )


def _format_mentions(user_ids_csv: str) -> str:
    ids = [x.strip() for x in (user_ids_csv or "").split(",") if x.strip()]
    if not ids:
        return ""
    return " ".join(f"<@{uid}>" for uid in ids)


def _is_dm(event: dict) -> bool:
    ch = event.get("channel", "")
    return isinstance(ch, str) and ch.startswith("D")


def _is_channel_or_group(event: dict) -> bool:
    ch = event.get("channel", "")
    return isinstance(ch, str) and (ch.startswith("C") or ch.startswith("G"))


def _build_pretty_message(
    *,
    mentions: str,
    uploader_mention: str,
    filename: str,
    ocr_text: str,
    preview_lines: int,
) -> tuple[str, str]:
    """
    Returns: (pretty_text_message, preview_only_text)
    """
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [ln.rstrip() for ln in (ocr_text or "").splitlines()]
    # Remove excessive empty lines for nicer preview
    cleaned = [ln for ln in lines if ln.strip() != ""]
    if not cleaned:
        cleaned = lines  # fallback

    preview = "\n".join(cleaned[:preview_lines]).strip()
    if not preview:
        preview = "(no readable text detected)"

    remaining = max(0, len(cleaned) - preview_lines)
    more_note = f"\n\n_…({remaining} more lines)_ " if remaining > 0 else ""

    header = []
    if mentions:
        header.append(mentions)
    header.append("*OCR Transcription*")
    header.append(f"• *File:* `{filename}`")
    if uploader_mention:
        header.append(f"• *Uploaded by:* {uploader_mention}")
    header.append(f"• *Time:* {now_utc}")

    header_text = "\n".join(header)

    # IMPORTANT: mentions must be OUTSIDE code block to notify
    message = f"{header_text}\n\n```{preview}```{more_note}".strip()
    return message, preview


def main() -> None:
    load_dotenv()

    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    logger = logging.getLogger("ablerco_slack_ocr_socket")

    bot_token = os.getenv("SLACK_BOT_TOKEN", "").strip()
    app_token = os.getenv("SLACK_APP_TOKEN", "").strip()
    ocr_lang = os.getenv("OCR_LANG", "eng").strip() or "eng"
    allow_channels = _env_bool("ALLOW_CHANNELS", True)

    # Posting destination + notifications
    target_channel_id = os.getenv("TARGET_CHANNEL_ID", "").strip()
    notify_user_ids = os.getenv("NOTIFY_USER_IDS", "").strip()
    reply_back_to_source = _env_bool("REPLY_BACK_TO_SOURCE", True)

    # Design options
    upload_full_text_as_file = _env_bool("UPLOAD_FULL_TEXT_AS_FILE", True)
    preview_lines = _env_int("PREVIEW_LINES", 15)

    if not bot_token or not app_token:
        raise SystemExit(
            "Missing SLACK_BOT_TOKEN and/or SLACK_APP_TOKEN. Create a .env file."
        )

    if not target_channel_id:
        raise SystemExit(
            "Missing TARGET_CHANNEL_ID in .env. Example: TARGET_CHANNEL_ID=C012ABCDEF"
        )

    _resolve_tesseract_cmd(logger)

    app = App(token=bot_token)

    @app.event("message")
    def handle_message_events(event, client, logger):
        try:
            subtype = event.get("subtype")
            if subtype in {"bot_message", "message_changed", "message_deleted"}:
                return

            if "ts" not in event or "channel" not in event:
                return

            # Accept DMs or channels (if enabled)
            if _is_dm(event):
                pass
            elif allow_channels and _is_channel_or_group(event):
                pass
            else:
                return

            files = event.get("files") or []
            if not isinstance(files, list) or not files:
                return

            source_channel = event["channel"]
            source_thread_ts = event["ts"]

            # Only image files
            image_files = []
            for f in files:
                if not isinstance(f, dict):
                    continue
                mimetype = (f.get("mimetype") or "").lower()
                if mimetype.startswith("image/"):
                    image_files.append(f)

            if not image_files:
                return

            _post_thread_reply(
                client,
                channel=source_channel,
                thread_ts=source_thread_ts,
                text="Got it — running OCR on your screenshot…",
            )

            uploader_id = event.get("user")
            uploader_mention = f"<@{uploader_id}>" if uploader_id else ""
            mentions = _format_mentions(notify_user_ids)

            for f in image_files:
                file_id = f.get("id")
                name = f.get("name") or f.get("title") or "image"
                url = f.get("url_private_download") or f.get("url_private")

                if not url:
                    logger.warning("No downloadable URL for file_id=%s name=%s", file_id, name)
                    _post_thread_reply(
                        client,
                        channel=source_channel,
                        thread_ts=source_thread_ts,
                        text=f"Couldn't download `{name}` (missing private URL).",
                    )
                    continue

                try:
                    logger.info("Downloading Slack file %s (%s)", name, file_id)
                    content = _download_slack_file(url, bot_token=bot_token)
                except Exception as e:
                    logger.exception("Failed downloading file_id=%s name=%s", file_id, name)
                    _post_thread_reply(
                        client,
                        channel=source_channel,
                        thread_ts=source_thread_ts,
                        text=f"Failed to download `{name}`. Error: `{type(e).__name__}`",
                    )
                    continue

                try:
                    ocr_text = _run_ocr(content, lang=ocr_lang)
                except Exception as e:
                    logger.exception("OCR failed for file_id=%s name=%s", file_id, name)
                    if type(e).__name__ == "TesseractNotFoundError":
                        _post_thread_reply(
                            client,
                            channel=source_channel,
                            thread_ts=source_thread_ts,
                            text=(
                                "OCR failed because **Tesseract** isn't installed / isn't on PATH.\n"
                                "On Linux: set `TESSERACT_CMD=/usr/bin/tesseract` in `.env`."
                            ),
                        )
                        continue
                    _post_thread_reply(
                        client,
                        channel=source_channel,
                        thread_ts=source_thread_ts,
                        text=f"OCR failed for `{name}`. Error: `{type(e).__name__}`",
                    )
                    continue

                if not ocr_text:
                    _post_thread_reply(
                        client,
                        channel=source_channel,
                        thread_ts=source_thread_ts,
                        text="OCR ran, but I didn’t detect readable text. Try a clearer screenshot.",
                    )
                    continue

                # Keep full text safe for file upload; keep preview pretty
                full_text = _truncate(ocr_text, 12000)  # file can be bigger; keep reasonable
                pretty_message, _ = _build_pretty_message(
                    mentions=mentions,
                    uploader_mention=uploader_mention,
                    filename=name,
                    ocr_text=ocr_text,
                    preview_lines=preview_lines,
                )

                # Post nice message to shared channel
                _post_message(client, channel=target_channel_id, text=pretty_message)

                # Optional: upload full text as file for clean UI
                if upload_full_text_as_file:
                    try:
                        safe_base = os.path.splitext(name)[0] or "ocr"
                        filename = f"{safe_base}_ocr.txt"
                        _upload_text_file(
                            client,
                            channel=target_channel_id,
                            filename=filename,
                            content=full_text,
                            title=f"OCR Full Text — {name}",
                        )
                    except Exception:
                        logger.exception("Failed to upload OCR txt file (missing files:write scope?)")

                # Confirm back to source thread
                if reply_back_to_source:
                    _post_thread_reply(
                        client,
                        channel=source_channel,
                        thread_ts=source_thread_ts,
                        text=f"✅ OCR complete — posted to <#{target_channel_id}>.",
                    )

        except Exception:
            logger.exception("Unhandled error while processing message event")

    logger.info("Starting Slack Socket Mode bot (channels enabled: %s)", allow_channels)
    SocketModeHandler(app, app_token).start()


if __name__ == "__main__":
    main()
