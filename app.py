import io
import logging
import os
import shutil
from typing import Optional

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


def _truncate(s: str, max_len: int = 3500) -> str:
    if len(s) <= max_len:
        return s
    # keep some room for truncation note
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
        # Fix common orientation issues and improve OCR reliability a bit
        img = ImageOps.exif_transpose(img)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")

        text = pytesseract.image_to_string(img, lang=lang)
        return (text or "").strip()


def _resolve_tesseract_cmd(logger: logging.Logger) -> bool:
    """
    Best-effort resolution of the Tesseract binary.
    - If TESSERACT_CMD is set, use it.
    - Else, try PATH (shutil.which).
    - Else, try common Windows install locations.

    Returns True if Tesseract is usable, False otherwise.
    """
    env_cmd = os.getenv("TESSERACT_CMD", "").strip()
    candidates = []
    if env_cmd:
        candidates.append(env_cmd)

    path_cmd = shutil.which("tesseract")
    if path_cmd:
        candidates.append(path_cmd)

    # Common Windows install locations
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
            # Normalize slashes a bit (dotenv + Windows paths)
            cmd_norm = cmd.strip().strip('"')
            if os.path.isfile(cmd_norm):
                pytesseract.pytesseract.tesseract_cmd = cmd_norm
                logger.info("Using tesseract binary at: %s", cmd_norm)
                _ = pytesseract.get_tesseract_version()
                return True
        except Exception:
            # keep trying other candidates
            continue

    # Final check: maybe pytesseract can find it without explicit cmd
    try:
        _ = pytesseract.get_tesseract_version()
        return True
    except Exception:
        logger.warning(
            "Tesseract not found. OCR will fail until installed. "
            "Install Tesseract and set TESSERACT_CMD in .env, e.g. "
            'TESSERACT_CMD=C:/Program Files/Tesseract-OCR/tesseract.exe'
        )
        return False


def _post_thread_reply(client, channel: str, thread_ts: str, text: str) -> None:
    client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)


def _is_dm(event: dict) -> bool:
    # DMs use channel ids starting with D
    ch = event.get("channel", "")
    return isinstance(ch, str) and ch.startswith("D")


def _is_channel_or_group(event: dict) -> bool:
    # Public channels start with C; private channels (groups) start with G
    ch = event.get("channel", "")
    return isinstance(ch, str) and (ch.startswith("C") or ch.startswith("G"))


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

    if not bot_token or not app_token:
        raise SystemExit(
            "Missing SLACK_BOT_TOKEN and/or SLACK_APP_TOKEN. "
            "Create a .env file (see .env.example)."
        )

    # Best effort: don't crash the bot if Tesseract isn't installed yet.
    _resolve_tesseract_cmd(logger)

    app = App(token=bot_token)

    @app.event("message")
    def handle_message_events(event, client, logger):
        try:
            subtype = event.get("subtype")
            if subtype in {"bot_message", "message_changed", "message_deleted"}:
                return

            # Slack can send events that aren't actual user messages
            if "ts" not in event or "channel" not in event:
                return

            if _is_dm(event):
                pass  # required
            elif allow_channels and _is_channel_or_group(event):
                pass  # optional
            else:
                return

            files = event.get("files") or []
            if not isinstance(files, list) or not files:
                return

            channel = event["channel"]
            thread_ts = event["ts"]

            # Process only image/* files; ignore everything else
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
                channel=channel,
                thread_ts=thread_ts,
                text="Got it — running OCR on your screenshot…",
            )

            # If multiple images are attached, process each and reply separately
            for f in image_files:
                file_id = f.get("id")
                name = f.get("name") or f.get("title") or "image"
                url = f.get("url_private_download") or f.get("url_private")

                if not url:
                    logger.warning("No downloadable URL for file_id=%s name=%s", file_id, name)
                    _post_thread_reply(
                        client,
                        channel=channel,
                        thread_ts=thread_ts,
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
                        channel=channel,
                        thread_ts=thread_ts,
                        text=f"Failed to download `{name}`. Error: `{type(e).__name__}`",
                    )
                    continue

                try:
                    text = _run_ocr(content, lang=ocr_lang)
                except Exception as e:
                    logger.exception("OCR failed for file_id=%s name=%s", file_id, name)
                    if type(e).__name__ == "TesseractNotFoundError":
                        _post_thread_reply(
                            client,
                            channel=channel,
                            thread_ts=thread_ts,
                            text=(
                                "OCR failed because **Tesseract** isn't installed / isn't on PATH.\n"
                                "On Windows, install Tesseract and set `TESSERACT_CMD` in `.env`, e.g.\n"
                                "`TESSERACT_CMD=C:\\Program Files\\Tesseract-OCR\\tesseract.exe`"
                            ),
                        )
                        continue
                    _post_thread_reply(
                        client,
                        channel=channel,
                        thread_ts=thread_ts,
                        text=f"OCR failed for `{name}`. Error: `{type(e).__name__}`",
                    )
                    continue

                if not text:
                    _post_thread_reply(
                        client,
                        channel=channel,
                        thread_ts=thread_ts,
                        text="OCR ran, but I didn’t detect readable text. Try a clearer screenshot.",
                    )
                    continue

                text = _truncate(text, 3500)
                _post_thread_reply(
                    client,
                    channel=channel,
                    thread_ts=thread_ts,
                    text=f"```{text}```",
                )

        except Exception:
            logger.exception("Unhandled error while processing message event")

    logger.info("Starting Slack Socket Mode bot (channels enabled: %s)", allow_channels)
    SocketModeHandler(app, app_token).start()


if __name__ == "__main__":
    main()
