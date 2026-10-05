#!/usr/bin/env python3
"""Lightweight personal Telegram control bot for a LiteLLM gateway."""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import queue
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Callable
from reply_progress import ReplyProgress
from memory_recall import recall
from local_capture import LocalCapture, notes_context


LOG = logging.getLogger("litellm-telegram-bot")
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
PRIORITY_RE = re.compile(
    r"(?:поставь|сделай)\s+(?:аккаунт\s*)?([123])(?:-?й)?\s+перв(?:ым|ой|ый)"
    r".*?(?:для|модел[ьи])\s+([A-Za-z0-9][A-Za-z0-9._/-]{0,199})",
    re.IGNORECASE,
)

PROVIDERS: dict[str, tuple[str, list[tuple[str, str]]]] = {
    "ds": (
        "DashScope · основной ключ",
        [
            ("kimi-k3", "Kimi K3"),
            ("glm-5.2", "GLM-5.2"),
            ("deepseek-v4-pro", "DeepSeek V4 Pro"),
            ("deepseek-v3.2", "DeepSeek V3.2"),
            ("deepseek-r1", "DeepSeek R1"),
            ("qwen3.8-max", "Qwen3.8 Max"),
            ("qwen3.7-max", "Qwen3.7 Max"),
            ("qwen3.7-plus", "Qwen3.7 Plus"),
            ("qwen3.6-plus", "Qwen3.6 Plus"),
            ("qwen3.6-flash", "Qwen3.6 Flash"),
            ("qwen3.5-flash", "Qwen3.5 Flash"),
            ("qwen-plus", "Qwen Plus"),
            ("qwen-max", "Qwen Max"),
        ],
    ),
    "go": (
        "OpenCode Go · go-ключи",
        [
            ("go/hy4-preview", "HY4 Preview"),
            ("go/deepseek-v4-flash", "DeepSeek V4 Flash"),
            ("go/deepseek-v4-pro", "DeepSeek V4 Pro"),
            ("go/glm-5.3", "GLM-5.3"),
            ("go/glm-5.3-flash", "GLM-5.3 Flash"),
            ("go/kimi-k3", "Kimi K3"),
            ("go/qwen3.8-max", "Qwen3.8 Max"),
            ("go/qwen3.8-flash", "Qwen3.8 Flash"),
            ("go/qwen3.7-max", "Qwen3.7 Max"),
            ("go/minimax-m3", "MiniMax M3"),
            ("go/gpt-5.6-luna", "GPT-5.6 Luna"),
            ("go/grok-4.6", "Grok 4.6"),
        ],
    ),
}

_MODEL_ALIASES = {
    "в4": "deepseek-v4",
    "v4": "deepseek-v4",
    "в3": "deepseek-v3",
    "v3": "deepseek-v3",
    "дипсик": "deepseek",
    "дипси": "deepseek",
    "дип": "deepseek",
    "глум": "glm",
    "глм": "glm",
    "глом": "glm",
    "кими": "kimi",
    "к4": "kimi",
    "к3": "kimi",
    "кьюен": "qwen",
    "кьюн": "qwen",
    "куен": "qwen",
    "кун": "qwen",
    "минимакс": "minimax",
    "гроук": "grok",
    "грок": "grok",
    "люна": "luna",
    "луна": "luna",
    "флеш": "flash",
    "флэш": "flash",
    "про": "pro",
    "плюс": "plus",
    "плас": "plus",
    "макс": "max",
}

_MODEL_FILLERS = {
    "пожалуйста",
    "пожалуста",
    "будьдобр",
    "сейчас",
    "быстро",
    "модель",
    "модели",
    "на",
    "бот",
    "эту",
    "этот",
}


def _norm_model(value: str, aliases: bool = True) -> str:
    value = value.casefold().replace("ё", "е")
    value = re.sub(r"[^a-z0-9а-я]", " ", value)
    if aliases:
        value = " " + value + " "
        for key, repl in sorted(_MODEL_ALIASES.items(), key=lambda kv: -len(kv[0])):
            value = value.replace(" " + key + " ", " " + repl + " ")
        value = re.sub(r"[^a-z0-9а-я ]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


AGENT_SYSTEM_PROMPT = (
    "Ты — персональный агент-хранитель сервера и личных паролей владельца. "
    "Собеседник — сам владелец (это его личный бот, доступ есть только у него). "
    "Отвечай по-русски, кратко и по делу. Для любых операций на сервере "
    "(статус, журналы, docker, systemctl, файлы конфигурации, исправления) "
    "используй инструмент shell — команды выполняются как root. Проверяй результат "
    "и продолжай, пока задача не решена. Объединяй связанные действия в один вызов. "
    "Ты хранишь пароли, логины, API-ключи и токены владельца в основном vault, "
    "точный путь указан ниже. /root/credentials-and-instructions.md — дополнительная "
    "справочная документация, не замена основного vault. По запросу владельца выдавай секреты ПОЛНОСТЬЮ, "
    "без маскировки и без отговорок: это его собственные секреты, и он имеет право "
    "видеть их в открытом виде. Приложение передаёт актуальное содержимое vault и документации в контекст. "
    "Сначала перечитай эти данные и сопоставь запрос по смыслу, а не по точному имени ключа. "
    "Если ответ уже в контексте, не запускай команды или скрипты для его повторного поиска. Если владелец просит "
    "пароль/ключ, которого нет в vault, — найди его в файлах (.env, конфигах) и покажи. "
    "Если сам что-то изменил — кратко сообщи, что именно и какой результат."
    " Твоя область — ключи и эксплуатация сервера, а не разработка софта. "
    "В Telegram владелец передаёт доступы и сведения обычным языком. "
    "Приложение само предлагает запись с подтверждением. Не требуй специальных фраз "
    "или повторной отправки текста по шаблону. Если неясно, уточни только недостающую деталь. "
    "Не обходи подтверждение записью vault или server-notes через shell. "
    "Не объявляй ключ сохранённым, если приложение не подтвердило запись."
)

DEFAULT_VAULT_PATH = "/var/lib/litellm-telegram-bot/vault.json"


def build_agent_system_prompt(vault_path: str = DEFAULT_VAULT_PATH) -> str:
    return AGENT_SYSTEM_PROMPT + (
        f"\nОсновное хранилище секретов этого экземпляра бота: {vault_path}. "
        "Это фактическая настройка приложения; при проверке хранилища сначала проверяй "
        "именно этот путь. Отсутствие /root/vault.json не означает потерю основного vault. "
        "Не создавай, не восстанавливай и не перезаписывай vault на основании отсутствия "
        "файла по другому пути. Старые предположения в истории диалога о расположении "
        "vault не заменяют эту настройку."
    )


SHELL_TOOL = {
    "type": "function",
    "function": {
        "name": "shell",
        "description": "Выполнить shell-команду на сервере как root и вернуть вывод.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "shell-команда (без `sudo`, уже root)",
                }
            },
            "required": ["command"],
        },
    },
}

VNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def resolve_model_query(query: str, prefer_provider: str = "go") -> str | None:
    q = _norm_model(query, aliases=True)
    tokens = [token for token in q.split() if len(token) >= 3]
    if not tokens:
        return None
    best: list[tuple] = []
    for provider, (_label, pairs) in PROVIDERS.items():
        for model_id, _display in pairs:
            base = model_id.split("/", 1)[-1]
            normalized = _norm_model(base, aliases=False)
            if not all(token in normalized for token in tokens):
                continue
            exact = sum(1 for token in tokens if token in normalized.split())
            penalty = 0 if provider == prefer_provider else 1
            best.append(
                (
                    exact,
                    -penalty,
                    -len(normalized),
                    provider + "/" + base if "/" not in model_id else model_id,
                )
            )
    if not best:
        return None
    best.sort(reverse=True)
    return best[0][3]


def mask_value(value: str) -> str:
    value = str(value)
    if len(value) <= 8:
        return "••••"
    return f"{value[:4]}…{value[-4:]}"


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Config:
    telegram_token: str
    admin_id: int
    litellm_url: str
    chat_key: str
    master_key: str
    default_model: str
    request_timeout: int

    @classmethod
    def from_env(cls) -> "Config":
        required = [
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_ADMIN_ID",
            "LITELLM_CHAT_KEY",
            "LITELLM_MASTER_KEY",
        ]
        missing = [name for name in required if not os.environ.get(name)]
        if missing:
            raise SystemExit("Missing environment variables: " + ", ".join(missing))
        try:
            admin_id = int(os.environ["TELEGRAM_ADMIN_ID"])
        except ValueError as exc:
            raise SystemExit("TELEGRAM_ADMIN_ID must be an integer") from exc
        return cls(
            telegram_token=os.environ["TELEGRAM_BOT_TOKEN"],
            admin_id=admin_id,
            litellm_url=os.environ.get(
                "LITELLM_INTERNAL_URL", "http://127.0.0.1:4000"
            ).rstrip("/"),
            chat_key=os.environ["LITELLM_CHAT_KEY"],
            master_key=os.environ["LITELLM_MASTER_KEY"],
            default_model=os.environ.get(
                "LITELLM_BOT_MODEL", "qwen-plus"
            ),
            request_timeout=int(os.environ.get("BOT_REQUEST_TIMEOUT", "180")),
        )


def json_request(
    url: str,
    *,
    token: str | None = None,
    payload: dict[str, Any] | None = None,
    timeout: int = 60,
) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "telegram-vps-agent/1.0",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        url,
        data=data,
        method="POST" if payload is not None else "GET",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw)
            message = body.get("error", {}).get("message") or body.get("description") or raw
        except json.JSONDecodeError:
            message = raw
        raise ApiError(exc.code, str(message)[:1000]) from exc
    except urllib.error.URLError as exc:
        raise ApiError(0, str(exc.reason)) from exc


def human_bytes(value: int) -> str:
    size = float(value)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if size < 1024 or unit == "ТБ":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} ТБ"


def parse_meminfo() -> tuple[int, int]:
    values: dict[str, int] = {}
    with open("/proc/meminfo", "r", encoding="utf-8") as handle:
        for line in handle:
            key, raw = line.split(":", 1)
            values[key] = int(raw.strip().split()[0]) * 1024
    return values.get("MemTotal", 0), values.get("MemAvailable", 0)


def parse_priority_request(text: str) -> tuple[str, tuple[int, int, int]] | None:
    match = PRIORITY_RE.search(text)
    if not match:
        return None
    first = int(match.group(1))
    model = match.group(2)
    if not model.startswith("go/"):
        model = f"go/{model}"
    remaining = tuple(number for number in (1, 2, 3) if number != first)
    return model, (first, *remaining)


class TelegramClient:
    def __init__(self, token: str):
        self.base = f"https://api.telegram.org/bot{token}"

    def call(
        self, method: str, payload: dict[str, Any], timeout: int = 45
    ) -> dict[str, Any]:
        result = json_request(f"{self.base}/{method}", payload=payload, timeout=timeout)
        if not result.get("ok"):
            raise ApiError(0, str(result.get("description", "Telegram API error")))
        return result.get("result", {})

    def get_updates(self, offset: int | None) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "timeout": 30,
            "allowed_updates": ["message", "callback_query"],
        }
        if offset is not None:
            payload["offset"] = offset
        # HTTP-таймаут 35 с < TimeoutStopSec=40: процесс успевает выйти по
        # SIGTERM, а не умирать от SIGKILL (payload timeout=30 — длинный опрос).
        result = self.call("getUpdates", payload, timeout=35)
        return result if isinstance(result, list) else []

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        chunks = [text[index : index + 3900] for index in range(0, len(text), 3900)] or [""]
        for index, chunk in enumerate(chunks):
            payload: dict[str, Any] = {"chat_id": chat_id, "text": chunk}
            if reply_markup is not None and index == len(chunks) - 1:
                payload["reply_markup"] = reply_markup
            self.call("sendMessage", payload)

    def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text[:3900],
        }
        # Telegram отвергает "reply_markup": null ошибкой
        # «Bad Request: object expected as reply markup» — поэтому ключ
        # добавляем только когда клавиатура реально есть.
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        self.call("editMessageText", payload)

    def answer_callback(self, callback_id: str, text: str) -> None:
        self.call(
            "answerCallbackQuery",
            {"callback_query_id": callback_id, "text": text[:180]},
        )


