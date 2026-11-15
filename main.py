#!/usr/bin/env python3
"""Free AI model aggregator -- make.fun bounty "free-model-aggregator-01".

One server, two client APIs (OpenAI Chat Completions and Anthropic Messages,
each in JSON and in server-sent events), over ten simulated free-tier model
providers (OpenAI, Anthropic and Gemini wires). For each of the trial's 20
requests it picks the best free model that meets the request's requirements,
speaks that provider's wire, fails over when a provider refuses (402/429/5xx),
and returns the answer in the wire the client used -- with the provider's
text whole, its usage counts as that wire reports them, and its stop reason
mapped to the client's wire.

Run modes
---------
* ``python3 main.py`` -- read the brief at $MAKEFUN_PROMPT_FILE, probe the
  providers under $MAKEFUN_ENV_URL, register with the env, serve the trial,
  and exit 0 when the env says the workload is done.
* ``python3 main.py --list`` -- print the catalog document (the same document
  the register body carries) and exit 0. With no brief and no env it prints
  ``{"providers":[]}``.

Only the Python standard library is used: nothing is installed before the
server runs. Every routing choice is a function of the brief, the requests
and the providers' answers so far: no wall clock and no unseeded randomness,
so a replay leaves the same state export.

Routing, in short
-----------------
* The catalog is built by probing every provider's own catalog route, in
  that provider's wire, and keeping the listing's order (the scorer compares
  the free-model list against the providers' true catalogs in order).
  A probe that fails falls back to the brief's quality table for that
  provider; a provider with no probe and no table row is left out.
* For each request, candidates are the free models meeting its requirements
  (tools / vision / JSON mode / minimum context). A request that carries
  JSON mode on the OpenAI wire is never routed to an Anthropic-wire provider:
  that wire cannot carry ``response_format`` and its echo would say JSON
  mode was absent. The model must be free; a paid model is never picked.
* A greedy plan over the requests still to come (highest weight and
  scarcest requirements first) assigns each request its best model under
  per-provider planning budgets (the per-trial minimums of the kit's
  provider table); the plan is recomputed from the calls made so far
  before every request, so refusals and spent budgets reshape it.
* A provider call that answers 200 is used: it is the request's one
  successful call. A call that is refused (402, 429, 5xx) or unreachable is
  counted and the request is retried on the next candidate, in rating
  order, for up to two passes over the candidates. A 400 (unknown or paid
  model) drops that model for the rest of the trial.
"""

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROTOCOL_VERSION = 1
POLL_INTERVAL_S = 0.1
PROBE_TIMEOUT_S = 5.0
UPSTREAM_TIMEOUT_S = 10.0
REGISTER_ATTEMPTS = 10
REGISTER_RETRY_S = 0.2
MAX_BODY_BYTES = 32 * 1024 * 1024
FIXED_OPENAI_CREATED = 1767225600
FIXED_ANTHROPIC_CREATED_AT = "2026-01-01T00:00:00Z"
ANTHROPIC_VERSION = "2023-06-01"

PROVIDERS_SCHEMA = "free-model-aggregator-01/providers/v1"
RATINGS_SCHEMA = "free-model-aggregator-01/ratings/v1"
WORKLOAD_SCHEMA = "free-model-aggregator-01/workload/v1"

# Per-provider planning budgets: the per-trial minimum call counts of the
# kit's provider table (program/generator/draw.mjs draws each trial's budget
# in [calls_min, calls_max]). They are planning caps only -- the server
# adapts to the real budgets at runtime, learning them from refusals.
PLAN_MIN_CALLS = {
    "groq": 9,
    "cerebras": 5,
    "gemini": 6,
    "zai": 4,
    "openrouter": 10,
    "sambanova": 4,
    "pollinations": 10,
    "cloudflare": 7,
    "ovh": 5,
    "kilol": 12,
}

ENV_URL = os.environ.get("MAKEFUN_ENV_URL", "").rstrip("/")
PROMPT_FILE = os.environ.get("MAKEFUN_PROMPT_FILE", "")


def log(message):
    sys.stderr.write("aggregator: %s\n" % message)
    sys.stderr.flush()


def is_record(value):
    return isinstance(value, dict)


def string_of(value):
    return value if isinstance(value, str) else ""


def number_or_none(value):
    return value if isinstance(value, (int, float)) and value == value and abs(value) != float("inf") else None


def bool_or_none(value):
    return value if isinstance(value, bool) else None


# ---------------------------------------------------------------------------
# The brief: three JSON documents found by their `schema` member.
# ---------------------------------------------------------------------------

def _object_end(text, start):
    """Index of the `}` closing the object that opens at `start`, or -1."""
    depth = 0
    in_string = False
    i = start
    while i < len(text):
        ch = text[i]
        if in_string:
            if ch == "\\":
                i += 1
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _documents_in(text):
    """Every JSON object in `text` carrying a string `schema`, in order."""
    out = []
    start = text.find("{")
    while start != -1:
        end = _object_end(text, start)
        if end != -1:
            try:
                value = json.loads(text[start:end + 1])
            except (ValueError, RecursionError):
                value = None
            if is_record(value) and isinstance(value.get("schema"), str):
                out.append(value)
        start = text.find("{", start + 1)
    return out


def _provider_of(value):
    if not is_record(value):
        return None
    pid = string_of(value.get("id"))
    wire = value.get("wire")
    if pid == "" or wire not in ("openai", "anthropic", "gemini"):
        return None
    auth = value.get("auth")
    if auth not in ("bearer", "x-api-key", "x-goog-api-key", "none"):
        auth = "bearer"
    base = ""
    for key in ("path", "base_path", "base_url", "base"):
        if string_of(value.get(key)) != "":
            base = string_of(value.get(key))
            break
    return {
        "id": pid,
        "name": string_of(value.get("name")) or pid,
        "wire": wire,
        "auth": auth,
        "base": base,
        "key_env": string_of(value.get("key_env")),
        "key": string_of(value.get("key")),
    }


def _rating_of(value):
    if not is_record(value):
        return None
    model = string_of(value.get("model"))
    provider = string_of(value.get("provider"))
    if model == "" or provider == "":
        return None
    rating = value.get("rating_milli")
    return {
        "model": model,
        "provider": provider,
        "rating_milli": rating if isinstance(rating, (int, float)) else 0,
        "context_length": number_or_none(value.get("context_length")),
        "tools": value.get("tools") is True,
        "vision": value.get("vision") is True,
        "json": value.get("json") is True,
        "free": value.get("free") is True,
    }


def _workload_request_of(value):
    if not is_record(value):
        return None
    rid = string_of(value.get("id"))
    if rid == "":
        return None
    requires = value.get("requires") if is_record(value.get("requires")) else {}
    out = {}
    if requires.get("tools") is True:
        out["tools"] = True
    if requires.get("vision") is True:
        out["vision"] = True
    if requires.get("json") is True:
        out["json"] = True
    min_context = requires.get("min_context")
    if isinstance(min_context, (int, float)):
        out["min_context"] = min_context
    return {
        "id": rid,
        "api": "anthropic" if value.get("api") == "anthropic" else "openai",
        "stream": value.get("stream") is True,
        "weight": 2 if value.get("weight") == 2 else 1,
        "requires": out,
    }


