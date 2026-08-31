from __future__ import annotations

import hashlib
import json
import ipaddress
import logging
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from urllib.parse import urlsplit

from app.database import Database
from app.secrets import protect_secret, unprotect_secret
from app.validators import ValidationError, extract_json
from config import load_settings, save_settings


logger = logging.getLogger(__name__)
EXTERNAL_API_REQUEST_TIMEOUT_SECONDS = 300
EXTERNAL_API_MAX_ATTEMPTS = 3
EXTERNAL_API_RETRY_DELAYS_SECONDS = (2.0, 5.0)
_TRANSIENT_HTTP_STATUS = {429, 502, 503, 504}
# This intentionally small allow-list avoids silently fetching an arbitrary
# model from the Internet. More models can be added only with an explicit
# product decision, size estimate, and licence review.
LOCAL_MODEL_CATALOG = {
    "llama3.2:1b": {"label": "Llama 3.2 1B", "size": "about 1.3 GB", "vram": "approx. 2 GB VRAM (or 8 GB RAM on CPU)", "description": "Lowest storage use; suitable for quick simple tasks."},
    "llama3.2:3b": {"label": "Llama 3.2 3B", "size": "about 2 GB", "vram": "approx. 4 GB VRAM (or 8 GB RAM on CPU)", "description": "Balanced local general-purpose model."},
    "gemma3:4b": {"label": "Gemma 3 4B", "size": "about 3.3 GB", "vram": "approx. 6 GB VRAM (or 12 GB RAM on CPU)", "description": "Stronger general model; requires Ollama 0.6 or later."},
    "qwen3.5:4b": {"label": "Qwen 3.5 4B", "size": "about 3.4 GB", "vram": "approx. 6 GB VRAM (or 12 GB RAM on CPU)", "description": "Balanced long-context local model."},
    "qwen3.5:9b": {"label": "Qwen 3.5 9B", "size": "about 6.6 GB", "vram": "approx. 10 GB VRAM (or 16 GB RAM on CPU)", "description": "Higher quality; needs substantially more RAM or GPU memory."},
    "gpt-oss:20b": {"label": "GPT-OSS 20B", "size": "about 14 GB", "vram": "approx. 16 GB VRAM or unified memory", "description": "Open-weight GPT model; stronger reasoning, but slower on modest hardware."},
    "gpt-oss:120b": {"label": "GPT-OSS 120B", "size": "about 65 GB", "vram": "approx. 80 GB VRAM or unified memory", "description": "High-end workstation/server model; not recommended for ordinary PCs."},
}


class AIError(RuntimeError):
    pass


class AICancelled(AIError):
    pass


def validated_endpoint(value: str, *, local_only: bool = False) -> str:
    """Allow HTTPS endpoints and loopback HTTP; never send secrets to plain remote HTTP."""
    raw = str(value or "").strip().rstrip("/")
    parsed = urlsplit(raw)
    host = (parsed.hostname or "").casefold().rstrip(".")
    if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
        raise AIError("AI endpoint must be a valid HTTP(S) URL without embedded credentials")
    try: loopback = ipaddress.ip_address(host).is_loopback
    except ValueError: loopback = host == "localhost"
    if local_only and not loopback:
        raise AIError("Local AI endpoint must use localhost or a loopback IP address")
    if parsed.scheme != "https" and not loopback:
        raise AIError("External AI endpoint must use HTTPS")
    return raw


def external_chat_endpoint(value: str) -> str:
    """Normalize an OpenAI-compatible Base URL exactly once."""
    base = validated_endpoint(value, local_only=False)
    return base if base.endswith("/chat/completions") else base + "/chat/completions"


def _header(headers, *names: str) -> str:
    for name in names:
        value = headers.get(name)
        if value:
            return str(value)
    return ""


def _safe_error_detail(raw: bytes) -> str:
    """Return a short provider error without request data or credentials."""
    try:
        parsed = json.loads(raw.decode("utf-8", "replace"))
        error = parsed.get("error", parsed) if isinstance(parsed, dict) else {}
        detail = error.get("message", "") if isinstance(error, dict) else ""
        return str(detail).replace("\r", " ").replace("\n", " ")[:300]
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError, TypeError):
        return ""


