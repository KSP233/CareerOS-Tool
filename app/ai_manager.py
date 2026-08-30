from __future__ import annotations

import json
import ipaddress
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from app.database import Database
from app.secrets import protect_secret, unprotect_secret
from app.validators import ValidationError, extract_json
from config import load_settings, save_settings


class AIError(RuntimeError):
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

    def _generate_api(self, system_prompt: str, user_prompt: str, temperature: float) -> tuple[dict, str]:
        api = self.settings.get("api", {})
        model = str(api.get("model", "")).strip()
        encrypted_key = str(api.get("encrypted_key", "")); key = unprotect_secret(encrypted_key)
        if not model or not key:
            raise AIError("API model or key is not configured")
        if encrypted_key and not encrypted_key.startswith("dpapi:"):
            # One-time migration from the old shared Fernet-key format. The old
            # key file is never copied into a portable build.
            self.settings.setdefault("api", {})["encrypted_key"] = protect_secret(key)
            save_settings(self.settings)
        base = validated_endpoint(str(api.get("base_url", "")), local_only=False)
        endpoint = base if base.endswith("/chat/completions") else base + "/chat/completions"
        body = {
            "model": model,
            "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            "temperature": temperature,
            "response_format": {"type": "json_object"},
        }
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        )
        # validated_endpoint permits HTTPS or loopback HTTP only.
        with urllib.request.urlopen(request, timeout=240) as response:  # nosec B310
            result = json.load(response)
        content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        return extract_json(content), f"API: {model}"

    def generate_json(self, task: str, system_prompt: str, user_prompt: str, selected: str | None = None, temperature: float = 0.25) -> tuple[dict, str]:
        errors = []
        for model in self._route(task, selected):
            started = time.perf_counter()
            try:
                if model == "api":
                    parsed, model_used = self._generate_api(system_prompt, user_prompt, temperature)
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
            except (OSError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValidationError, json.JSONDecodeError, AIError, ValueError) as exc:
                errors.append(f"{model}: {exc}")
                self.db.record_ai(task, model, int((time.perf_counter() - started) * 1000), False, str(exc))
        raise AIError("; ".join(errors) or "No AI model available")