class LiteLLMClient:
    # Сколько шагов с инструментами делаем до принудительного итога.
    AGENT_MAX_STEPS = 20
    # Модели с рассуждениями тратят сотни токенов на «мысли» до ответа.
    AGENT_MAX_TOKENS = 4000
    AGENT_FINAL_PROMPT = (
        "Шаги с инструментами исчерпаны. Дай итоговый ответ владельцу, опираясь "
        "на результаты уже выполненных команд. Если задача решена не полностью — "
        "честно напиши, что сделано и что осталось. Без извинений и служебных фраз."
    )

    def __init__(self, config: Config):
        self.base = config.litellm_url
        self.chat_key = config.chat_key
        self.master_key = config.master_key
        self.timeout = config.request_timeout
        configured = os.environ.get(
            "LITELLM_FALLBACK_MODELS",
            "qwen-flash,qwen-plus",
        )
        self.fallback_models = tuple(
            item.strip() for item in configured.split(",") if item.strip()
        )

    def fallback_chain(self, primary: str) -> list[str]:
        """Primary route, reserve provider, then low-cost/free GO routes."""
        return list(dict.fromkeys((primary, *self.fallback_models)))

    def health(self) -> bool:
        try:
            json_request(f"{self.base}/health/liveliness", timeout=15)
            return True
        except ApiError:
            return False

    def models(self) -> list[str]:
        body = json_request(
            f"{self.base}/v1/models", token=self.chat_key, timeout=45
        )
        return sorted(
            {
                item["id"]
                for item in body.get("data", [])
                if isinstance(item, dict) and item.get("id")
            }
        )

    def chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 700,
    ) -> str:
        body = json_request(
            f"{self.base}/v1/chat/completions",
            token=self.chat_key,
            payload={
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
            },
            timeout=self.timeout,
        )
        choices = body.get("choices") or []
        if not choices:
            raise ApiError(0, "Модель вернула ответ без choices")
        message = choices[0].get("message") or {}
        content = message.get("content") or message.get("reasoning_content") or ""
        return str(content).strip() or "Модель вернула пустой текст."

    def test_model(self, model: str) -> tuple[bool, str]:
        try:
            self.chat(
                model,
                [{"role": "user", "content": "Reply with exactly: OK"}],
                # Моделям с рассуждениями 64 токена не хватает даже на «мысли».
                max_tokens=512,
            )
            return True, "OK"
        except ApiError as exc:
            label = f"HTTP {exc.status}" if exc.status else "ошибка сети"
            return False, f"{label}: {exc}"

    def run_shell(self, command: str) -> str:
        from tool_actions import run_agent_command
        return run_agent_command(command)

    def agent_chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        on_progress: Callable[[int, str], None] | None = None,
        tools_enabled: bool = True,
        tool_runner: Callable[[str], str] | None = None,
        on_tool: Callable[..., None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
        on_text: Callable[[str], None] | None = None,
        vault_path: str | None = None,
    ) -> str:
        working = list(messages)
        local_notes = notes_context()
        if local_notes is not None:
            working.insert(max(1, len(working) - 1), local_notes)
        from private_context import owner_context
        private = owner_context(vault_path)
        if private:
            working.insert(1, private)
        context = None if private else recall(messages)
        if context is not None:
            # Keep the actual owner request last and memory below system priority.
            working.insert(max(1, len(working) - 1), context)
        with_tools = tools_enabled
        candidates = self.fallback_chain(model)
        candidate_index = 0
        active_model = candidates[candidate_index]
        for step in range(self.AGENT_MAX_STEPS):
            if should_cancel and should_cancel():
                return 'Запрос остановлен владельцем.'
            payload: dict[str, Any] = {
                "model": active_model,
                "messages": working,
                "max_tokens": self.AGENT_MAX_TOKENS,
            }
            if with_tools:
                payload["tools"] = [SHELL_TOOL]
            while True:
                payload["model"] = active_model
                try:
                    if on_text:
                        from chat_stream import stream_chat
                        on_text('')
                        body = stream_chat(f"{self.base}/v1/chat/completions", token=self.chat_key,
                            payload=payload, timeout=self.timeout, on_text=on_text,
                            should_cancel=should_cancel, error_type=ApiError)
                    else:
                        body = json_request(
                            f"{self.base}/v1/chat/completions",
                            token=self.chat_key,
                            payload=payload,
                            timeout=self.timeout,
                        )
                    break
                except ApiError as exc:
                    # Preserve tools during failover; never silently drop agent access.
                    if candidate_index + 1 >= len(candidates):
                        raise
                    previous = active_model
                    candidate_index += 1
                    active_model = candidates[candidate_index]
                    with_tools = tools_enabled
                    if with_tools:
                        payload["tools"] = [SHELL_TOOL]
                    LOG.warning(
                        "Model %s failed (HTTP %s); falling back to %s",
                        previous,
                        exc.status or "network",
                        active_model,
                    )
            choices = body.get("choices") or []
            if not choices:
                return "Модель вернула ответ без choices."
            message = choices[0].get("message") or {}
            content = message.get("content") or ""
            if isinstance(content, list):
                content = "".join(
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict) and part.get("text")
                )
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                text = str(content).strip()
                if text:
                    return text
                # Модели с рассуждениями тратят токены на «мысли» и могут
                # вернуть пустой ответ — выжимаем итог отдельным вызовом.
                return self.final_answer(
                    active_model, working, candidates[candidate_index + 1 :]
                )
            working.append(
                {"role": "assistant", "content": str(content).strip() or "", "tool_calls": tool_calls}
            )
            for call in tool_calls:
                if should_cancel and should_cancel():
                    return 'Запрос остановлен владельцем.'
                call_id = str(call.get("id", ""))
                fn = call.get("function") or {}
                arguments = fn.get("arguments") or "{}"
                try:
                    args = json.loads(arguments)
                except json.JSONDecodeError:
                    args = {}
                command = str(args.get("command") or "").strip() if isinstance(args, dict) else ''
                if command and on_progress is not None:
                    try:
                        # Номер шага и начало команды: владелец видит, что происходит,
                        # но чат не забивается — сообщение раз в 5 шагов.
                        on_progress(step + 1, command[:70])
                    except Exception:
                        LOG.warning("Не удалось отправить прогресс", exc_info=True)
                if not tools_enabled or fn.get('name') != 'shell' or not command or len(command) > 8000:
                    result = 'error: инструмент недоступен или некорректная команда; ничего не выполнено'
                else:
                    if on_tool:
                        on_tool(command)
                    result = (tool_runner or self.run_shell)(command)
                    if on_tool:
                        on_tool(command, result)
                working.append({"role": "tool", "tool_call_id": call_id, "content": result})
        # Шаги кончились — не обрываем разговор служебной фразой, а просим
        # модель свести итог из уже выполненных шагов.
        return self.final_answer(
            active_model, working, candidates[candidate_index + 1 :]
        )

    def final_answer(
        self,
        model: str,
        working: list[dict[str, Any]],
        fallback_models: list[str] | None = None,
    ) -> str:
        """Итог без инструментов: владелец всегда получает ответ, а не обрыв."""
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                *working,
                {"role": "user", "content": self.AGENT_FINAL_PROMPT},
            ],
            "max_tokens": self.AGENT_MAX_TOKENS,
        }
        last_error: ApiError | None = None
        for candidate in list(dict.fromkeys((model, *(fallback_models or [])))):
            payload["model"] = candidate
            try:
                body = json_request(
                    f"{self.base}/v1/chat/completions",
                    token=self.chat_key,
                    payload=payload,
                    timeout=self.timeout,
                )
                break
            except ApiError as exc:
                last_error = exc
                LOG.warning(
                    "Final answer model %s failed (HTTP %s)",
                    candidate,
                    exc.status or "network",
                )
        else:
            exc = last_error or ApiError(0, "unknown error")
            label = f"HTTP {exc.status}" if exc.status else "ошибка сети"
            return f"Не смог собрать итоговый ответ: {label}. Подробности в журнале."
        choices = body.get("choices") or []
        if not choices:
            return "Модель вернула ответ без choices."
        message = choices[0].get("message") or {}
        content = message.get("content") or ""
        if isinstance(content, list):
            content = "".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("text")
            )
        return str(content).strip() or "Готово: шаги выполнены, текстового итога модель не дала."

    def model_info(self) -> list[dict[str, Any]]:
        body = json_request(
            f"{self.base}/model/info", token=self.master_key, timeout=90
        )
        rows = body.get("data", [])
        return rows if isinstance(rows, list) else []

    def update_model(self, payload: dict[str, Any]) -> None:
        json_request(
            f"{self.base}/model/update",
            token=self.master_key,
            payload=payload,
            timeout=90,
        )

    def set_go_priority(self, alias: str, order: tuple[int, int, int]) -> None:
        rows = [row for row in self.model_info() if row.get("model_name") == alias]
        deployments: dict[int, dict[str, Any]] = {}
        for row in rows:
            tags = (row.get("model_info") or {}).get("tags") or []
            account = None
            for tag in tags:
                match = re.fullmatch(r"account-([123])", str(tag))
                if match:
                    account = int(match.group(1))
                    break
            if account is not None:
                deployments[account] = row
        if set(deployments) != {1, 2, 3}:
            raise ApiError(0, f"Для {alias} не найдены все три deployment'а")

        new_orders = {account: position for position, account in enumerate(order, 1)}
        old_orders = {
            account: int((row.get("litellm_params") or {}).get("order") or account)
            for account, row in deployments.items()
        }
        updated: list[int] = []

        def payload_for(account: int, priority: int) -> dict[str, Any]:
            row = deployments[account]
            params = row.get("litellm_params") or {}
            info = row.get("model_info") or {}
            return {
                "model_name": alias,
                "litellm_params": {
                    "model": params["model"],
                    "api_base": params["api_base"],
                    "api_key": f"os.environ/OPENCODE_GO_KEY_{account}",
                    "order": priority,
                },
                "model_info": {
                    "id": info["id"],
                    "tags": info.get("tags") or [
                        "opencode-go",
                        f"account-{account}",
                        "automatic-failover",
                    ],
                },
            }

        try:
            for account in (1, 2, 3):
                self.update_model(payload_for(account, new_orders[account]))
                updated.append(account)
        except Exception:
            LOG.exception("Priority update failed; attempting rollback")
            for account in updated:
                try:
                    self.update_model(payload_for(account, old_orders[account]))
                except Exception:
                    LOG.exception("Rollback failed for account %s", account)
            raise