def read_brief(path):
    """The brief at `path`: providers, ratings rows, workload manifest."""
    brief = {"providers": [], "ratings": {}, "workload": [], "error": None}
    if path == "":
        brief["error"] = "MAKEFUN_PROMPT_FILE is not set"
        return brief
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError as err:
        brief["error"] = "cannot read %s: %s" % (path, err)
        return brief
    docs = _documents_in(text)
    directory = next((d for d in docs if d.get("schema") == PROVIDERS_SCHEMA and isinstance(d.get("providers"), list)), None)
    if directory is None:
        brief["error"] = "the brief at %s holds no %s document" % (path, PROVIDERS_SCHEMA)
        return brief
    for raw in directory["providers"]:
        provider = _provider_of(raw)
        if provider is not None:
            brief["providers"].append(provider)
    ratings_doc = next((d for d in docs if d.get("schema") == RATINGS_SCHEMA and isinstance(d.get("ratings"), list)), None)
    if ratings_doc is not None:
        for raw in ratings_doc["ratings"]:
            row = _rating_of(raw)
            if row is not None:
                brief["ratings"][(row["provider"], row["model"])] = row
    workload_doc = next((d for d in docs if d.get("schema") == WORKLOAD_SCHEMA and isinstance(d.get("manifest"), list)), None)
    if workload_doc is not None:
        for raw in workload_doc["manifest"]:
            entry = _workload_request_of(raw)
            if entry is not None:
                brief["workload"].append(entry)
    return brief


# ---------------------------------------------------------------------------
# Provider catalog probe.
# ---------------------------------------------------------------------------

def join_base(env_url, base):
    """A base path joined to the env's URL; an absolute base is used as is."""
    base = base.strip()
    if base.lower().startswith("http://") or base.lower().startswith("https://"):
        return base.rstrip("/")
    prefix = env_url.rstrip("/")
    if base == "":
        return prefix
    return prefix + ("" if base.startswith("/") else "/") + base.rstrip("/")


def auth_headers(auth, key):
    """The headers a provider wants its key in; none takes no key."""
    if key == "":
        return {}
    if auth == "bearer":
        return {"Authorization": "Bearer " + key}
    if auth == "x-api-key":
        return {"x-api-key": key}
    if auth == "x-goog-api-key":
        return {"x-goog-api-key": key}
    return {}


def _http_get(url, headers, timeout):
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as err:
        try:
            err.read()
        except Exception:
            pass
        return err.code, b""
    except Exception:
        return 0, b""


def _zero_priced(value):
    """True when every price in a listing's `pricing` is zero, else None."""
    pricing = value.get("pricing") if is_record(value) else None
    if not is_record(pricing):
        return None
    numbers = []
    for item in pricing.values():
        if isinstance(item, (int, float)):
            numbers.append(item)
        elif isinstance(item, str) and item.strip() != "":
            try:
                numbers.append(float(item))
            except ValueError:
                pass
    if not numbers:
        return None
    return all(n == 0 for n in numbers)


def _model_in(wire, value):
    """One model as a catalog listing wrote it, in its wire's spelling."""
    if not is_record(value):
        return None
    if wire == "gemini":
        model_id = string_of(value.get("name")).replace("models/", "", 1)
    else:
        model_id = string_of(value.get("id"))
    if model_id == "":
        return None
    context = number_or_none(value.get("context_length"))
    if context is None:
        context = number_or_none(value.get("context_window"))
    if context is None:
        context = number_or_none(value.get("inputTokenLimit"))
    free = bool_or_none(value.get("free"))
    if free is None:
        free = _zero_priced(value)
    return {
        "id": model_id,
        "display_name": string_of(value.get("display_name")) or string_of(value.get("displayName")) or model_id,
        "free": free,
        "context_length": context,
        "tools": bool_or_none(value.get("tools")),
        "vision": bool_or_none(value.get("vision")),
        "json": bool_or_none(value.get("json")),
    }


def probe_provider(env_url, provider, timeout=PROBE_TIMEOUT_S):
    """One provider's catalog route, in its own wire. Never raises."""
    base = join_base(env_url, provider["base"])
    if provider["wire"] == "gemini":
        url = base + "/v1beta/models"
    else:
        url = base + "/v1/models"
    headers = {"Accept": "application/json"}
    headers.update(auth_headers(provider["auth"], provider["key"]))
    if provider["wire"] == "anthropic":
        headers["anthropic-version"] = ANTHROPIC_VERSION
    status, raw = _http_get(url, headers, timeout)
    if status < 200 or status >= 300:
        return {"provider": provider, "ok": False, "status": status, "models": [],
                "error": "GET %s answered HTTP %s" % (url, status)}
    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {"provider": provider, "ok": False, "status": status, "models": [],
                "error": "GET %s answered a body that is not JSON" % url}
    if not is_record(body):
        return {"provider": provider, "ok": False, "status": status, "models": [],
                "error": "GET %s answered a JSON value that is not one object" % url}
    listing = body.get("models") if provider["wire"] == "gemini" else body.get("data")
    models = []
    if isinstance(listing, list):
        for raw_model in listing:
            model = _model_in(provider["wire"], raw_model)
            if model is not None:
                models.append(model)
    return {"provider": provider, "ok": True, "status": status, "models": models, "error": None}


def rating_of(ratings, provider_id, model_id):
    return ratings.get((provider_id, model_id))


def model_free(model, row):
    """Whether a model is free: the quality table where it has a row, else
    the listing's own word; paid when neither says."""
    if row is not None:
        return row["free"]
    return model["free"] if model["free"] is not None else False


def capabilities_of(model, row):
    """What a model can do: the quality table where it has a row, else the
    listing's own fields (None where neither said)."""
    if row is not None:
        return {
            "context_length": row["context_length"],
            "tools": row["tools"],
            "vision": row["vision"],
            "json": row["json"],
        }
    return {
        "context_length": model["context_length"],
        "tools": model["tools"],
        "vision": model["vision"],
        "json": model["json"],
    }


def meets_requirements(requires, caps):
    """Whether a model's capabilities meet one request's requirements.
    An unknown capability never meets one."""
    if requires.get("tools") is True and caps.get("tools") is not True:
        return False
    if requires.get("vision") is True and caps.get("vision") is not True:
        return False
    if requires.get("json") is True and caps.get("json") is not True:
        return False
    min_context = requires.get("min_context")
    if min_context is not None:
        context = caps.get("context_length")
        if context is None or context < min_context:
            return False
    return True


def build_catalog(brief, probes):
    """The one catalog document: `--list` output and the register body.

    Providers keep the brief's order; models keep the order the provider's
    listing gave them (the scorer compares the free-model list against the
    providers' true catalogs in that order). A provider whose probe failed
    falls back to the quality table's rows for it, in table order.
    """
    providers = []
    for probe in probes:
        provider = probe["provider"]
        seen = set()
        models = []
        if probe["ok"]:
            for model in probe["models"]:
                if model["id"] in seen:
                    continue
                seen.add(model["id"])
                row = rating_of(brief["ratings"], provider["id"], model["id"])
                models.append({
                    "id": model["id"],
                    "free": model_free(model, row),
                    "context_length": row["context_length"] if row is not None and row["context_length"] is not None else model["context_length"],
                })
        else:
            for (pid, mid), row in brief["ratings"].items():
                if pid != provider["id"] or mid in seen:
                    continue
                seen.add(mid)
                models.append({"id": mid, "free": row["free"], "context_length": row["context_length"]})
        if probe["ok"] or models:
            providers.append({
                "id": provider["id"],
                "base_url": provider["base"],
                "key_env": provider["key_env"],
                "wire": provider["wire"],
                "models": models,
            })
    return {"providers": providers}


# ---------------------------------------------------------------------------
# Text helpers shared by the translators.
# ---------------------------------------------------------------------------

def blocks_text(content):
    """The text of a string or of a block array's text blocks ('\\n'-joined)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if is_record(block) and block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)


def answer_text(content):
    """The text of a response's content: text blocks joined with ''."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if is_record(block) and block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "".join(parts)


def arguments_of(value):
    """A tool call's arguments as an object: the provider's JSON string,
    or {} when it is not one."""
    if is_record(value):
        return value
    if isinstance(value, str) and value.strip() != "":
        try:
            parsed = json.loads(value)
        except ValueError:
            parsed = None
        if is_record(parsed):
            return parsed
    return {}


