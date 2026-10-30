#!/usr/bin/env python3
"""Alexa custom-skill backend for the "Toby AI" skill.

Receives Alexa JSON requests, asks a Gemma model through the Google Gemini
API, and returns plain-text speech.  Python standard library only.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import base64
import datetime
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_PORT = 8432
ENV_FILE = os.environ.get("ENV_FILE") or os.path.expanduser("~/.env")
MODEL_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemma-4-26b-a4b-it:generateContent"
)
FALLBACK = "Sorry, I could not reach my brain just now. Please try again."
SPEECH_LIMIT = 7800

CERT_HOST = "s3.amazonaws.com"
CERT_PATH_PREFIX = "/echo.api/"
MAX_CERT_BYTES = 32 * 1024
CHAIN_CACHE_TTL = 3600
REPLAY_WINDOW_SECONDS = 150

SYSTEM_PROMPT = '''You are Toby, a voice assistant in a smart speaker. Your reply is spoken aloud, never displayed.

## Input
Speech recognition may deliver a fragment rather than a question. Infer the intent and answer it.

## Voice
- One or two short sentences, plain spoken English
- No markdown, bullets, headings, emoji, parentheses or symbols
- Speak numbers the way a person would; never read out URLs, code or long digit strings
- Answer directly; never open with "Sure", "Certainly" or a restatement of the question

## Accuracy
- Answer truthfully. If unsure of a figure, say it is approximate or that you do not know.
- Never invent precision you do not have.'''

# Runtime configuration, filled in by main() before the server starts.
PORT = DEFAULT_PORT
SKILL_ID = ""
GEMINI_API_KEY = ""
API_KEY = ""
ENFORCE_SIGNATURE = False

# Serialises stdout logging so concurrent requests produce intact lines.
LOG_LOCK = threading.Lock()

# Downloaded certificate chains, keyed by URL, with a monotonic expiry.
CHAIN_CACHE = {}
CHAIN_CACHE_LOCK = threading.Lock()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so a fetched certificate URL cannot pivot elsewhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def sanitise(text):
    """Turn arbitrary text into safe, speaker-friendly plain speech."""
    if not text:
        return ""

    # Collapse every run of whitespace to a single space.
    text = re.sub(r"\s+", " ", str(text))

    # Strip leading bullet and numbered-list markers, including stacked ones.
    while True:
        cleaned = re.sub(r"^\s*(?:[-*]\s+|\d+\.\s+)", "", text)
        if cleaned == text:
            break
        text = cleaned

    # Replace links before deleting markdown characters.
    text = re.sub(r"https?://\S+", "a link", text)

    # Drop markdown punctuation that should never be spoken.
    text = re.sub(r"[*_`#>]", "", text)
    text = text.strip()

    # Truncate at the last sentence end before the limit.
    if len(text) > SPEECH_LIMIT:
        head = text[:SPEECH_LIMIT]
        cut = max(head.rfind("."), head.rfind("!"), head.rfind("?"))
        text = head[: cut + 1] if cut != -1 else head

    return text.strip()


def speech_response(speech="", end_session=True, reprompt=""):
    """Build the exact Alexa response envelope required by the skill."""
    speech = sanitise(speech)
    reprompt = sanitise(reprompt)

    response = {}
    if speech:
        response["outputSpeech"] = {"type": "PlainText", "text": speech}

    response["shouldEndSession"] = end_session

    if not end_session and reprompt:
        response["reprompt"] = {"type": "PlainText", "text": reprompt}

    return {"version": "1.0", "response": response}


def ask_model(question, deadline):
    """Ask Gemma for an answer, returning the fallback line on any failure."""
    payload = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": question}]}],
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": 500,
            "thinkingConfig": {"thinkingLevel": "minimal"},
        },
    }
    data = json.dumps(payload).encode("utf-8")
    url = MODEL_URL + "?key=" + urllib.parse.quote(GEMINI_API_KEY, safe="")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=5.5) as response:
            raw = response.read()

        parsed = json.loads(raw.decode("utf-8"))
        parts = parsed["candidates"][0]["content"]["parts"]

        chunks = []
        for part in parts:
            # Reasoning parts must never be spoken aloud.
            if part.get("thought"):
                continue
            value = part.get("text")
            if value:
                chunks.append(value)

        answer = " ".join(chunks).strip()
        if not answer:
            raise ValueError("model returned no usable text")
        return answer

    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        TimeoutError,
        socket.timeout,
        json.JSONDecodeError,
    ) as exc:
        print(f"[alexa-toby] model call failed: {exc!r}", file=sys.stderr, flush=True)
        return FALLBACK
    except Exception as exc:  # Safety net: the handler must never raise.
        print(
            f"[alexa-toby] unexpected model error: {exc!r}",
            file=sys.stderr,
            flush=True,
        )
        return FALLBACK
    finally:
        if time.monotonic() > deadline:
            print(
                "[alexa-toby] deadline exceeded during model call",
                file=sys.stderr,
                flush=True,
            )


def _run_openssl(args, timeout=3.0):
    """Run the openssl binary, returning None when it is missing or broken."""
    try:
        return subprocess.run(
            ["openssl", *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _download_chain(cert_url):
    """Fetch a PEM chain, using an in-memory cache, and return (text, error)."""
    now = time.monotonic()
    with CHAIN_CACHE_LOCK:
        cached = CHAIN_CACHE.get(cert_url)
        if cached and now < cached[0]:
            return cached[1], ""

    try:
        opener = urllib.request.build_opener(_NoRedirect)
        with opener.open(cert_url, timeout=3.5) as response:
            raw = response.read(MAX_CERT_BYTES + 1)
    except Exception:
        return None, "certificate fetch failed"

    if len(raw) > MAX_CERT_BYTES:
        return None, "certificate chain too large"

    text = raw.decode("utf-8", "replace")
    with CHAIN_CACHE_LOCK:
        CHAIN_CACHE[cert_url] = (
            time.monotonic() + CHAIN_CACHE_TTL,
            text,
        )
    return text, ""


def _validate_cert_url(cert_url):
    """Return an error string when the certificate URL is not a safe S3 URL."""
    try:
        parsed = urllib.parse.urlparse(cert_url)
        port = parsed.port
    except (TypeError, ValueError):
        return "invalid certificate url"

    if parsed.scheme != "https":
        return "certificate url is not https"
    if parsed.hostname != CERT_HOST:
        return "certificate url host is not s3.amazonaws.com"
    if not parsed.path.startswith(CERT_PATH_PREFIX):
        return "certificate url path is not under /echo.api/"
    if port is not None and port != 443:
        return "certificate url port is not 443"
    return ""


def _check_timestamp(body_bytes):
    """Reject a request whose request.timestamp is too far from now."""
    try:
        body = json.loads(body_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return True, ""

    if not isinstance(body, dict):
        return True, ""
    request = body.get("request")
    if not isinstance(request, dict):
        return True, ""
    timestamp = request.get("timestamp")
    if not isinstance(timestamp, str) or not timestamp.strip():
        return True, ""

    value = timestamp.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"

    try:
        when = datetime.datetime.fromisoformat(value)
    except ValueError:
        return True, ""

    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)

    now = datetime.datetime.now(datetime.timezone.utc)
    if abs((now - when).total_seconds()) > REPLAY_WINDOW_SECONDS:
        return False, "timestamp outside replay window"
    return True, ""


def verify_amazon_signature(cert_url, signature_b64, body_bytes):
    """Verify an Alexa request signature, returning (ok, reason)."""
    url_error = _validate_cert_url(cert_url)
    if url_error:
        return False, url_error

    chain, fetch_error = _download_chain(cert_url)
    if chain is None:
        return False, fetch_error

    blocks = re.findall(
        r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
        chain,
        re.DOTALL,
    )
    if not blocks:
        return False, "no certificate in chain"
    leaf = blocks[0]

    temp_paths = []

    def _temp_file(data):
        fd, path = tempfile.mkstemp()
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
        except OSError:
            try:
                os.unlink(path)
            except OSError:
                pass
            raise
        temp_paths.append(path)
        return path

    try:
        cert_path = _temp_file((leaf + "\n").encode("utf-8"))

        result = _run_openssl(["x509", "-noout", "-text", "-in", cert_path])
        if result is None or result.returncode != 0:
            return False, "could not read certificate"
        cert_text = result.stdout.decode("utf-8", "replace")
        san_match = re.search(
            r"Subject Alternative Name:\s*(.*?)(?:\n\s*\n|\Z)",
            cert_text,
            re.DOTALL,
        )
        san = san_match.group(1) if san_match else ""
        if "echo-api.amazon.com" not in san:
            return False, "certificate does not cover echo-api.amazon.com"

        result = _run_openssl(
            ["x509", "-noout", "-checkend", "0", "-in", cert_path]
        )
        if result is None:
            return False, "could not check certificate expiry"
        if result.returncode != 0:
            return False, "certificate is expired or not yet valid"

        result = _run_openssl(
            ["x509", "-pubkey", "-noout", "-in", cert_path]
        )
        if result is None or result.returncode != 0 or not result.stdout.strip():
            return False, "could not extract certificate public key"
        pubkey_path = _temp_file(result.stdout)

        try:
            signature = base64.b64decode(signature_b64, validate=True)
        except (TypeError, ValueError):
            return False, "invalid signature header"
        if not signature:
            return False, "invalid signature header"

        signature_path = _temp_file(signature)
        body_path = _temp_file(body_bytes)

        for algorithm in ("-sha256", "-sha1"):
            result = _run_openssl(
                [
                    "dgst",
                    algorithm,
                    "-verify",
                    pubkey_path,
                    "-signature",
                    signature_path,
                    body_path,
                ]
            )
            if result is not None and result.returncode == 0:
                return True, "verified"
        return False, "signature verification failed"
    finally:
        for path in temp_paths:
            try:
                os.unlink(path)
            except OSError:
                pass


def authorize(headers, body_bytes):
    """Decide whether a request may proceed, returning (allowed, status, reason)."""
    provided_key = headers.get("X-Api-Key")
    if API_KEY and provided_key == API_KEY:
        return True, 200, "api-key"

    cert_url = headers.get("SignatureCertChainUrl")
    signature = headers.get("Signature")
    if cert_url and signature:
        ok, reason = verify_amazon_signature(cert_url, signature, body_bytes)
        if ok:
            ok, reason = _check_timestamp(body_bytes)
            if ok:
                return True, 200, "signature"
            # A valid signature proves origin; an old timestamp only proves replay.
            if ENFORCE_SIGNATURE:
                return False, 401, reason
            print(
                f"[alexa-toby] signature ok but {reason} (not enforced)",
                flush=True,
            )
            return True, 200, "signature-replay-soft"
        if ENFORCE_SIGNATURE:
            return False, 401, reason
        # Soft mode. Real Alexa traffic always carries these headers, so a bug in
        # the verifier must never be able to take the skill down. Log loudly and
        # keep serving; flip ENFORCE_SIGNATURE once the log shows clean passes.
        print(
            f"[alexa-toby] signature verification failed (soft mode): {reason}",
            flush=True,
        )
        return True, 200, "signature-soft-fail"

    if ENFORCE_SIGNATURE:
        return False, 401, "no credentials"
    if API_KEY:
        return False, 401, "no credentials"
    return True, 200, "open"


class AlexaHandler(BaseHTTPRequestHandler):
    """HTTP handler for health checks and Alexa skill requests."""

    protocol_version = "HTTP/1.1"
    server_version = "AlexaToby/1.0"

    def log_message(self, fmt, *args):
        """Silence the default stderr chatter; we log requests ourselves."""
        return

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _not_found(self):
        self._send_json(404, {"error": "not found"})

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return b""
        if length <= 0:
            return b""
        try:
            return self.rfile.read(length)
        except (ConnectionError, OSError):
            return b""

    def _log_request(self, req_type, intent_name, question, start, answer_len):
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
        elapsed = time.monotonic() - start
        snippet = (question or "").replace("\n", " ")[:80]
        line = (
            f"{timestamp} type={req_type or '-'} intent={intent_name or '-'} "
            f"q={snippet!r} elapsed={elapsed:.2f}s answer_len={answer_len}"
        )
        with LOG_LOCK:
            print(line, flush=True)

    def do_GET(self):
        start = time.monotonic()
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._send_json(200, {"status": "ok"})
        else:
            self._not_found()
        self._log_request("GET", "-", "", start, 0)

    def do_POST(self):
        start = time.monotonic()
        deadline = start + 6.0
        raw = self._read_body()

        path = self.path.split("?", 1)[0]
        if path not in ("/alexa", "/"):
            self._not_found()
            self._log_request("POST", "-", "", start, 0)
            return

        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"error": "bad request"})
            self._log_request("POST", "-", "", start, 0)
            return

        if not isinstance(body, dict):
            self._send_json(400, {"error": "bad request"})
            self._log_request("POST", "-", "", start, 0)
            return

        session = body.get("session") or {}
        if not isinstance(session, dict):
            session = {}
        application = session.get("application") or {}
        if not isinstance(application, dict):
            application = {}
        app_id = application.get("applicationId", "")

        if SKILL_ID and app_id != SKILL_ID:
            self._send_json(400, {"error": "bad application id"})
            self._log_request("POST", "-", "", start, 0)
            return

        allowed, status, reason = authorize(self.headers, raw)
        if not allowed:
            self._send_json(status, {"error": "unauthorized"})
            print(
                f"[alexa-toby] unauthorized request: {reason}",
                flush=True,
            )
            self._log_request("POST", "-", "", start, 0)
            return

        request = body.get("request") or {}
        if not isinstance(request, dict):
            request = {}
        req_type = request.get("type") or ""
        intent_name = "-"
        question = ""
        answer_len = 0

        try:
            if req_type == "LaunchRequest":
                result = speech_response(
                    "Toby here. What do you want to know?",
                    end_session=False,
                    reprompt="Ask me anything.",
                )
            elif req_type == "SessionEndedRequest":
                result = {"version": "1.0", "response": {}}
            elif req_type == "IntentRequest":
                intent = request.get("intent") or {}
                if not isinstance(intent, dict):
                    intent = {}
                intent_name = intent.get("name") or "-"

                if intent_name == "AskIntent":
                    slots = intent.get("slots") or {}
                    if not isinstance(slots, dict):
                        slots = {}
                    slot = slots.get("question") or {}
                    if not isinstance(slot, dict):
                        slot = {}
                    question = (slot.get("value") or "").strip()

                    if not question:
                        result = speech_response("I did not catch the question.", True)
                    else:
                        answer = ask_model(question, deadline)
                        speech = sanitise(answer)
                        answer_len = len(speech)
                        result = speech_response(speech, True)
                elif intent_name == "AMAZON.HelpIntent":
                    result = speech_response(
                        "Ask me a question. Say, ask Toby AI, followed by what "
                        "you want to know.",
                        True,
                    )
                elif intent_name in (
                    "AMAZON.StopIntent",
                    "AMAZON.CancelIntent",
                    "AMAZON.NavigateHomeIntent",
                ):
                    result = speech_response("Okay.", True)
                else:
                    result = speech_response("Sorry, I did not get that.", True)
            else:
                result = speech_response("Sorry, I did not get that.", True)
        except Exception as exc:  # Final guard: never let a request crash us.
            print(f"[alexa-toby] handler error: {exc!r}", file=sys.stderr, flush=True)
            result = speech_response(FALLBACK, True)
            answer_len = len(FALLBACK)

        self._send_json(200, result)
        self._log_request(req_type, intent_name, question, start, answer_len)

    def do_HEAD(self):
        self._not_found()

    def do_PUT(self):
        self._not_found()

    def do_DELETE(self):
        self._not_found()

    def do_PATCH(self):
        self._not_found()

    def do_OPTIONS(self):
        self._not_found()


def _load_api_key():
    """Read the Gemini key from the environment, then from the dotenv file."""
    key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if key:
        return key

    try:
        with open(ENV_FILE, "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("GEMINI_API_KEY="):
                    value = line.split("=", 1)[1]
                    value = value.strip().strip('"').strip("'")
                    value = value.rstrip("\r").strip()
                    return value
    except OSError as exc:
        print(
            f"[alexa-toby] could not read {ENV_FILE}: {exc!r}",
            file=sys.stderr,
            flush=True,
        )

    return ""


def main():
    global PORT, SKILL_ID, GEMINI_API_KEY, API_KEY, ENFORCE_SIGNATURE

    GEMINI_API_KEY = _load_api_key()
    if not GEMINI_API_KEY:
        print(
            "[alexa-toby] ERROR: no Gemini API key found; set GEMINI_API_KEY "
            f"or add GEMINI_API_KEY=... to {ENV_FILE}",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(1)

    try:
        PORT = int(os.environ.get("PORT", str(DEFAULT_PORT)))
    except ValueError:
        PORT = DEFAULT_PORT

    SKILL_ID = (os.environ.get("SKILL_ID") or "").strip()
    API_KEY = (os.environ.get("API_KEY") or "").strip()
    ENFORCE_SIGNATURE = (
        os.environ.get("ENFORCE_SIGNATURE", "0").strip().lower()
        in {"1", "true", "yes", "on"}
    )

    server = ThreadingHTTPServer(("0.0.0.0", PORT), AlexaHandler)
    print(
        f"[alexa-toby] listening on 0.0.0.0:{PORT} key_found=yes "
        f"api_key_set={bool(API_KEY)} enforce_signature={ENFORCE_SIGNATURE}",
        flush=True,
    )

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[alexa-toby] shutting down", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