def _retry_after_seconds(headers) -> float | None:
    value = _header(headers, "Retry-After")
    try:
        return min(60.0, max(0.0, float(value))) if value else None
    except ValueError:
        return None


def _wait_with_cancellation(seconds: float, cancelled: Callable[[], bool] | None) -> None:
    deadline = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < deadline:
        if cancelled and cancelled():
            raise AICancelled("AI request cancelled")
        time.sleep(min(.2, max(.01, deadline - time.monotonic())))


class AIManager:
    def __init__(self, database: Database, settings: dict | None = None):
        self.db = database
        self.settings = settings or load_settings()
        self.base_url = self.settings["ollama_url"].rstrip("/")

    def reload_settings(self) -> None:
        self.settings = load_settings()
        self.base_url = self.settings["ollama_url"].rstrip("/")

    def available_models(self) -> list[str]:
        try:
            base = validated_endpoint(self.base_url, local_only=True)
            # validated_endpoint restricts this request to loopback HTTP(S).
            with urllib.request.urlopen(f"{base}/api/tags", timeout=3) as response:  # nosec B310
                data = json.load(response)
            return [m["name"] for m in data.get("models", [])]
        except Exception:
            return []

    def local_runtime_info(self) -> dict[str, object]:
        """Return only local Ollama availability; this never contacts a cloud provider."""
        try:
            base = validated_endpoint(self.base_url, local_only=True)
            with urllib.request.urlopen(f"{base}/api/version", timeout=3) as response:  # nosec B310
                version = str(json.load(response).get("version") or "unknown")
            return {"available": True, "version": version, "models": self.available_models()}
        except Exception as exc:
            logger.info("ollama_unavailable error_type=%s", type(exc).__name__)
            return {"available": False, "version": "", "models": []}

    @staticmethod
    def local_model_catalog() -> dict[str, dict[str, str]]:
        return {name: dict(metadata) for name, metadata in LOCAL_MODEL_CATALOG.items()}

    def install_ollama(self, progress: Callable[[str], None] = lambda _message: None, cancelled: Callable[[], bool] = lambda: False) -> dict[str, str]:
        """Install Ollama through Windows Package Manager after explicit UI confirmation."""
        if os.name != "nt":
            raise AIError("Automatic Ollama installation is currently available only on Windows.")
        winget = shutil.which("winget")
        if not winget:
            raise AIError("Windows Package Manager is unavailable. Use the official Ollama download page instead.")
        progress("Windows is downloading and installing Ollama…")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            process = subprocess.Popen(
                [winget, "install", "--id", "Ollama.Ollama", "--exact", "--accept-package-agreements", "--accept-source-agreements", "--disable-interactivity"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags,
            )
            while process.poll() is None:
                if cancelled():
                    process.terminate()
                    raise AICancelled("Ollama installation cancelled")
                time.sleep(0.25)
        except AICancelled:
            raise
        except OSError as exc:
            raise AIError("CareerOS could not start Windows Package Manager. Use the official Ollama download page instead.") from exc
        if process.returncode not in {0, 3010}:
            raise AIError("Ollama installation did not finish. Use the official Ollama download page or check Windows Package Manager.")
        logger.info("ollama_install_completed return_code=%s", process.returncode)
        return {"status": "installed"}

    def pull_local_model(self, model: str, progress: Callable[[str], None] = lambda _message: None, cancelled: Callable[[], bool] = lambda: False) -> dict[str, str]:
        """Download a reviewed model through the local Ollama service after explicit UI consent."""
        if model not in LOCAL_MODEL_CATALOG:
            raise ValueError("CareerOS can download only a reviewed local model from its built-in list.")
        base = validated_endpoint(self.base_url, local_only=True)
        request = urllib.request.Request(
            f"{base}/api/pull", data=json.dumps({"name": model, "stream": True}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        logger.info("ollama_pull_started model=%s", model)
        last_status = "Starting download"
        try:
            with urllib.request.urlopen(request, timeout=900) as response:  # nosec B310
                for raw_line in response:
                    if cancelled():
                        raise AICancelled("Local model download cancelled")
                    try:
                        event = json.loads(raw_line.decode("utf-8", "replace"))
                    except json.JSONDecodeError:
                        continue
                    if event.get("error"):
                        raise AIError("Ollama could not download this local model. Check the Ollama application and your network connection.")
                    last_status = str(event.get("status") or last_status)
                    total, completed = int(event.get("total") or 0), int(event.get("completed") or 0)
                    if total > 0:
                        progress(f"Downloading {model}: {min(100, round(completed * 100 / total))}%")
                    else:
                        progress(f"Downloading {model}: {last_status}")
        except AICancelled:
            logger.info("ollama_pull_cancelled model=%s", model)
            raise
        except (OSError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            logger.warning("ollama_pull_failed model=%s error_type=%s", model, type(exc).__name__)
            raise AIError("Ollama is not available. Install or start Ollama, then try the download again.") from exc
        logger.info("ollama_pull_completed model=%s", model)
        return {"model": model, "status": last_status}

    def delete_local_model(self, model: str) -> None:
        """Delete only a model confirmed as installed by the local service."""
        if model not in self.available_models():
            raise ValueError("The selected model is not installed locally.")
        base = validated_endpoint(self.base_url, local_only=True)
        request = urllib.request.Request(
            f"{base}/api/delete", data=json.dumps({"name": model}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="DELETE",
        )
        try:
            with urllib.request.urlopen(request, timeout=30):  # nosec B310
                pass
        except (OSError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            raise AIError("CareerOS could not remove the local model. Make sure Ollama is running and try again.") from exc
        logger.info("ollama_model_deleted model=%s", model)

    def _route(self, task: str, selected: str | None = None) -> list[str]:
        selected = selected or self.settings.get("ai_mode", "Auto")
        models = self.settings["models"]
        if selected.startswith("API:"):
            return ["api"]
        if selected != "Auto":
            order = [selected]
            other = models["fast"] if selected == models["deep"] else models["deep"]
        elif task in {"extract", "summarize", "translate"}:
            order, other = [models["fast"]], models["deep"]
        else:
            order, other = [models["deep"]], models["fast"]
        if self.settings.get("fallback_enabled", True) and other not in order:
            order.append(other)
        return order[:2]

    def api_label(self) -> str | None:
        api = self.settings.get("api", {})
        if api.get("enabled") and api.get("model") and api.get("encrypted_key"):
            return f"API: {api['model']}"
        return None

    @staticmethod
    def _external_request_summary(request_id: str, endpoint: str, model: str, system_prompt: str, user_prompt: str) -> dict[str, object]:
        return {
            "request_id": request_id, "provider": "external-openai-compatible",
            "endpoint": endpoint, "model": model, "method": "POST", "stream": False,
            "message_count": 2, "input_chars": len(system_prompt) + len(user_prompt),
            "system_chars": len(system_prompt), "user_chars": len(user_prompt),
            "system_sha256": hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()[:12],
            "user_sha256": hashlib.sha256(user_prompt.encode("utf-8")).hexdigest()[:12],
            "timeout_seconds": EXTERNAL_API_REQUEST_TIMEOUT_SECONDS, "authorization_present": True,
        }

    def _external_post_json(self, endpoint: str, key: str, body: dict, *, request_id: str, system_prompt: str, user_prompt: str, progress: Callable[[str], None] | None = None, cancelled: Callable[[], bool] | None = None) -> dict:
        model = str(body.get("model") or "")
        logger.debug("external_api_request_start %s", self._external_request_summary(request_id, endpoint, model, system_prompt, user_prompt))
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        for attempt in range(1, EXTERNAL_API_MAX_ATTEMPTS + 1):
            if cancelled and cancelled():
                raise AICancelled("AI request cancelled")
            if progress:
                progress("Sending request..." if attempt == 1 else f"Retrying AI request ({attempt}/{EXTERNAL_API_MAX_ATTEMPTS})...")
            started = time.perf_counter()
            request = urllib.request.Request(endpoint, data=payload, headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}", "X-CareerOS-Request-Id": request_id})
            try:
                if progress:
                    progress("Waiting for AI response...")
                with urllib.request.urlopen(request, timeout=EXTERNAL_API_REQUEST_TIMEOUT_SECONDS) as response:  # nosec B310
                    raw = response.read(); status = getattr(response, "status", None); status = response.getcode() if status is None else status; headers = response.headers
                logger.debug("external_api_response request_id=%s attempt=%s status=%s elapsed_s=%.3f bytes=%s provider_request_id=%s", request_id, attempt, status, time.perf_counter() - started, len(raw), _header(headers, "x-request-id", "x-goog-request-id"))
                if progress:
                    progress("Processing response...")
                try:
                    return json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise AIError("The AI service returned malformed JSON.") from exc
            except urllib.error.HTTPError as exc:
                raw = exc.read(4096); detail = _safe_error_detail(raw); retry_after = _retry_after_seconds(exc.headers)
                logger.warning("external_api_http_error request_id=%s attempt=%s status=%s elapsed_s=%.3f retry_after=%s provider_request_id=%s detail=%s", request_id, attempt, exc.code, time.perf_counter() - started, retry_after, _header(exc.headers, "x-request-id", "x-goog-request-id"), detail or "<none>")
                if exc.code in _TRANSIENT_HTTP_STATUS and attempt < EXTERNAL_API_MAX_ATTEMPTS:
                    _wait_with_cancellation(retry_after if retry_after is not None else EXTERNAL_API_RETRY_DELAYS_SECONDS[attempt - 1], cancelled)
                    continue
                if exc.code in {401, 403}:
                    raise AIError("The API credentials were rejected. Please check your API key and provider settings.") from exc
                if exc.code == 404:
                    raise AIError("The configured AI endpoint or model could not be found. Please check the Base URL and model name.") from exc
                if exc.code == 429:
                    raise AIError("The AI service rate limit was reached after retrying. Please wait and try again.") from exc
                if exc.code == 503:
                    raise AIError("The AI service is temporarily unavailable after retrying. Please try again later.") from exc
                raise AIError(f"The AI service returned HTTP {exc.code}.") from exc
            except (socket.timeout, TimeoutError, urllib.error.URLError, ConnectionError, OSError) as exc:
                logger.warning("external_api_transport_error request_id=%s attempt=%s error_type=%s elapsed_s=%.3f message=%s", request_id, attempt, type(exc).__name__, time.perf_counter() - started, str(exc)[:300])
                if attempt < EXTERNAL_API_MAX_ATTEMPTS:
                    _wait_with_cancellation(EXTERNAL_API_RETRY_DELAYS_SECONDS[attempt - 1], cancelled)
                    continue
                raise AIError("The AI service did not respond in time or the network connection failed. Please retry or check your network connection.") from exc
        raise AIError("The external AI request did not complete.")

    @staticmethod
    def _external_content(result: dict, request_id: str) -> str:
        choices = result.get("choices") if isinstance(result, dict) else None
        if not isinstance(choices, list) or not choices:
            raise AIError("The AI service response did not include any choices.")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if not isinstance(message, dict):
            raise AIError("The AI service response did not include a message.")
        content = message.get("content")
        if isinstance(content, list):
            content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
        content = str(content or "").strip()
        if not content:
            raise AIError("The AI service returned an empty response.")
        logger.debug("external_api_content_ready request_id=%s content_chars=%s", request_id, len(content))
        return content

    def _generate_api(self, system_prompt: str, user_prompt: str, temperature: float, *, progress: Callable[[str], None] | None = None, cancelled: Callable[[], bool] | None = None) -> tuple[dict, str]:
        api = self.settings.get("api", {})
        model = str(api.get("model", "")).strip()
        encrypted_key = str(api.get("encrypted_key", ""))
        try:
            key = unprotect_secret(encrypted_key)
        except Exception as exc:
            logger.warning("external_api_key_unavailable format=%s error_type=%s", "dpapi" if encrypted_key.startswith("dpapi:") else "legacy", type(exc).__name__)
            raise AIError("The saved API key cannot be read. Re-enter it in Settings to protect it for this Windows account.") from exc
        if not model or not key:
            raise AIError("API model or key is not configured")
        if encrypted_key and not encrypted_key.startswith("dpapi:"):
            self.settings.setdefault("api", {})["encrypted_key"] = protect_secret(key)
            save_settings(self.settings)
        endpoint = external_chat_endpoint(str(api.get("base_url", "")))
        request_id = "ext-" + uuid.uuid4().hex[:12]
        body = {"model": model, "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}], "temperature": temperature, "response_format": {"type": "json_object"}, "stream": False}
        result = self._external_post_json(endpoint, key, body, request_id=request_id, system_prompt=system_prompt, user_prompt=user_prompt, progress=progress, cancelled=cancelled)
        content = self._external_content(result, request_id)
        logger.debug("external_api_structured_parse_start request_id=%s", request_id)
        parsed = extract_json(content)
        logger.debug("external_api_structured_parse_end request_id=%s keys=%s", request_id, sorted(parsed.keys()))
        return parsed, f"API: {model}"

    def external_smoke_test(self) -> dict[str, object]:
        """Send a minimal request without resume, job, GUI, or database content."""
        api = self.settings.get("api", {})
        model = str(api.get("model", "")).strip(); encrypted_key = str(api.get("encrypted_key", ""))
        try:
            key = unprotect_secret(encrypted_key)
        except Exception as exc:
            raise AIError("The saved API key cannot be read. Re-enter it in Settings to protect it for this Windows account.") from exc
        if not model or not key:
            raise AIError("API model or key is not configured")
        endpoint = external_chat_endpoint(str(api.get("base_url", ""))); system_prompt, user_prompt = "You are a test assistant.", "Reply with exactly: OK"; request_id = "smoke-" + uuid.uuid4().hex[:12]; started = time.perf_counter()
        result = self._external_post_json(endpoint, key, {"model": model, "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}], "stream": False}, request_id=request_id, system_prompt=system_prompt, user_prompt=user_prompt)
        return {"request_id": request_id, "endpoint": endpoint, "model": model, "elapsed_seconds": round(time.perf_counter() - started, 3), "content": self._external_content(result, request_id)}

    def generate_json(self, task: str, system_prompt: str, user_prompt: str, selected: str | None = None, temperature: float = 0.25, *, progress: Callable[[str], None] | None = None, cancelled: Callable[[], bool] | None = None) -> tuple[dict, str]:
        errors = []
        for model in self._route(task, selected):
            started = time.perf_counter()
            try:
                if model == "api":
                    parsed, model_used = self._generate_api(system_prompt, user_prompt, temperature, progress=progress, cancelled=cancelled)
                    self.db.record_ai(task, model_used, int((time.perf_counter() - started) * 1000), True)
                    return parsed, model_used
                request_body = {
                    "model": model,
                    "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
                    "stream": False,
                    "format": "json",
                    "options": {"temperature": temperature, "num_ctx": 12288},
                }
                # Current gpt-oss Ollama builds can return an empty content field when
                # thinking is explicitly disabled. Let that model use its native mode;
                # keep thinking disabled for the faster model to reduce latency.
                if not model.casefold().startswith("gpt-oss"):
                    request_body["think"] = False
                payload = json.dumps(request_body).encode()
                base = validated_endpoint(self.base_url, local_only=True)
                request = urllib.request.Request(f"{base}/api/chat", data=payload, headers={"Content-Type": "application/json"})
                # validated_endpoint restricts this request to loopback HTTP(S).
                with urllib.request.urlopen(request, timeout=240) as response:  # nosec B310
                    result = json.load(response)
                content = str(result.get("message", {}).get("content", "")).strip()
                try:
                    parsed = extract_json(content)
                except json.JSONDecodeError:
                    # Some local models translate correctly but ignore Ollama's
                    # JSON-format hint. Translation has a single unambiguous
                    # string field, so retain that local output instead of
                    # failing the entire action.
                    if task == "translate" and content:
                        parsed = {"translation": content}
                    else:
                        raise
                self.db.record_ai(task, model, int((time.perf_counter() - started) * 1000), True)
                return parsed, model
            except AICancelled:
                logger.info("ai_request_cancelled task=%s model=%s", task, model)
                raise
            except (OSError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValidationError, json.JSONDecodeError, AIError, ValueError) as exc:
                errors.append(f"{model}: {exc}")
                self.db.record_ai(task, model, int((time.perf_counter() - started) * 1000), False, str(exc))
        raise AIError("; ".join(errors) or "No AI model available")