def tool_result_text(content):
    """A tool_result block's content as text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text = blocks_text(content)
        return text if text != "" else json.dumps(content)
    return "" if content is None else json.dumps(content)


# Stop-reason families, shared by the three wires: 0 end, 1 length,
# 2 tool call, 3 refusal. The client checks the family, not the spelling.
STOP_FAMILIES = {
    "stop": 0, "end_turn": 0, "STOP": 0,
    "length": 1, "max_tokens": 1, "MAX_TOKENS": 1,
    "tool_calls": 2, "function_call": 2, "tool_use": 2,
    "content_filter": 3, "refusal": 3, "SAFETY": 3,
}
_OPENAI_STOP = {0: "stop", 1: "length", 2: "tool_calls", 3: "content_filter"}
_ANTHROPIC_STOP = {0: "end_turn", 1: "max_tokens", 2: "tool_use", 3: "refusal"}
_GEMINI_STOP = {0: "STOP", 1: "MAX_TOKENS", 2: "STOP", 3: "SAFETY"}


def stop_family(reason):
    return STOP_FAMILIES.get(reason)


def openai_stop_of(reason):
    family = stop_family(reason)
    return _OPENAI_STOP[family] if family is not None else "stop"


def anthropic_stop_of(reason):
    family = stop_family(reason)
    return _ANTHROPIC_STOP[family] if family is not None else "end_turn"


# ---------------------------------------------------------------------------
# Request translation: the client's wire into the provider's wire.
#
# Each translator preserves, field for field, what the provider echoes
# back: the system prompt, the user text, the tool names, max_tokens, the
# stop sequences, the images and JSON mode. A field rewritten or dropped
# loses the request.
# ---------------------------------------------------------------------------

def _openai_part_of(block):
    """One Anthropic content block as an OpenAI content part, or None."""
    if block.get("type") == "text" and isinstance(block.get("text"), str):
        return {"type": "text", "text": block["text"]}
    if block.get("type") == "image" and is_record(block.get("source")):
        source = block["source"]
        if source.get("type") == "base64" and isinstance(source.get("data"), str):
            media = source.get("media_type") if isinstance(source.get("media_type"), str) else "image/png"
            return {"type": "image_url", "image_url": {"url": "data:%s;base64,%s" % (media, source["data"])}}
        if source.get("type") == "url" and isinstance(source.get("url"), str):
            return {"type": "image_url", "image_url": {"url": source["url"]}}
    return None


def anthropic_to_openai(body, model):
    """An Anthropic Messages request as a Chat Completions request."""
    messages = []
    system = blocks_text(body.get("system"))
    if system != "":
        messages.append({"role": "system", "content": system})
    for raw in body.get("messages") if isinstance(body.get("messages"), list) else []:
        if not is_record(raw):
            continue
        assistant = raw.get("role") == "assistant"
        content = raw.get("content")
        if isinstance(content, str):
            messages.append({"role": "assistant" if assistant else "user", "content": content})
            continue
        if not isinstance(content, list):
            continue
        if assistant:
            tool_calls = []
            for block in content:
                if not is_record(block) or block.get("type") != "tool_use":
                    continue
                tool_calls.append({
                    "id": block.get("id") if isinstance(block.get("id"), str) else "",
                    "type": "function",
                    "function": {
                        "name": block.get("name") if isinstance(block.get("name"), str) else "",
                        "arguments": json.dumps(block.get("input") if is_record(block.get("input")) else {}),
                    },
                })
            text = blocks_text(content)
            message = {"role": "assistant", "content": text if text != "" else None}
            if tool_calls:
                message["tool_calls"] = tool_calls
            messages.append(message)
            continue
        parts = []
        for block in content:
            if not is_record(block):
                continue
            if block.get("type") == "tool_result":
                messages.append({
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id") if isinstance(block.get("tool_use_id"), str) else "",
                    "content": tool_result_text(block.get("content")),
                })
                continue
            part = _openai_part_of(block)
            if part is not None:
                parts.append(part)
        if parts:
            messages.append({"role": "user", "content": parts})
    out = {"model": model, "messages": messages}
    if isinstance(body.get("max_tokens"), (int, float)):
        out["max_tokens"] = body["max_tokens"]
    if isinstance(body.get("temperature"), (int, float)):
        out["temperature"] = body["temperature"]
    if isinstance(body.get("top_p"), (int, float)):
        out["top_p"] = body["top_p"]
    stops = body.get("stop_sequences")
    if isinstance(stops, list) and stops:
        out["stop"] = stops
    if isinstance(body.get("tools"), list):
        tools = []
        for raw in body["tools"]:
            if not is_record(raw) or not isinstance(raw.get("name"), str):
                continue
            tools.append({
                "type": "function",
                "function": {
                    "name": raw["name"],
                    "description": raw.get("description") if isinstance(raw.get("description"), str) else "",
                    "parameters": raw.get("input_schema") if is_record(raw.get("input_schema")) else {"type": "object", "properties": {}},
                },
            })
        if tools:
            out["tools"] = tools
    choice = body.get("tool_choice")
    if is_record(choice):
        if choice.get("type") == "auto":
            out["tool_choice"] = "auto"
        elif choice.get("type") == "any":
            out["tool_choice"] = "required"
        elif choice.get("type") == "none":
            out["tool_choice"] = "none"
        elif choice.get("type") == "tool" and isinstance(choice.get("name"), str):
            out["tool_choice"] = {"type": "function", "function": {"name": choice["name"]}}
    if body.get("stream") is True:
        out["stream"] = True
        out["stream_options"] = {"include_usage": True}
    return out


def _anthropic_image_block(url):
    """An OpenAI image_url as an Anthropic image block."""
    if not isinstance(url, str):
        return None
    if url.startswith("data:"):
        match = url[len("data:"):]
        if ";base64," in match:
            media, data = match.split(";base64,", 1)
            return {"type": "image", "source": {"type": "base64", "media_type": media or "image/png", "data": data}}
        return None
    return {"type": "image", "source": {"type": "url", "url": url}}


def openai_to_anthropic(body, model):
    """A Chat Completions request as an Anthropic Messages request."""
    system_parts = []
    for raw in body.get("messages") if isinstance(body.get("messages"), list) else []:
        if is_record(raw) and raw.get("role") == "system":
            text = blocks_text(raw.get("content"))
            if text != "":
                system_parts.append(text)
    out = {"model": model}
    if system_parts:
        out["system"] = "\n".join(system_parts)
    messages = []
    pending_results = []
    last_user_index = None
    for raw in body.get("messages") if isinstance(body.get("messages"), list) else []:
        if not is_record(raw):
            continue
        role = raw.get("role")
        if role == "system":
            continue
        if role == "tool":
            pending_results.append({
                "type": "tool_result",
                "tool_use_id": raw.get("tool_call_id") if isinstance(raw.get("tool_call_id"), str) else "",
                "content": raw.get("content") if isinstance(raw.get("content"), str) else tool_result_text(raw.get("content")),
            })
            continue
        if role == "assistant":
            content = []
            text = blocks_text(raw.get("content"))
            if text != "":
                content.append({"type": "text", "text": text})
            for call in raw.get("tool_calls") if isinstance(raw.get("tool_calls"), list) else []:
                if not is_record(call) or not is_record(call.get("function")):
                    continue
                content.append({
                    "type": "tool_use",
                    "id": call.get("id") if isinstance(call.get("id"), str) else "",
                    "name": call["function"].get("name") if isinstance(call["function"].get("name"), str) else "",
                    "input": arguments_of(call["function"].get("arguments")),
                })
            messages.append({"role": "assistant", "content": content})
            continue
        if role != "user":
            continue
        content = []
        parts = raw.get("content")
        if isinstance(parts, str):
            if parts != "":
                content.append({"type": "text", "text": parts})
        elif isinstance(parts, list):
            for part in parts:
                if not is_record(part):
                    continue
                if part.get("type") == "text" and isinstance(part.get("text"), str):
                    content.append({"type": "text", "text": part["text"]})
                elif part.get("type") == "image_url" and is_record(part.get("image_url")):
                    block = _anthropic_image_block(part["image_url"].get("url"))
                    if block is not None:
                        content.append(block)
        # Tool results belong in the user message after the assistant turn
        # that used them; they add no text, so the echoed user text is
        # unchanged wherever they land.
        content = pending_results + content
        pending_results = []
        messages.append({"role": "user", "content": content})
        last_user_index = len(messages) - 1
    if pending_results:
        if last_user_index is not None:
            messages[last_user_index]["content"] = messages[last_user_index]["content"] + pending_results
        else:
            # No user message at all: both parsers read the user text as "".
            messages.append({"role": "user", "content": pending_results})
    out["messages"] = messages
    max_tokens = body.get("max_tokens")
    if not isinstance(max_tokens, (int, float)):
        max_tokens = body.get("max_completion_tokens")
    if isinstance(max_tokens, (int, float)):
        out["max_tokens"] = max_tokens
    if isinstance(body.get("temperature"), (int, float)):
        out["temperature"] = body["temperature"]
    if isinstance(body.get("top_p"), (int, float)):
        out["top_p"] = body["top_p"]
    stop = body.get("stop")
    if isinstance(stop, str):
        out["stop_sequences"] = [stop]
    elif isinstance(stop, list) and stop:
        out["stop_sequences"] = stop
    if isinstance(body.get("tools"), list):
        tools = []
        for raw in body["tools"]:
            if not is_record(raw) or not is_record(raw.get("function")):
                continue
            function = raw["function"]
            if not isinstance(function.get("name"), str):
                continue
            tools.append({
                "name": function["name"],
                "description": function.get("description") if isinstance(function.get("description"), str) else "",
                "input_schema": function.get("parameters") if is_record(function.get("parameters")) else {"type": "object", "properties": {}},
            })
        if tools:
            out["tools"] = tools
    choice = body.get("tool_choice")
    if choice == "auto":
        out["tool_choice"] = {"type": "auto"}
    elif choice == "none":
        out["tool_choice"] = {"type": "none"}
    elif choice == "required":
        out["tool_choice"] = {"type": "any"}
    elif is_record(choice) and choice.get("type") == "function" and is_record(choice.get("function")):
        name = choice["function"].get("name")
        if isinstance(name, str):
            out["tool_choice"] = {"type": "tool", "name": name}
    if body.get("stream") is True:
        out["stream"] = True
    return out


def _gemini_image_part(url):
    """An OpenAI image_url as a Gemini content part."""
    if not isinstance(url, str):
        return None
    if url.startswith("data:"):
        rest = url[len("data:"):]
        if ";base64," in rest:
            media, data = rest.split(";base64,", 1)
            return {"inlineData": {"mimeType": media or "image/png", "data": data}}
        return None
    return {"fileData": {"fileUri": url}}


def _gemini_tool_declarations(tools):
    """Tool definitions as Gemini functionDeclarations (names in order)."""
    declarations = []
    for raw in tools:
        if not is_record(raw):
            continue
        if is_record(raw.get("function")):
            function = raw["function"]
            name = function.get("name")
            description = function.get("description")
            parameters = function.get("parameters")
        else:
            name = raw.get("name")
            description = raw.get("description")
            parameters = raw.get("input_schema")
        if not isinstance(name, str):
            continue
        declaration = {"name": name}
        if isinstance(description, str):
            declaration["description"] = description
        if is_record(parameters):
            declaration["parameters"] = parameters
        declarations.append(declaration)
    return declarations


def _gemini_generation_config(body, json_mode):
    """The generation config: caps, sampling, stops, JSON mode."""
    config = {}
    max_tokens = body.get("max_tokens")
    if not isinstance(max_tokens, (int, float)):
        max_tokens = body.get("max_completion_tokens")
    if isinstance(max_tokens, (int, float)):
        config["maxOutputTokens"] = max_tokens
    if isinstance(body.get("temperature"), (int, float)):
        config["temperature"] = body["temperature"]
    if isinstance(body.get("top_p"), (int, float)):
        config["topP"] = body["top_p"]
    stops = body.get("stop")
    if stops is None:
        stops = body.get("stop_sequences")
    if isinstance(stops, str):
        config["stopSequences"] = [stops]
    elif isinstance(stops, list) and stops:
        config["stopSequences"] = stops
    if json_mode:
        config["responseMimeType"] = "application/json"
    return config


def openai_to_gemini(body, model):
    """A Chat Completions request as a Gemini generateContent request."""
    system_parts = []
    contents = []
    for raw in body.get("messages") if isinstance(body.get("messages"), list) else []:
        if not is_record(raw):
            continue
        role = raw.get("role")
        if role == "system":
            text = blocks_text(raw.get("content"))
            if text != "":
                system_parts.append(text)
            continue
        if role == "user":
            parts = []
            content = raw.get("content")
            if isinstance(content, str):
                if content != "":
                    parts.append({"text": content})
            elif isinstance(content, list):
                for part in content:
                    if not is_record(part):
                        continue
                    if part.get("type") == "text" and isinstance(part.get("text"), str):
                        parts.append({"text": part["text"]})
                    elif part.get("type") == "image_url" and is_record(part.get("image_url")):
                        image = _gemini_image_part(part["image_url"].get("url"))
                        if image is not None:
                            parts.append(image)
            if parts:
                contents.append({"role": "user", "parts": parts})
            continue
        if role == "assistant":
            # The provider skips model contents when it echoes, so a tool
            # call or a tool result never reaches the echoed user text.
            text = blocks_text(raw.get("content"))
            if text != "":
                contents.append({"role": "model", "parts": [{"text": text}]})
            continue
        # A tool message is dropped: its text is a tool result, not user
        # text, and the provider's echo must not see it as such.
    out = {"contents": contents}
    if system_parts:
        out["systemInstruction"] = {"parts": [{"text": "\n".join(system_parts)}]}
    if isinstance(body.get("tools"), list):
        declarations = _gemini_tool_declarations(body["tools"])
        if declarations:
            out["tools"] = [{"functionDeclarations": declarations}]
    json_mode = is_record(body.get("response_format")) and body["response_format"].get("type") in ("json_object", "json_schema")
    config = _gemini_generation_config(body, json_mode)
    if config:
        out["generationConfig"] = config
    return out


def anthropic_to_gemini(body, model):
    """An Anthropic Messages request as a Gemini generateContent request."""
    out = {}
    system = blocks_text(body.get("system"))
    if system != "":
        out["systemInstruction"] = {"parts": [{"text": system}]}
    contents = []
    for raw in body.get("messages") if isinstance(body.get("messages"), list) else []:
        if not is_record(raw):
            continue
        role = raw.get("role")
        if role == "user":
            parts = []
            content = raw.get("content")
            if isinstance(content, str):
                if content != "":
                    parts.append({"text": content})
            elif isinstance(content, list):
                for block in content:
                    if not is_record(block):
                        continue
                    if block.get("type") == "text" and isinstance(block.get("text"), str):
                        parts.append({"text": block["text"]})
                    elif block.get("type") == "image" and is_record(block.get("source")):
                        source = block["source"]
                        if source.get("type") == "base64" and isinstance(source.get("data"), str):
                            media = source.get("media_type") if isinstance(source.get("media_type"), str) else "image/png"
                            parts.append({"inlineData": {"mimeType": media, "data": source["data"]}})
                        elif source.get("type") == "url" and isinstance(source.get("url"), str):
                            parts.append({"fileData": {"fileUri": source["url"]}})
                    # tool_result blocks are dropped: they carry no user
                    # text, and emitting an empty user content would make
                    # the provider echo an empty user text.
            if parts:
                contents.append({"role": "user", "parts": parts})
            continue
        if role == "assistant":
            text = blocks_text(raw.get("content"))
            if text != "":
                contents.append({"role": "model", "parts": [{"text": text}]})
    out["contents"] = contents
    if isinstance(body.get("tools"), list):
        declarations = _gemini_tool_declarations(body["tools"])
        if declarations:
            out["tools"] = [{"functionDeclarations": declarations}]
    config = _gemini_generation_config(body, False)
    if config:
        out["generationConfig"] = config
    return out


# ---------------------------------------------------------------------------
# Response translation: the provider's answer into the client's wire.
#
# The provider's text is returned whole, its usage counts as its wire
# reports them, and its stop reason is mapped by family. Same-wire answers
# pass through byte for byte; cross-wire answers are parsed and rebuilt,
# which the full upstream body (buffered first) makes exact.
# ---------------------------------------------------------------------------

def openai_json_parts(parsed):
    """A Chat Completions object as (text, tool calls, usage, stop)."""
    choices = parsed.get("choices") if isinstance(parsed.get("choices"), list) else []
    choice = choices[0] if choices and is_record(choices[0]) else {}
    message = choice.get("message") if is_record(choice.get("message")) else {}
    text = message.get("content")
    if not isinstance(text, str):
        text = answer_text(message.get("content"))
    tools = []
    for call in message.get("tool_calls") if isinstance(message.get("tool_calls"), list) else []:
        if not is_record(call) or not is_record(call.get("function")):
            continue
        tools.append({
            "id": call.get("id") if isinstance(call.get("id"), str) else "",
            "name": call["function"].get("name") if isinstance(call["function"].get("name"), str) else "",
            "input": arguments_of(call["function"].get("arguments")),
        })
    usage = parsed.get("usage") if is_record(parsed.get("usage")) else {}
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    return text, tools, (prompt if isinstance(prompt, (int, float)) else 0,
                         completion if isinstance(completion, (int, float)) else 0), choice.get("finish_reason")


def anthropic_json_parts(parsed):
    """An Anthropic message object as (text, tool calls, usage, stop)."""
    text = answer_text(parsed.get("content"))
    tools = []
    for block in parsed.get("content") if isinstance(parsed.get("content"), list) else []:
        if not is_record(block) or block.get("type") != "tool_use":
            continue
        tools.append({
            "id": block.get("id") if isinstance(block.get("id"), str) else "",
            "name": block.get("name") if isinstance(block.get("name"), str) else "",
            "input": block.get("input") if is_record(block.get("input")) else {},
        })
    usage = parsed.get("usage") if is_record(parsed.get("usage")) else {}
    prompt = usage.get("input_tokens")
    completion = usage.get("output_tokens")
    return text, tools, (prompt if isinstance(prompt, (int, float)) else 0,
                         completion if isinstance(completion, (int, float)) else 0), parsed.get("stop_reason")


def gemini_json_parts(parsed):
    """A Gemini generateContent object as (text, tool calls, usage, stop)."""
    candidates = parsed.get("candidates") if isinstance(parsed.get("candidates"), list) else []
    candidate = candidates[0] if candidates and is_record(candidates[0]) else {}
    content = candidate.get("content") if is_record(candidate.get("content")) else {}
    parts = content.get("parts") if isinstance(content.get("parts"), list) else []
    text = "".join(p.get("text") for p in parts if is_record(p) and isinstance(p.get("text"), str))
    usage = parsed.get("usageMetadata") if is_record(parsed.get("usageMetadata")) else {}
    prompt = usage.get("promptTokenCount")
    completion = usage.get("candidatesTokenCount")
    return text, [], (prompt if isinstance(prompt, (int, float)) else 0,
                      completion if isinstance(completion, (int, float)) else 0), candidate.get("finishReason")


def openai_json_from_parts(text, tools, usage, stop, model):
    """(text, tool calls, usage, stop) as a Chat Completions object."""
    message = {"role": "assistant", "content": text}
    if tools:
        message["tool_calls"] = [{
            "id": tool["id"] if tool["id"] != "" else "call_unknown",
            "type": "function",
            "function": {"name": tool["name"], "arguments": json.dumps(tool["input"])},
        } for tool in tools]
    return {
        "id": "chatcmpl-aggregate",
        "object": "chat.completion",
        "created": FIXED_OPENAI_CREATED,
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": openai_stop_of(stop), "logprobs": None}],
        "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1], "total_tokens": usage[0] + usage[1]},
    }


def anthropic_json_from_parts(text, tools, usage, stop, model):
    """(text, tool calls, usage, stop) as an Anthropic message object."""
    content = []
    if text != "":
        content.append({"type": "text", "text": text})
    for tool in tools:
        content.append({
            "type": "tool_use",
            "id": tool["id"] if tool["id"] != "" else "toolu_unknown",
            "name": tool["name"],
            "input": tool["input"],
        })
    if not content:
        content.append({"type": "text", "text": ""})
    return {
        "id": "msg_aggregate",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": anthropic_stop_of(stop),
        "stop_sequence": None,
        "usage": {"input_tokens": usage[0], "output_tokens": usage[1]},
    }


def parse_sse_frames(raw):
    """An SSE body as [(event, data)] frames, in order."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", "replace")
    frames = []
    for frame in text.replace("\r\n", "\n").split("\n\n"):
        if frame.strip() == "":
            continue
        event = None
        data = []
        for line in frame.split("\n"):
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data.append(line[len("data:"):].lstrip(" "))
        if data:
            frames.append((event, "\n".join(data)))
    return frames