class PersonalBot:
    def __init__(self, config: Config):
        self.config = config
        self.telegram = TelegramClient(config.telegram_token)
        self.litellm = LiteLLMClient(config)
        self.stop_event = threading.Event()
        self.pending: dict[str, dict[str, Any]] = {}
        self.history: dict[int, deque[dict[str, str]]] = defaultdict(
            lambda: deque(maxlen=12)
        )
        self.chat_model = config.default_model
        self.vault_path = os.environ.get(
            "VAULT_PATH", DEFAULT_VAULT_PATH
        )
        self.vault: dict[str, str] = {}
        self.load_vault()
        self.allowed_path = os.environ.get(
            "ALLOWED_PATH", "/var/lib/litellm-telegram-bot/allowed.json"
        )
        self.allowed_groups: set[int] = set()
        self.allowed_users: set[int] = set()
        self.load_allowed()
        for var, target in (
            ("ALLOWED_GROUPS", self.allowed_groups),
            ("ALLOWED_USERS", self.allowed_users),
        ):
            for value in os.environ.get(var, "").split(","):
                raw = value.strip()
                if raw.isdigit():
                    target.add(int(raw))

    def load_allowed(self) -> None:
        try:
            with open(self.allowed_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                groups = data.get("groups")
                users = data.get("users")
                if isinstance(groups, list):
                    self.allowed_groups = {
                        int(value) for value in groups if str(value).lstrip("-").isdigit()
                    }
                if isinstance(users, list):
                    self.allowed_users = {
                        int(value) for value in users if str(value).lstrip("-").isdigit()
                    }
        except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
            self.allowed_groups, self.allowed_users = set(), set()

    def save_allowed(self) -> None:
        data = {
            "groups": sorted(self.allowed_groups),
            "users": sorted(self.allowed_users),
        }
        tmp = self.allowed_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.allowed_path)

    def allow_text(self, args: str, chat_id: int, ctype: str) -> str:
        if ctype in ("group", "supergroup"):
            self.allowed_groups.add(chat_id)
        for part in args.split():
            raw = part.strip()
            if raw.lstrip("-").isdigit():
                self.allowed_users.add(int(raw))
        self.save_allowed()
        lines = [
            f"Группы ({len(self.allowed_groups)}): " + ", ".join(map(str, self.allowed_groups)) or "нет",
            f"Пользователи ЛС ({len(self.allowed_users)}): " + ", ".join(map(str, self.allowed_users)) or "нет",
        ]
        return "Разрешено.\n" + "\n".join(lines)

    def deny_text(self, args: str, chat_id: int, ctype: str) -> str:
        if ctype in ("group", "supergroup"):
            self.allowed_groups.discard(chat_id)
        if args.strip():
            self.allowed_users.difference_update(
                int(part) for part in args.split() if part.strip().lstrip("-").isdigit()
            )
        self.save_allowed()
        return "Обновлено: группы " + ", ".join(map(str, self.allowed_groups)) + " · ЛС " + ", ".join(map(str, self.allowed_users))

    def load_vault(self) -> None:
        try:
            with open(self.vault_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self.vault = data if isinstance(data, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            self.vault = {}

    def save_vault(self) -> None:
        tmp = self.vault_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.vault, fh, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.vault_path)

    def vault_text(self, args: str) -> str:
        parts = args.split()
        sub = parts[0].lower() if parts else "list"
        name = parts[1] if len(parts) > 1 else ""
        if sub == "list":
            if not self.vault:
                return "Хранилище пусто. Использование: /vault set <имя> <значение>"
            lines = [
                f"{key}: {mask_value(value)}" for key, value in sorted(self.vault.items())
            ]
            return "Секреты:\n" + "\n".join(lines)
        if sub == "show":
            if not VNAME_RE.fullmatch(name):
                return "Использование: /vault show <имя>"
            value = self.vault.get(name)
            if value is None:
                return f"Нет записи: {name}"
            return f"{name}: {value}"
        if sub == "set":
            if not VNAME_RE.fullmatch(name) or len(parts) < 3:
                return "Использование: /vault set <имя> <значение>"
            value = args[len(parts[0]) + 1 + len(name) + 1 :].strip()
            if not value:
                return "Значение пустое."
            self.vault[name] = value
            self.save_vault()
            return f"Сохранено в vault: {name} (замаскировано: {mask_value(value)})"
        if sub == "del":
            if not VNAME_RE.fullmatch(name) or name not in self.vault:
                return f"Нет записи: {name}"
            del self.vault[name]
            self.save_vault()
            return f"Удалено из vault: {name}"
        return (
            "Подкоманды vault:\n"
            "/vault list\n"
            "/vault show <имя>\n"
            "/vault set <имя> <значение>\n"
            "/vault del <имя>"
        )

    def is_authorized(self, actor_id: int | None, chat: dict[str, Any]) -> bool:
        if actor_id is None:
            return False
        if actor_id == self.config.admin_id:
            return True
        if actor_id in self.allowed_users:
            return True
        ctype = chat.get("type")
        if ctype in ("group", "supergroup"):
            return chat.get("id") in self.allowed_groups
        return False

    def help_text(self) -> str:
        return (
            "Личный бот управления LiteLLM.\n\n"
            "/menu — выбрать провайдера и модель\n"
            "/status — VPS и LiteLLM\n"
            "/accounts — проверить 3 аккаунта OpenCode Go\n"
            "/models [prefix] — модели или фильтр\n"
            "/test <model> — тест модели\n"
            "/use|/model <model> — выбрать модель для общения с ботом (можно и текстом: «перейди на в4 про»)\n"
            "/priority <model> <порядок> — например: "
            "/priority kimi-k3 3 1 2\n"
            "/vault list · show · set · del — хранилище паролей/ключей\n"
            "/allow [id id…] — разрешить эту группу и/или ID в ЛС\n"
            "/deny [id id…] — запретить группу/ID\n"
            "/help — эта справка\n\n"
            "Можно писать обычным текстом: бот-агент сам выполнит команды на сервере "
            "через root-шелл. Изменения маршрутов требуют подтверждения."
        )

    def status_text(self) -> str:
        healthy = self.litellm.health()
        try:
            models = self.litellm.models()
            model_text = str(len(models))
        except ApiError as exc:
            model_text = f"ошибка ({exc.status or 'network'})"
        disk = shutil.disk_usage("/")
        mem_total, mem_available = parse_meminfo()
        return (
            f"LiteLLM: {'работает' if healthy else 'НЕ ОТВЕЧАЕТ'}\n"
            f"Моделей: {model_text}\n"
            f"Модель бота: {self.chat_model}\n"
            f"Диск: свободно {human_bytes(disk.free)} из {human_bytes(disk.total)}\n"
            f"RAM: доступно {human_bytes(mem_available)} из {human_bytes(mem_total)}"
        )

    def accounts_text(self) -> str:
        def check(account: int) -> tuple[int, bool, str]:
            ok, detail = self.litellm.test_model(f"go{account}/glm-5.3-flash")
            return account, ok, detail

        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            results = sorted(pool.map(check, (1, 2, 3)))
        lines = ["OpenCode Go:"]
        for account, ok, detail in results:
            state = "работает" if ok else "недоступен"
            lines.append(f"#{account}: {state}" + ("" if ok else f" — {detail[:240]}"))
        lines.append("Общий маршрут go/* автоматически пропускает нерабочий аккаунт.")
        return "\n".join(lines)

    def models_text(self, prefix: str = "") -> str:
        models = self.litellm.models()
        if prefix:
            models = [model for model in models if model.startswith(prefix)]
            heading = f"Модели с префиксом {prefix}: {len(models)}"
        else:
            heading = (
                f"Всего моделей: {len(models)}\n"
                f"DashScope/без префикса: {sum('/' not in model for model in models)}\n"
                f"go/*: {sum(model.startswith('go/') for model in models)}\n"
                f"go1/*: {sum(model.startswith('go1/') for model in models)}\n"
                f"go2/*: {sum(model.startswith('go2/') for model in models)}\n"
                f"go3/*: {sum(model.startswith('go3/') for model in models)}"
            )
        if not prefix:
            return heading
        shown = models[:80]
        suffix = "\n…список сокращён" if len(models) > len(shown) else ""
        return heading + "\n\n" + "\n".join(shown) + suffix

    def propose_priority(
        self, chat_id: int, alias: str, order: tuple[int, int, int]
    ) -> None:
        if not alias.startswith("go/"):
            alias = f"go/{alias}"
        if not MODEL_RE.fullmatch(alias) or sorted(order) != [1, 2, 3]:
            self.telegram.send_message(chat_id, "Неверная модель или порядок аккаунтов.")
            return
        nonce = secrets.token_urlsafe(8)
        self.pending[nonce] = {
            "kind": "priority",
            "alias": alias,
            "order": order,
            "expires": time.time() + 300,
            "chat_id": chat_id,
        }
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "Подтвердить", "callback_data": f"confirm:{nonce}"},
                    {"text": "Отмена", "callback_data": f"cancel:{nonce}"},
                ]
            ]
        }
        self.telegram.send_message(
            chat_id,
            f"Изменить приоритет {alias}: {' → '.join(map(str, order))}?\n"
            "Подтверждение действует 5 минут.",
            reply_markup=keyboard,
        )

    def provider_menu_markup(self) -> dict[str, Any]:
        rows = [
            [{"text": name, "callback_data": f"prov:{key}"}]
            for key, (name, _models) in PROVIDERS.items()
        ]
        return {"inline_keyboard": rows}

    def models_menu_markup(self, key: str) -> dict[str, Any]:
        models = PROVIDERS[key][1]
        rows = []
        for index, (model_id, label) in enumerate(models):
            mark = " ✓" if model_id == self.chat_model else ""
            rows.append(
                [
                    {
                        "text": f"{label}{mark}",
                        "callback_data": f"model:{key}:{index}",
                    }
                ]
            )
        rows.append([{"text": "◀ К провайдерам", "callback_data": "menu:prov"}])
        return {"inline_keyboard": rows}

    def show_model_menu(self, chat_id: int) -> None:
        self.telegram.send_message(
            chat_id,
            f"Выбери провайдера.\n\nСейчас: {self.chat_model}",
            reply_markup=self.provider_menu_markup(),
        )

    def switch_model(
        self,
        chat_id: int,
        message_id: int,
        callback_id: str,
        provider_key: str,
        index: int,
    ) -> None:
        try:
            model_id, label = PROVIDERS[provider_key][1][index]
        except (IndexError, KeyError):
            self.telegram.answer_callback(callback_id, "Модель не найдена")
            return
        if model_id != self.chat_model:
            self.chat_model = model_id
            self.history[chat_id].clear()
        self.telegram.answer_callback(callback_id, f"Выбрано: {label}")
        self.telegram.edit_message(
            chat_id,
            message_id,
            f"Модель общения: {label} ({model_id})",
            # Пустая клавиатура — явный способ убрать кнопки у сообщения.
            reply_markup={"inline_keyboard": []},
        )

    def handle_menu_callback(
        self,
        chat_id: int,
        message_id: int,
        callback_id: str,
        data: str,
    ) -> bool:
        action, _, param = data.partition(":")
        if action == "prov" and param in PROVIDERS:
            name = PROVIDERS[param][0]
            self.telegram.answer_callback(callback_id, "Список моделей")
            self.telegram.edit_message(
                chat_id,
                message_id,
                f"Провайдер: {name}\n\nМодель (сейчас: {self.chat_model}):",
                reply_markup=self.models_menu_markup(param),
            )
            return True
        if action == "model":
            provider_key, _, raw_index = param.partition(":")
            if provider_key in PROVIDERS and raw_index.isdigit():
                self.switch_model(
                    chat_id, message_id, callback_id, provider_key, int(raw_index)
                )
                return True
        if action == "menu" and param == "prov":
            self.telegram.answer_callback(callback_id, "К списку провайдеров")
            self.telegram.edit_message(
                chat_id,
                message_id,
                f"Выбери провайдера.\n\nСейчас: {self.chat_model}",
                reply_markup=self.provider_menu_markup(),
            )
            return True
        self.telegram.answer_callback(callback_id, "Неизвестное действие")
        return True

    def handle_command(
        self, chat_id: int, actor_id: int, ctype: str, text: str
    ) -> None:
        command, _, args = text.strip().partition(" ")
        command = command.split("@", 1)[0].lower()
        args = args.strip()
        if command in ("/start", "/help"):
            self.telegram.send_message(chat_id, self.help_text())
        elif command in ("/allow", "/deny"):
            if actor_id != self.config.admin_id:
                self.telegram.send_message(chat_id, "Только админ может менять доступ.")
                return
            self.telegram.send_message(
                chat_id,
                self.allow_text(args, chat_id, ctype)
                if command == "/allow"
                else self.deny_text(args, chat_id, ctype),
            )
        elif command == "/vault":
            self.telegram.send_message(chat_id, self.vault_text(args))
        elif command == "/menu":
            self.show_model_menu(chat_id)
        elif command == "/status":
            self.telegram.send_message(chat_id, self.status_text())
        elif command == "/accounts":
            self.telegram.send_message(chat_id, "Проверяю три аккаунта…")
            self.telegram.send_message(chat_id, self.accounts_text())
        elif command == "/models":
            self.telegram.send_message(chat_id, self.models_text(args))
        elif command == "/test":
            if not MODEL_RE.fullmatch(args):
                self.telegram.send_message(chat_id, "Использование: /test <model>")
                return
            self.telegram.send_message(chat_id, f"Проверяю {args}…")
            ok, detail = self.litellm.test_model(args)
            self.telegram.send_message(
                chat_id, f"{args}: {'работает' if ok else 'ошибка'} — {detail[:800]}"
            )
        elif command in ("/use", "/model", "/switch"):
            if not args:
                # Без аргумента показываем текущую модель, а не ругаемся.
                self.telegram.send_message(
                    chat_id,
                    f"Сейчас: {self.chat_model}\n"
                    "Сменить: /use <модель> — например: /use hy4 preview\n"
                    "Список: /models go/",
                )
                return
            resolved = self.resolve_model_arg(args)
            if not resolved:
                hint = self.suggest_models_text(args)
                self.telegram.send_message(
                    chat_id,
                    f"Не нашёл модель «{args[:60]}» в /v1/models."
                    + (f"\n\nПохожие:\n{hint}" if hint else "\n\nСписок: /models"),
                )
                return
            self.chat_model = resolved
            self.history[chat_id].clear()
            self.telegram.send_message(chat_id, f"Модель общения: {resolved}")
        elif command == "/priority":
            parts = args.split()
            if len(parts) != 4:
                self.telegram.send_message(
                    chat_id, "Использование: /priority <model> 3 1 2"
                )
                return
            try:
                order = tuple(int(value) for value in parts[1:])
            except ValueError:
                order = ()
            if len(order) != 3:
                self.telegram.send_message(chat_id, "Порядок должен выглядеть как 3 1 2.")
                return
            self.propose_priority(chat_id, parts[0], order)  # type: ignore[arg-type]
        else:
            self.telegram.send_message(chat_id, "Неизвестная команда. Используйте /help.")

    @staticmethod
    def _squeeze(value: str) -> str:
        """«Hy4 preview» и «hy4-preview» -> «hy4preview»."""
        return re.sub(r"[^a-z0-9]+", "", value.casefold())

    def resolve_model_arg(self, raw: str) -> str | None:
        """Принимает и точный id, и человеческое название с пробелами."""
        query = " ".join(raw.split()).strip()
        if not query:
            return None
        try:
            known = self.litellm.models()
        except ApiError:
            return query if MODEL_RE.fullmatch(query) else None
        squeezed = self._squeeze(query)
        for model in known:
            if model.casefold() == query.casefold():
                return model
        for model in known:
            if self._squeeze(model) == squeezed:
                return model
        resolved = resolve_model_query(query)
        if resolved and resolved in known:
            return resolved
        return None

    def suggest_models_text(self, raw: str, limit: int = 8) -> str:
        """Подсказка: модели, похожие на то, что ввёл владелец."""
        needle = self._squeeze(raw)
        try:
            known = self.litellm.models()
        except ApiError:
            return ""
        hits = [model for model in known if needle and needle in self._squeeze(model)]
        if not hits:
            hits = [model for model in known if model.startswith("go/")]
        return "\n".join(hits[:limit])

    def handle_text(self, chat_id: int, text: str) -> None:
        lowered = text.casefold()
        priority = parse_priority_request(text)
        if priority:
            self.propose_priority(chat_id, *priority)
            return
        switch_match = re.search(
            r"(?:перейди|переключи|переключай|смени|поменяй|поставь|выбери|установи|хочу)"
            r".*\b(?:на|модель(?:ю)?)\s+([^.,!?\n]{1,60})",
            lowered,
            re.IGNORECASE,
        )
        if switch_match:
            query = switch_match.group(1).strip()
            trailing = True
            while trailing:
                prompt = query.rstrip()
                trailing = False
                model = resolve_model_query(prompt)
                if model is None or model not in self.litellm.models():
                    parts = prompt.split()
                    if len(parts) > 1:
                        query = " ".join(parts[:-1])
                        trailing = True
                    else:
                        break
            if model and model in self.litellm.models():
                self.chat_model = model
                self.history[chat_id].clear()
                self.telegram.send_message(chat_id, f"Модель общения: {model}")
                return
            self.telegram.send_message(
                chat_id,
                "Не разобрал модель. Список: /models · смена: /use <model>",
            )
            return
        if "статус" in lowered or "состояние сервера" in lowered:
            self.telegram.send_message(chat_id, self.status_text())
            return
        if "аккаунт" in lowered and any(
            word in lowered for word in ("проверь", "статус", "покажи", "работ")
        ):
            self.telegram.send_message(chat_id, "Проверяю три аккаунта…")
            self.telegram.send_message(chat_id, self.accounts_text())
            return
        if "модел" in lowered and any(
            word in lowered for word in ("какая", "что за", "у тебя", "скажи", "ты на")
        ):
            current = self.chat_model
            for _provider, (_label, pairs) in PROVIDERS.items():
                for model_id, display in pairs:
                    if model_id == self.chat_model:
                        current = f"{display} ({model_id})"
            self.telegram.send_message(
                chat_id, f"Сейчас отвечаю на модели: {current}"
            )
            return
        if "модел" in lowered and any(
            word in lowered for word in ("покажи", "список", "сколько")
        ):
            self.telegram.send_message(chat_id, self.models_text())
            return

        history = self.history[chat_id]
        messages = [
            {"role": "system", "content": build_agent_system_prompt(getattr(self, "vault_path", DEFAULT_VAULT_PATH))},
            *list(history),
            {"role": "user", "content": text[:12000]},
        ]
        progress = ReplyProgress(self.telegram, chat_id).start()
        try:
            answer = self.litellm.agent_chat(
                self.chat_model, messages, on_progress=progress.report,
                tool_runner=__import__('tool_actions').run_agent_command,
                vault_path=getattr(self, 'vault_path', DEFAULT_VAULT_PATH)
            )
        except Exception:
            LOG.warning("Agent request failed")
            progress.finish("Не удалось обработать запрос. Попробуйте ещё раз.")
            return
        history.append({"role": "user", "content": text[:12000]})
        history.append({"role": "assistant", "content": answer[:12000]})
        progress.finish(answer)

    def handle_callback(self, callback: dict[str, Any]) -> None:
        from console_approval import handle_console_callback
        if handle_console_callback(self, callback):
            return
        from tool_actions import handle_tool_callback
        if handle_tool_callback(self, callback):
            return
        if not hasattr(self, 'local_capture'):
            self.local_capture = LocalCapture(self)
        if self.local_capture.callback(callback):
            return
        actor_id = (callback.get("from") or {}).get("id")
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        callback_id = str(callback.get("id", ""))
        if not self.is_authorized(actor_id, chat):
            self.telegram.answer_callback(callback_id, "Доступ запрещён")
            return
        data = str(callback.get("data", ""))
        if data.startswith(("prov:", "model:", "menu:")):
            self.handle_menu_callback(
                chat["id"], message.get("message_id"), callback_id, data
            )
            return
        action, _, nonce = data.partition(":")
        pending = self.pending.pop(nonce, None)
        if not pending or pending.get("chat_id") != chat.get("id"):
            self.telegram.answer_callback(callback_id, "Запрос уже недействителен")
            return
        if pending["expires"] < time.time():
            self.telegram.answer_callback(callback_id, "Подтверждение истекло")
            return
        if action == "cancel":
            self.telegram.answer_callback(callback_id, "Отменено")
            self.telegram.send_message(chat["id"], "Изменение отменено.")
            return
        if action != "confirm":
            self.telegram.answer_callback(callback_id, "Неизвестное действие")
            return
        self.telegram.answer_callback(callback_id, "Выполняю")
        if pending["kind"] == "priority":
            self.litellm.set_go_priority(pending["alias"], pending["order"])
            self.telegram.send_message(
                chat["id"],
                f"Готово: {pending['alias']} → "
                f"{' → '.join(map(str, pending['order']))}",
            )

    def handle_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            self.handle_callback(update["callback_query"])
            return
        message = update.get("message") or {}
        actor_id = (message.get("from") or {}).get("id")
        chat = message.get("chat") or {}
        if not self.is_authorized(actor_id, chat):
            LOG.warning("Rejected Telegram actor id=%s chat_type=%s", actor_id, chat.get("type"))
            if actor_id == self.config.admin_id:
                self.telegram.send_message(
                    chat["id"],
                    "Доступ к этому чату не настроен. Отправь здесь /allow, чтобы разрешить его.",
                )
            else:
                self.telegram.send_message(
                    chat["id"], "Нет доступа. Если это твой аккаунт — попроси админа отправить /allow."
                )
            return
        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            self.telegram.send_message(chat["id"], "Пока поддерживаются только текстовые сообщения.")
            return
        capture_chat = dict(chat)
        capture_chat['message_thread_id'] = message.get('message_thread_id')
        if text.startswith("/"):
            if not hasattr(self, 'local_capture'):
                self.local_capture = LocalCapture(self)
            if self.local_capture.propose(actor_id, capture_chat, text):
                return
            self.handle_command(chat["id"], actor_id, chat.get("type", ""), text)
        else:
            if not hasattr(self, 'local_capture'):
                self.local_capture = LocalCapture(self)
            if self.local_capture.propose(actor_id, capture_chat, text):
                return
            self.handle_text(chat["id"], text)

    def run(self) -> None:
        bot_info = self.telegram.call("getMe", {})
        LOG.info("Started Telegram bot @%s", bot_info.get("username", "unknown"))
        offset: int | None = None
        pending_updates = queue.Queue(maxsize=32)

        def process(update):
            try:
                self.handle_update(update)
            except Exception as exc:
                LOG.exception("Update failed")
                message = update.get("message") or (update.get("callback_query") or {}).get("message") or {}
                chat_id = (message.get("chat") or {}).get("id")
                actor = update.get("message", {}).get("from", {}).get("id") or update.get("callback_query", {}).get("from", {}).get("id")
                if chat_id and actor == self.config.admin_id:
                    label = f"HTTP {exc.status}" if isinstance(exc, ApiError) and exc.status else "ошибка"
                    try:
                        self.telegram.send_message(chat_id, f"Не удалось выполнить запрос: {label}. Подробности записаны в журнал.")
                    except Exception:
                        LOG.warning("Could not report update failure")

        def worker():
            while not self.stop_event.is_set():
                try:
                    update = pending_updates.get(timeout=0.5)
                except queue.Empty:
                    continue
                try:
                    process(update)
                finally:
                    pending_updates.task_done()

        threading.Thread(target=worker, name='telegram-messages', daemon=True).start()
        while not self.stop_event.is_set():
            try:
                for update in self.telegram.get_updates(offset):
                    offset = int(update["update_id"]) + 1
                    callback = update.get('callback_query') or {}
                    if str(callback.get('data', '')).startswith(('cc:', 'tc:')):
                        process(update)
                    else:
                        try:
                            pending_updates.put_nowait(update)
                        except queue.Full:
                            LOG.warning('Telegram message queue is full; update rejected')
                            message = update.get('message') or callback.get('message') or {}
                            actor = (update.get('message', {}).get('from') or callback.get('from') or {}).get('id')
                            if actor == self.config.admin_id and message.get('chat', {}).get('id'):
                                self.telegram.send_message(message['chat']['id'], 'Очередь заполнена. Повторите сообщение после завершения текущего запроса.')
            except ApiError as exc:
                LOG.warning("Telegram polling error status=%s: %s", exc.status, exc)
                self.stop_event.wait(5)


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("BOT_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    config = Config.from_env()
    bot = PersonalBot(config)
    bot.local_capture = LocalCapture(bot)
    bot.local_capture.start_auto()
    from bot_chat_bridge import start_bridge
    web_bridge = start_bridge(bot, build_agent_system_prompt(bot.vault_path))

    def stop_handler(_signum: int, _frame: Any) -> None:
        bot.stop_event.set()

    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    bot.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