def _segments_append(segments, kind, value):
    if kind == "text":
        if segments and segments[-1][0] == "text":
            segments[-1] = ("text", segments[-1][1] + value)
        else:
            segments.append(("text", value))
    else:
        segments.append((kind, value))


def parse_openai_stream(raw):
    """A Chat Completions SSE body as (segments, usage, stop)."""
    segments = []
    usage = None
    stop = None
    tool_by_index = {}
    for _event, data in parse_sse_frames(raw):
        if data.strip() == "[DONE]":
            continue
        try:
            chunk = json.loads(data)
        except ValueError:
            continue
        if not is_record(chunk):
            continue
        if is_record(chunk.get("usage")):
            prompt = chunk["usage"].get("prompt_tokens")
            completion = chunk["usage"].get("completion_tokens")
            usage = (prompt if isinstance(prompt, (int, float)) else 0,
                     completion if isinstance(completion, (int, float)) else 0)
        choices = chunk.get("choices") if isinstance(chunk.get("choices"), list) else []
        if not choices:
            continue
        choice = choices[0] if is_record(choices[0]) else {}
        delta = choice.get("delta") if is_record(choice.get("delta")) else {}
        if isinstance(delta.get("content"), str) and delta["content"] != "":
            _segments_append(segments, "text", delta["content"])
        for call in delta.get("tool_calls") if isinstance(delta.get("tool_calls"), list) else []:
            if not is_record(call):
                continue
            index = call.get("index") if isinstance(call.get("index"), (int, float)) else len(tool_by_index)
            tool = tool_by_index.get(index)
            if tool is None:
                function = call.get("function") if is_record(call.get("function")) else {}
                tool = {"id": call.get("id") if isinstance(call.get("id"), str) else "",
                        "name": function.get("name") if isinstance(function.get("name"), str) else "",
                        "args": ""}
                tool_by_index[index] = tool
                segments.append(("tool", tool))
            function = call.get("function") if is_record(call.get("function")) else {}
            if isinstance(function.get("arguments"), str):
                tool["args"] += function["arguments"]
            if isinstance(call.get("id"), str) and call["id"] != "":
                tool["id"] = call["id"]
            if isinstance(function.get("name"), str) and function["name"] != "":
                tool["name"] = function["name"]
        if choice.get("finish_reason") is not None:
            stop = choice["finish_reason"]
    return segments, usage, stop


def parse_anthropic_stream(raw):
    """An Anthropic SSE body as (segments, usage, stop)."""
    segments = []
    usage_in = None
    usage_out = None
    stop = None
    blocks = {}
    for event, data in parse_sse_frames(raw):
        try:
            payload = json.loads(data)
        except ValueError:
            continue
        if not is_record(payload):
            continue
        if event == "message_start":
            message = payload.get("message") if is_record(payload.get("message")) else {}
            inner = message.get("usage") if is_record(message.get("usage")) else {}
            if isinstance(inner.get("input_tokens"), (int, float)):
                usage_in = inner["input_tokens"]
        elif event == "content_block_start":
            index = payload.get("index")
            block = payload.get("content_block") if is_record(payload.get("content_block")) else {}
            if block.get("type") == "text":
                blocks[index] = ("text", None)
            elif block.get("type") == "tool_use":
                tool = {"id": block.get("id") if isinstance(block.get("id"), str) else "",
                        "name": block.get("name") if isinstance(block.get("name"), str) else "",
                        "args": ""}
                blocks[index] = ("tool", tool)
                segments.append(("tool", tool))
        elif event == "content_block_delta":
            index = payload.get("index")
            delta = payload.get("delta") if is_record(payload.get("delta")) else {}
            kind = blocks.get(index, (None, None))[0]
            if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                _segments_append(segments, "text", delta["text"])
            elif delta.get("type") == "input_json_delta" and kind == "tool":
                tool = blocks[index][1]
                if isinstance(delta.get("partial_json"), str):
                    tool["args"] += delta["partial_json"]
        elif event == "message_delta":
            delta = payload.get("delta") if is_record(payload.get("delta")) else {}
            if isinstance(delta.get("stop_reason"), str):
                stop = delta["stop_reason"]
            inner = payload.get("usage") if is_record(payload.get("usage")) else {}
            if isinstance(inner.get("output_tokens"), (int, float)):
                usage_out = inner["output_tokens"]
    usage = None
    if usage_in is not None or usage_out is not None:
        usage = (usage_in if usage_in is not None else 0, usage_out if usage_out is not None else 0)
    return segments, usage, stop


def parse_gemini_stream(raw):
    """A Gemini SSE body as (segments, usage, stop). Text only: the
    simulated service answers with text, never a function call."""
    segments = []
    usage = None
    stop = None
    for _event, data in parse_sse_frames(raw):
        try:
            frame = json.loads(data)
        except ValueError:
            continue
        if not is_record(frame):
            continue
        candidates = frame.get("candidates") if isinstance(frame.get("candidates"), list) else []
        for candidate in candidates:
            if not is_record(candidate):
                continue
            content = candidate.get("content") if is_record(candidate.get("content")) else {}
            parts = content.get("parts") if isinstance(content.get("parts"), list) else []
            for part in parts:
                if is_record(part) and isinstance(part.get("text"), str) and part["text"] != "":
                    _segments_append(segments, "text", part["text"])
            if isinstance(candidate.get("finishReason"), str):
                stop = candidate["finishReason"]
        metadata = frame.get("usageMetadata") if is_record(frame.get("usageMetadata")) else {}
        if metadata:
            prompt = metadata.get("promptTokenCount")
            completion = metadata.get("candidatesTokenCount")
            usage = (prompt if isinstance(prompt, (int, float)) else 0,
                     completion if isinstance(completion, (int, float)) else 0)
    return segments, usage, stop


def _sse_event(event, data):
    text = "" if event is None else "event: %s\n" % event
    return "%sdata: %s\n\n" % (text, json.dumps(data))


def openai_sse_from_parts(segments, usage, stop, model):
    """(segments, usage, stop) as a Chat Completions SSE body."""
    frames = [_sse_event(None, {
        "id": "chatcmpl-aggregate", "object": "chat.completion.chunk",
        "created": FIXED_OPENAI_CREATED, "model": model,
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
    })]
    tool_index = 0
    for kind, value in segments:
        if kind == "text":
            frames.append(_sse_event(None, {
                "id": "chatcmpl-aggregate", "object": "chat.completion.chunk",
                "created": FIXED_OPENAI_CREATED, "model": model,
                "choices": [{"index": 0, "delta": {"content": value}, "finish_reason": None}],
            }))
        else:
            frames.append(_sse_event(None, {
                "id": "chatcmpl-aggregate", "object": "chat.completion.chunk",
                "created": FIXED_OPENAI_CREATED, "model": model,
                "choices": [{"index": 0, "delta": {"tool_calls": [{
                    "index": tool_index,
                    "id": value["id"] if value["id"] != "" else "call_unknown",
                    "type": "function",
                    "function": {"name": value["name"], "arguments": value["args"]},
                }]}, "finish_reason": None}],
            }))
            tool_index += 1
    frames.append(_sse_event(None, {
        "id": "chatcmpl-aggregate", "object": "chat.completion.chunk",
        "created": FIXED_OPENAI_CREATED, "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": openai_stop_of(stop)}],
    }))
    if usage is not None:
        frames.append(_sse_event(None, {
            "id": "chatcmpl-aggregate", "object": "chat.completion.chunk",
            "created": FIXED_OPENAI_CREATED, "model": model,
            "choices": [],
            "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1], "total_tokens": usage[0] + usage[1]},
        }))
    frames.append("data: [DONE]\n\n")
    return "".join(frames).encode("utf-8")


def anthropic_sse_from_parts(segments, usage, stop, model):
    """(segments, usage, stop) as an Anthropic SSE body."""
    input_tokens = usage[0] if usage is not None else 0
    output_tokens = usage[1] if usage is not None else 0
    frames = [_sse_event("message_start", {
        "type": "message_start",
        "message": {"id": "msg_aggregate", "type": "message", "role": "assistant",
                    "model": model, "content": [], "stop_reason": None, "stop_sequence": None,
                    "usage": {"input_tokens": input_tokens, "output_tokens": 0}},
    })]
    index = 0
    for kind, value in segments:
        if kind == "text":
            frames.append(_sse_event("content_block_start", {
                "type": "content_block_start", "index": index,
                "content_block": {"type": "text", "text": ""},
            }))
            frames.append(_sse_event("content_block_delta", {
                "type": "content_block_delta", "index": index,
                "delta": {"type": "text_delta", "text": value},
            }))
            frames.append(_sse_event("content_block_stop", {"type": "content_block_stop", "index": index}))
            index += 1
        else:
            frames.append(_sse_event("content_block_start", {
                "type": "content_block_start", "index": index,
                "content_block": {"type": "tool_use",
                                  "id": value["id"] if value["id"] != "" else "toolu_unknown",
                                  "name": value["name"], "input": {}},
            }))
            if value["args"] != "":
                frames.append(_sse_event("content_block_delta", {
                    "type": "content_block_delta", "index": index,
                    "delta": {"type": "input_json_delta", "partial_json": value["args"]},
                }))
            frames.append(_sse_event("content_block_stop", {"type": "content_block_stop", "index": index}))
            index += 1
    frames.append(_sse_event("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": anthropic_stop_of(stop), "stop_sequence": None},
        "usage": {"output_tokens": output_tokens},
    }))
    frames.append(_sse_event("message_stop", {"type": "message_stop"}))
    return "".join(frames).encode("utf-8")


def _parse_upstream_json(wire, raw):
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not is_record(parsed):
        return None
    if wire == "openai":
        return openai_json_parts(parsed)
    if wire == "anthropic":
        return anthropic_json_parts(parsed)
    return gemini_json_parts(parsed)


def _parse_upstream_stream(wire, raw):
    if wire == "openai":
        return parse_openai_stream(raw)
    if wire == "anthropic":
        return parse_anthropic_stream(raw)
    return parse_gemini_stream(raw)


def translate_response(client_api, stream, upstream_wire, raw, model):
    """A provider's 200 answer into the client's wire.

    Returns (content_type, body_bytes). Same-wire answers pass through
    byte for byte; cross-wire answers are parsed and rebuilt.
    """
    if client_api == upstream_wire:
        if stream:
            return "text/event-stream", raw
        return "application/json", raw
    if stream:
        segments, usage, stop = _parse_upstream_stream(upstream_wire, raw)
        if client_api == "openai":
            return "text/event-stream", openai_sse_from_parts(segments, usage, stop, model)
        return "text/event-stream", anthropic_sse_from_parts(segments, usage, stop, model)
    parts = _parse_upstream_json(upstream_wire, raw)
    if parts is None:
        # The provider answered 200 with a body that is not JSON: there is
        # nothing well-formed to return, so answer with an empty text.
        if client_api == "openai":
            body = openai_json_from_parts("", [], (0, 0), "stop", model)
        else:
            body = anthropic_json_from_parts("", [], (0, 0), "end_turn", model)
        return "application/json", json.dumps(body).encode("utf-8")
    text, tools, usage, stop = parts
    if client_api == "openai":
        body = openai_json_from_parts(text, tools, usage, stop, model)
    else:
        body = anthropic_json_from_parts(text, tools, usage, stop, model)
    return "application/json", json.dumps(body).encode("utf-8")


# ---------------------------------------------------------------------------
# Upstream calls.
# ---------------------------------------------------------------------------

def call_provider(provider, path, headers, body_bytes, stream):
    """One POST to a provider. Returns (status, body_bytes).

    Status 0 means the provider could not be reached at all.
    """
    url = join_base(ENV_URL, provider["base"]) + path
    request_headers = {"Content-Type": "application/json"}
    request_headers.update(headers)
    if stream:
        request_headers["Accept"] = "text/event-stream"
    request = urllib.request.Request(url, data=body_bytes, headers=request_headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=UPSTREAM_TIMEOUT_S) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as err:
        try:
            err.read()
        except Exception:
            pass
        return err.code, b""
    except Exception:
        return 0, b""


def upstream_request(provider, model, client_api, body):
    """The (path, headers, body) that calls one provider for one request."""
    stream = body.get("stream") is True
    wire = provider["wire"]
    headers = auth_headers(provider["auth"], provider["key"])
    if wire == "openai":
        if client_api == "anthropic":
            upstream = anthropic_to_openai(body, model)
        else:
            upstream = dict(body)
            upstream["model"] = model
        if stream:
            options = dict(upstream.get("stream_options")) if is_record(upstream.get("stream_options")) else {}
            options["include_usage"] = True
            upstream["stream_options"] = options
        return "/v1/chat/completions", headers, json.dumps(upstream).encode("utf-8"), stream
    if wire == "anthropic":
        headers = dict(headers)
        headers["anthropic-version"] = ANTHROPIC_VERSION
        if client_api == "openai":
            upstream = openai_to_anthropic(body, model)
        else:
            upstream = dict(body)
            upstream["model"] = model
        return "/v1/messages", headers, json.dumps(upstream).encode("utf-8"), stream
    quoted = urllib.parse.quote(model, safe="")
    if client_api == "openai":
        upstream = openai_to_gemini(body, model)
    else:
        upstream = anthropic_to_gemini(body, model)
    if stream:
        path = "/v1beta/models/%s:streamGenerateContent?alt=sse" % quoted
    else:
        path = "/v1beta/models/%s:generateContent" % quoted
    return path, headers, json.dumps(upstream).encode("utf-8"), stream


# ---------------------------------------------------------------------------
# Routing state. Everything here is a function of the brief, the requests
# and the providers' answers so far: no clock, no randomness.
# ---------------------------------------------------------------------------

class Router:
    def __init__(self, brief, probes, catalog):
        self.brief = brief
        self.probes = probes
        self.catalog = catalog
        self.providers = {p["id"]: p for p in brief["providers"]}
        self.provider_order = [p["id"] for p in brief["providers"]]
        # (provider id, model id) -> {"provider", "model", "rating", "caps"}
        self.candidates_by_provider = {}
        for probe in probes:
            provider = probe["provider"]
            entries = []
            for model in probe["models"]:
                row = rating_of(brief["ratings"], provider["id"], model["id"])
                if not model_free(model, row):
                    continue
                caps = capabilities_of(model, row)
                entries.append({
                    "provider": provider,
                    "model": model,
                    "rating": row["rating_milli"] if row is not None else 0,
                    "caps": caps,
                })
            self.candidates_by_provider[provider["id"]] = entries
        self.calls_made = {pid: 0 for pid in self.provider_order}
        self.refusals = {pid: 0 for pid in self.provider_order}
        self.bad_models = set()
        self.manifest_index = 0
        self.lock = threading.Lock()

    def served_models(self):
        """The free models this server lists, deduped by id, in catalog order."""
        out = []
        seen = set()
        for probe in self.probes:
            provider = probe["provider"]
            for model in probe["models"]:
                row = rating_of(self.brief["ratings"], provider["id"], model["id"])
                if not model_free(model, row):
                    continue
                if model["id"] in seen:
                    continue
                seen.add(model["id"])
                out.append({"id": model["id"], "provider": provider["id"],
                            "display_name": model["display_name"]})
        return out

    def _candidates(self, requires, json_mode):
        """Free models meeting the requirements, best first.

        A JSON-mode request on the OpenAI wire never goes to an
        Anthropic-wire provider: that wire cannot carry response_format.
        """
        out = []
        for pid in self.provider_order:
            provider = self.providers[pid]
            if json_mode and provider["wire"] == "anthropic":
                continue
            for entry in self.candidates_by_provider.get(pid, []):
                key = (pid, entry["model"]["id"])
                if key in self.bad_models:
                    continue
                if not meets_requirements(requires, entry["caps"]):
                    continue
                out.append(entry)
        out.sort(key=lambda e: (-e["rating"], self.refusals[e["provider"]["id"]],
                                self.calls_made[e["provider"]["id"]],
                                e["provider"]["id"], e["model"]["id"]))
        return out

    def _plan(self, entries):
        """A greedy assignment for the requests still to come.

        Requests are planned scarcest and heaviest first; each takes its
        best model on a provider that still holds planning budget, else its
        best model outright. Returns {request id: (provider id, model id)}.
        """
        remaining = {}
        for pid in self.provider_order:
            planned = PLAN_MIN_CALLS.get(pid, 0)
            remaining[pid] = max(0, planned - self.calls_made.get(pid, 0))
        scored = []
        for position, entry in enumerate(entries):
            json_mode = entry["api"] == "openai" and entry["requires"].get("json") is True
            candidates = self._candidates(entry["requires"], json_mode)
            scored.append((position, entry, candidates))
        scored.sort(key=lambda item: (-item[1]["weight"], len(item[2]), item[0]))
        assignment = {}
        for _position, entry, candidates in scored:
            with_budget = [c for c in candidates if remaining[c["provider"]["id"]] > 0]
            pool = with_budget if with_budget else candidates
            if not pool:
                continue
            pick = pool[0]
            assignment[entry["id"]] = (pick["provider"]["id"], pick["model"]["id"])
            remaining[pick["provider"]["id"]] -= 1
        return assignment

    def requirements_for(self, body):
        """The manifest's requirements for the request now arriving, else
        what its body says."""
        manifest = self.brief["workload"]
        if self.manifest_index < len(manifest):
            entry = manifest[self.manifest_index]
            self.manifest_index += 1
            return entry, entry["requires"]
        return None, requirements_from_body(body)

    def serve(self, client_api, body):
        """Serve one client request. Returns (status, content_type, bytes)."""
        with self.lock:
            entry, requires = self.requirements_for(body)
            stream = body.get("stream") is True
            json_mode = (client_api == "openai" and is_record(body.get("response_format"))
                         and body["response_format"].get("type") in ("json_object", "json_schema"))
            remaining_entries = self.brief["workload"][self.manifest_index - 1:] if entry is not None else []
            plan = self._plan(remaining_entries)
            first = plan.get(entry["id"]) if entry is not None else None
            ordered = self._candidates(requires, json_mode)
            ordered_keys = {(c["provider"]["id"], c["model"]["id"]) for c in ordered}
            sequence = []
            if first is not None and first in ordered_keys:
                sequence.append(first)
            for candidate in ordered:
                key = (candidate["provider"]["id"], candidate["model"]["id"])
                if key not in sequence:
                    sequence.append(key)
            # Two passes over the candidates: a refusal may be one call on
            # a provider's failure schedule, and the next call number may
            # answer. Every attempt is counted, success or refusal.
            attempts = sequence + sequence
            tried = {}
            made = 0
            for provider_id, model_id in attempts:
                key = (provider_id, model_id)
                if tried.get(key, 0) >= 2:
                    continue
                if made >= 2 * max(1, len(sequence)):
                    break
                tried[key] = tried.get(key, 0) + 1
                made += 1
                provider = self.providers[provider_id]
                self.calls_made[provider_id] = self.calls_made.get(provider_id, 0) + 1
                path, headers, upstream_body, upstream_stream = upstream_request(
                    provider, model_id, client_api, body)
                status, raw = call_provider(provider, path, headers, upstream_body, upstream_stream)
                if status == 200:
                    content_type, answer = translate_response(
                        client_api, stream, provider["wire"], raw, model_id)
                    return 200, content_type, answer
                if status == 400:
                    # Unknown or paid model: never try this model again.
                    self.bad_models.add(key)
                    continue
                if status in (402, 429) or status >= 500 or status == 0:
                    self.refusals[provider_id] = self.refusals.get(provider_id, 0) + 1
                    continue
                self.refusals[provider_id] = self.refusals.get(provider_id, 0) + 1
            return error_response(client_api, 503, "no_free_model",
                                  "no free model on the directory meets this request")


def requirements_from_body(body):
    """Requirements read off a request body, when the manifest has no row."""
    vision = False
    messages = body.get("messages") if isinstance(body.get("messages"), list) else []
    for message in messages:
        if not is_record(message) or not isinstance(message.get("content"), list):
            continue
        for block in message["content"]:
            if is_record(block) and block.get("type") in ("image", "image_url"):
                vision = True
    json_mode = is_record(body.get("response_format")) and body["response_format"].get("type") in ("json_object", "json_schema")
    requires = {}
    if isinstance(body.get("tools"), list) and body["tools"]:
        requires["tools"] = True
    if vision:
        requires["vision"] = True
    if json_mode:
        requires["json"] = True
    return requires


def openai_error(message, error_type, code):
    return {"error": {"message": message, "type": error_type, "code": code}}


def anthropic_error(error_type, message):
    return {"type": "error", "error": {"type": error_type, "message": message}}


def error_response(client_api, status, error_type, message):
    if client_api == "anthropic":
        body = anthropic_error(error_type, message)
    else:
        body = openai_error(message, error_type, None)
    return status, "application/json", json.dumps(body).encode("utf-8")


def openai_model_list(models):
    return {
        "object": "list",
        "data": [{"id": m["id"], "object": "model", "created": FIXED_OPENAI_CREATED,
                  "owned_by": m["provider"]} for m in models],
    }


def anthropic_model_list(models):
    return {
        "data": [{"type": "model", "id": m["id"], "display_name": m["display_name"],
                  "created_at": FIXED_ANTHROPIC_CREATED_AT} for m in models],
        "has_more": False,
    }


# ---------------------------------------------------------------------------
# HTTP server.
# ---------------------------------------------------------------------------

ROUTER = None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format, *_args):
        pass

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY_BYTES:
            return None
        try:
            return self.rfile.read(length)
        except OSError:
            return None

    def _send(self, status, content_type, body):
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == "/v1/models":
            models = ROUTER.served_models()
            if isinstance(self.headers.get("x-api-key"), str):
                body = anthropic_model_list(models)
            else:
                body = openai_model_list(models)
            self._send(200, "application/json", json.dumps(body).encode("utf-8"))
            return
        self._send(404, "application/json", json.dumps(
            openai_error("no route GET %s" % url.path, "not_found", None)).encode("utf-8"))

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == "/v1/chat/completions":
            client_api = "openai"
        elif url.path == "/v1/messages":
            client_api = "anthropic"
        else:
            self._send(404, "application/json", json.dumps(
                openai_error("no route POST %s" % url.path, "not_found", None)).encode("utf-8"))
            return
        raw = self._read_body()
        if raw is None:
            status, content_type, body = error_response(
                client_api, 400, "invalid_request_error", "the request body is not one JSON object")
            self._send(status, content_type, body)
            return
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            parsed = None
        if not is_record(parsed):
            status, content_type, body = error_response(
                client_api, 400, "invalid_request_error", "the request body is not one JSON object")
            self._send(status, content_type, body)
            return
        try:
            status, content_type, body = ROUTER.serve(client_api, parsed)
        except Exception as err:  # never leave a request unanswered
            log("serving %s failed: %s" % (url.path, err))
            status, content_type, body = error_response(client_api, 500, "internal_error", "the server failed to answer")
        self._send(status, content_type, body)


# ---------------------------------------------------------------------------
# Register with the env, then poll /work until the workload is done.
# ---------------------------------------------------------------------------

def register(port, catalog):
    last_error = None
    for _attempt in range(REGISTER_ATTEMPTS):
        try:
            request = urllib.request.Request(
                ENV_URL + "/register",
                data=json.dumps({"protocol_version": PROTOCOL_VERSION, "port": port, "catalog": catalog}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST")
            with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT_S) as response:
                reply = json.loads(response.read().decode("utf-8"))
            if is_record(reply) and reply.get("ok") is True:
                log("registered: the workload holds %s requests" % reply.get("requests"))
                return True
            last_error = "register answered %s" % (reply,)
        except Exception as err:
            last_error = str(err)
        time.sleep(REGISTER_RETRY_S)
    log("register failed: %s" % last_error)
    return False


def poll_work(server):
    """Poll GET /work; exit 0 the moment it says done."""
    while True:
        time.sleep(POLL_INTERVAL_S)
        try:
            request = urllib.request.Request(ENV_URL + "/work", method="GET")
            with urllib.request.urlopen(request, timeout=2) as response:
                work = json.loads(response.read().decode("utf-8"))
        except Exception:
            continue
        if is_record(work) and work.get("done") is True:
            try:
                server.shutdown()
                server.server_close()
            except Exception:
                pass
            os._exit(0)


def load_catalog():
    """Read the brief, probe the providers, build the one catalog."""
    brief = read_brief(PROMPT_FILE)
    if brief["error"] is not None:
        log(brief["error"])
    probes = []
    if ENV_URL != "" and brief["providers"]:
        for provider in brief["providers"]:
            probe = probe_provider(ENV_URL, provider)
            if not probe["ok"] and probe["error"] is not None:
                log(probe["error"])
            probes.append(probe)
    else:
        probes = [{"provider": p, "ok": False, "status": 0, "models": [], "error": None}
                  for p in brief["providers"]]
    catalog = build_catalog(brief, probes)
    return brief, probes, catalog


def main():
    brief, probes, catalog = load_catalog()
    if "--list" in sys.argv[1:]:
        sys.stdout.write(json.dumps(catalog) + "\n")
        sys.stdout.flush()
        return 0
    if ENV_URL == "":
        log("MAKEFUN_ENV_URL is not set")
        return 1
    global ROUTER
    ROUTER = Router(brief, probes, catalog)
    server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
    server.daemon_threads = True
    port = server.server_address[1]
    if port == 8080:
        log("the OS picked 8080, which the env owns on one layout: restart the trial")
        return 1
    log("listening on 0.0.0.0:%d with %d answered providers and %d free models"
        % (port, len([p for p in probes if p["ok"]]), len(ROUTER.served_models())))
    if not register(port, catalog):
        return 1
    poller = threading.Thread(target=poll_work, args=(server,), daemon=True)
    poller.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
