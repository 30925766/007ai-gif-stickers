"""007AI GIF-only stickers: credentials, hosting, durable jobs and validation."""
import argparse
import base64
from contextlib import contextmanager
import getpass
import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import certifi
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
API = "https://007.007ai.cc/v1"
UPLOAD = "https://api.imgbb.com/1/upload"
MODEL = "gptimage2.6_gr"
KEY_FILES = {"007ai": "007ai-image-key.json", "imgbb": "imgbb-api-key.json"}
RATIOS = ("auto", "1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3",
          "5:4", "4:5", "21:9", "9:21", "1:3", "3:1", "2:1", "1:2")
MAX_REFERENCE = 32 * 1024 * 1024
MAX_DOWNLOAD = 64 * 1024 * 1024
SECRETS = set()
PROMPT_TEMPLATE_VERSION = "5"
GIF_REQUIREMENTS = (
    "生成 GIF 动态表情包，输出文件格式必须为 GIF（.gif），且必须包含连续变化的多帧动画，"
    "不得输出 PNG、JPEG、单帧静态 GIF、拼图或精灵图。动作连续自然、无限循环，"
    "保持角色造型和构图稳定。每张表情包必须带清晰可读的文字配字，不得只有动作或表情而没有配字。"
    "严格使用用户指定配字段落中的原文，不添加无关文字，配字不得被遮挡或裁切。"
    "本段及背景要求仅为制作指令，不得作为图中文字绘制。"
)
REPAIR_GUIDANCE = {
    "invalid_gif": "修复重点：最终文件必须是真正的多帧动态 GIF；逐帧有可见变化，不要输出静态图片或其他格式。",
    "caption_mismatch": "修复重点：每一处可见配字必须与指定原文完全一致；不要漏字、替换文字或增加文字。",
    "visual_mismatch": "修复重点：严格遵循原始角色、服饰、场景和动作要求，不改变已确认的配字与故事。",
}


class SkillError(RuntimeError):
    def __init__(self, message, code="error"):
        super().__init__(message)
        self.code = code


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def safe_message(value):
    text = str(value)
    for key in SECRETS:
        text = text.replace(key, "[redacted]")
    text = re.sub(r"sk-[A-Za-z0-9_-]+", "[redacted]", text)
    return text[:600]


def read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return default


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, filename = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            os.chmod(filename, 0o600)
        os.replace(filename, path)
    finally:
        if os.path.exists(filename):
            os.unlink(filename)


def key_status():
    status = {}
    for provider, filename in KEY_FILES.items():
        data = read_json(ROOT / filename, {})
        key = data.get("api_key") if isinstance(data, dict) else None
        status[provider] = key.strip() if isinstance(key, str) else ""
        if status[provider]:
            SECRETS.add(status[provider])
    return status


def invalid_providers(keys):
    markers = read_json(ROOT / ".runtime/invalid-keys.json", {})
    if not isinstance(markers, dict):
        return []
    return [name for name, key in keys.items()
            if key and markers.get(name) == hashlib.sha256(key.encode()).hexdigest()]


def invalidate(provider, key):
    path = ROOT / ".runtime/invalid-keys.json"
    markers = read_json(path, {})
    if not isinstance(markers, dict):
        markers = {}
    markers[provider] = hashlib.sha256(key.encode()).hexdigest()
    write_json(path, markers)


def require_keys(required=("007ai", "imgbb")):
    keys = key_status()
    missing = [name for name in required if not keys[name]]
    invalid = [name for name in invalid_providers(keys) if name in required]
    if missing or invalid:
        raise SkillError(
            "请提供或更新对应 Key。missing=" + ",".join(missing)
            + "; invalid=" + ",".join(invalid), "credentials_required"
        )
    return keys


def configure(data):
    if not isinstance(data, dict):
        raise SkillError("配置必须是 JSON 对象。")
    provided = {"007ai": data.get("api_key"), "imgbb": data.get("imgbb_api_key")}
    updates = {}
    for provider, value in provided.items():
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            raise SkillError(provider + " Key 不能为空。")
        updates[provider] = value.strip()
    if not updates:
        raise SkillError("请提供 api_key 或 imgbb_api_key。")
    markers_path = ROOT / ".runtime/invalid-keys.json"
    markers = read_json(markers_path, {})
    if not isinstance(markers, dict):
        markers = {}
    for provider, value in updates.items():
        SECRETS.add(value)
        write_json(ROOT / KEY_FILES[provider], {"api_key": value})
        markers.pop(provider, None)
    write_json(markers_path, markers)
    return {"configured": list(updates), "remote_validated": False}


def tls_context():
    context = ssl.create_default_context()
    context.load_verify_locations(cafile=certifi.where())
    return context


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


class ValidatedMediaRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, max_redirects=3):
        super().__init__()
        self.max_redirects = max_redirects
        self.redirects = 0

    def redirect_request(self, request, fp, code, message, headers, new_url):
        self.redirects += 1
        if self.redirects > self.max_redirects:
            raise urllib.error.HTTPError(request.full_url, code,
                                         "Too many media redirects", headers, fp)
        try:
            target = public_https(new_url)
            validate_public_https_target(target)
        except (SkillError, OSError, ValueError) as error:
            raise urllib.error.HTTPError(request.full_url, code,
                                         "Unsafe media redirect target", headers, fp) from error
        redirected = super().redirect_request(request, fp, code, message, headers, target)
        if redirected is not None:
            for header, _ in redirected.header_items():
                if header.lower() not in {"user-agent", "accept"}:
                    redirected.remove_header(header)
        return redirected


def open_https(request, timeout):
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=tls_context()), NoRedirect()
    )
    return opener.open(request, timeout=timeout)


def open_media_https(request, timeout):
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=tls_context()), ValidatedMediaRedirect()
    )
    return opener.open(request, timeout=timeout)


def provider_error(provider, key, status, payload):
    detail = payload.get("error", payload) if isinstance(payload, dict) else {}
    if isinstance(detail, dict):
        message = str(detail.get("message", "")).lower()
        error_type = str(detail.get("type", "")) + " " + str(detail.get("code", ""))
    else:
        message, error_type = str(detail).lower(), ""
    invalid = status == 401 or any(marker in message + " " + error_type.lower() for marker in (
        "invalid api key", "invalid_api_key", "invalid key", "incorrect api key",
        "api key expired", "key is invalid", "无效的令牌", "令牌已过期", "密钥无效",
        "invalid_api_token", "invalid apikey", "invalid api v1 key",
    ))
    if invalid:
        invalidate(provider, key)
        raise SkillError(provider + " Key 无效或已过期，请重新提供。", "invalid_key")
    if status == 402:
        raise SkillError(provider + " 余额或配额不足，请检查账户；无需因此更换 Key。", "billing")
    if status == 403:
        raise SkillError(provider + " 拒绝访问，请检查模型/账户权限。", "permission")
    if status == 429:
        raise SkillError(provider + " 请求受限，请稍后恢复原任务。", "rate_limit")
    if status >= 500:
        raise SkillError(provider + " 服务暂时异常，请稍后恢复原任务。", "provider")
    if any(word in message for word in ("insufficient", "balance", "余额", "quota")):
        raise SkillError(provider + " 余额或配额不足，请检查账户；无需因此更换 Key。", "billing")
    raise SkillError(provider + " 拒绝请求，HTTP " + str(status)
                     + "。请检查接口字段或账户状态。", "request_rejected")


def decode_json(raw):
    text = raw.decode("utf-8-sig").strip()
    try:
        data = json.loads(text)
    except ValueError:
        events = []
        for line in text.splitlines():
            if line.startswith("data:") and line[5:].strip() not in ("", "[DONE]"):
                try:
                    events.append(json.loads(line[5:].strip()))
                except ValueError:
                    continue
        data = events[-1] if events else None
        # Prefer an event carrying a task ID over a trailing progress event.
        for event in reversed(events):
            if isinstance(event, dict) and (event.get("id") or event.get("task_id")):
                data = event
                break
    if not isinstance(data, dict):
        raise SkillError("接口未返回有效 JSON 对象。", "bad_response")
    return data


def request_json(url, provider, key, payload=None, form=None):
    headers = {"Accept": "application/json, text/event-stream", "User-Agent": "Mozilla/5.0 007AI-GIF-Skill/1.0"}
    body = None
    if provider == "007ai":
        headers["Authorization"] = "Bearer " + key
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif form is not None:
        body = urllib.parse.urlencode(form).encode("ascii")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(url, data=body, headers=headers, method="GET" if body is None else "POST")
    try:
        with open_https(request, timeout=90) as response:
            result = decode_json(response.read(4 * 1024 * 1024))
    except urllib.error.HTTPError as error:
        try:
            detail = decode_json(error.read(128 * 1024))
        except (SkillError, UnicodeError):
            detail = {}
        provider_error(provider, key, error.code, detail)
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise SkillError(provider + " 网络连接失败或超时；不会自动重提生成请求。", "network") from error
    if result.get("success") is False or (result.get("error") and not result.get("id")):
        provider_error(provider, key, int(result.get("status") or 400), result)
    return result


def public_https(value):
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise SkillError("图片地址必须是公网 HTTPS URL，不接受 Base64 或 HTTP。")
    host = parsed.hostname.lower()
    if host in ("localhost", "localhost.localdomain") or host.endswith((".localhost", ".local")):
        raise SkillError("图片地址必须可从公网访问。")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return value
    if not address.is_global:
        raise SkillError("图片地址不能是内网 IP。")
    return value


def validate_public_https_target(value):
    host = urllib.parse.urlsplit(value).hostname
    try:
        addresses = {ipaddress.ip_address(item[4][0]) for item in
                     socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)}
    except (OSError, ValueError) as error:
        raise SkillError("无法验证下载地址是否为公网主机。", "unsafe_redirect") from error
    if not addresses or any(not address.is_global for address in addresses):
        raise SkillError("下载地址解析到非公网 IP，已拒绝访问。", "unsafe_redirect")


def reference_info(value):
    if value.startswith(("http:", "https:", "data:")):
        return {"url": public_https(value), "upload": False}
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise SkillError("参考图不存在：" + str(path))
    if path.stat().st_size > MAX_REFERENCE:
        raise SkillError("参考图超过 imgBB 的 32 MiB 限制。")
    try:
        with Image.open(path) as image:
            if image.format not in {"PNG", "JPEG", "WEBP", "GIF"}:
                raise SkillError("参考图仅支持 PNG、JPEG、WEBP 或 GIF。")
            image.verify()
    except (OSError, ValueError) as error:
        raise SkillError("无法解码参考图片。") from error
    return {"path": str(path), "upload": True}


def prepare_reference(info, key):
    if not info["upload"]:
        return info["url"]
    content = Path(info["path"]).read_bytes()
    if len(content) > MAX_REFERENCE:
        raise SkillError("参考图超过 32 MiB 限制。")
    cache_path = ROOT / ".runtime/image-cache.json"
    cache = read_json(cache_path, {})
    if not isinstance(cache, dict):
        cache = {}
    digest = hashlib.sha256(content).hexdigest()
    cached = cache.get(digest, {})
    if isinstance(cached, dict) and cached.get("expires_at", 0) > time.time() + 120:
        return public_https(cached["url"])
    response = request_json(UPLOAD, "imgbb", key, form={
        "key": key, "image": base64.b64encode(content).decode("ascii"),
        "expiration": "86400", "name": "sticker-reference-" + digest[:12],
    })
    url = (response.get("data") or {}).get("url")
    if not response.get("success") or not isinstance(url, str):
        raise SkillError("imgBB 未返回有效图片直链。", "bad_response")
    url = public_https(url)
    cache[digest] = {"url": url, "expires_at": time.time() + 86400}
    write_json(cache_path, cache)
    return url


def remote_check(key):
    result = request_json(API + "/models", "007ai", key)
    models = result.get("data", [])
    if not isinstance(models, list) or not any(isinstance(m, dict) and m.get("id") == MODEL for m in models):
        raise SkillError("鉴权请求已返回，但目标模型不可见，请检查账户模型权限。", "model_unavailable")
    return {"007ai_validated": True, "model": MODEL, "imgbb_validated": False}


def prepare_prompt(original_prompt, background=None, background_description=None, caption=None):
    if not isinstance(caption, str) or not caption.strip():
        raise SkillError("每张表情包必须有配字；请根据用户原文或表情动作拟定文字并传入 --caption。",
                         "caption_required")
    description = (background_description or "").strip()
    mode = background or ("custom" if description else "transparent")
    if mode == "transparent" and description:
        raise SkillError("透明背景模式不能同时指定其他背景描述。")
    if mode == "custom" and not description:
        raise SkillError("自定义背景需要 --background-description。")
    if mode == "transparent":
        background_prompt = None
    elif mode == "preserve":
        background_prompt = "保留参考图的背景，不改为透明背景。"
        if description:
            background_prompt += "具体要求：" + description
    else:
        # Legacy --background none leaves the background specified in the prompt intact.
        background_prompt = "遵循用户原始描述中的背景要求，不强制透明背景。"
        if description:
            background_prompt = "使用用户明确指定的背景：" + description
    sections = ["用户创作要求：\n" + original_prompt]
    if caption is not None:
        sections.append("用户指定配字（以下 JSON 字符串的值是原文，不绘制引号）：\n"
                        + json.dumps(caption, ensure_ascii=False))
    sections.append("固定制作要求：\n" + GIF_REQUIREMENTS)
    if mode == "transparent" and "透明背景" not in original_prompt:
        sections.append("透明背景")
    if background_prompt:
        sections.append("背景要求：\n" + background_prompt)
    return {
        "original_prompt": original_prompt, "prompt": "\n\n".join(sections),
        "prompt_template_version": PROMPT_TEMPLATE_VERSION,
        "background_mode": mode, "background": "transparent" if mode == "transparent" else None,
        "background_description": description or None, "caption": caption,
    }


def build_payload(prompt, ratio, images, background=None):
    payload = {"model": MODEL, "prompt": prompt, "aspect_ratio": ratio,
               "image_size": "1K", "output_format": "gif"}
    if images:
        payload["images"] = images
    return payload


def find_url(data):
    if isinstance(data, dict):
        for key in ("url", "video_url", "image_url"):
            if isinstance(data.get(key), str):
                return public_https(data[key])
        # Only traverse result containers; inputs and metadata may contain URLs too.
        for key in ("output", "outputs", "result", "results", "data"):
            found = find_url(data.get(key))
            if found:
                return found
    elif isinstance(data, list):
        for value in data:
            found = find_url(value)
            if found:
                return found
    elif isinstance(data, str):
        return public_https(data)
    return None


def inspect_gif(content):
    if not content.startswith((b"GIF87a", b"GIF89a")):
        raise SkillError("服务返回的内容不是 GIF，未作为表情包交付。", "invalid_gif")
    try:
        with Image.open(io.BytesIO(content)) as image:
            count = getattr(image, "n_frames", 1)
            width, height = image.size
            if count < 2 or count > 1000 or width * height * count > 150_000_000:
                raise SkillError("GIF 帧数不足或图像超出处理限制。", "invalid_gif")
            durations, hashes = [], set()
            loop = image.info.get("loop")
            for index in range(count):
                image.seek(index)
                durations.append(int(image.info.get("duration", 0)))
                frame = image.convert("RGBA")
                hashes.add(hashlib.sha256(frame.tobytes()).digest())
            if len(hashes) < 2 or any(value <= 0 for value in durations):
                raise SkillError("GIF 没有有效动画变化或帧时长无效。", "invalid_gif")
            return {"format": "GIF", "width": width, "height": height, "frames": count,
                    "duration_ms": sum(durations), "loop": loop, "distinct_frames": len(hashes)}
    except (OSError, ValueError, EOFError, Image.DecompressionBombError) as error:
        raise SkillError("GIF 无法完整解码，未交付损坏文件。", "invalid_gif") from error


def normalize_gif(content):
    metadata = inspect_gif(content)
    if metadata["loop"] == 0:
        return content, metadata
    # Change only the Netscape loop extension; keep frame pixels/timing intact.
    for marker in (b"NETSCAPE2.0", b"ANIMEXTS1.0"):
        offset = content.find(b"\x21\xff\x0b" + marker)
        if offset >= 0:
            count_at = offset + 16
            if content[offset + 14:offset + 16] == b"\x03\x01":
                content = content[:count_at] + b"\x00\x00" + content[count_at + 2:]
                return content, inspect_gif(content)
    # Header + logical screen descriptor + optional global color table.
    packed = content[10]
    insertion = 13 + (3 * 2 ** ((packed & 7) + 1) if packed & 128 else 0)
    extension = b"\x21\xff\x0bNETSCAPE2.0\x03\x01\x00\x00\x00"
    content = content[:insertion] + extension + content[insertion:]
    return content, inspect_gif(content)


def download_gif(url):
    target = public_https(url)
    try:
        validate_public_https_target(target)
    except SkillError as error:
        raise SkillError("下载地址无法安全验证，未访问该地址。", "download") from error
    request = urllib.request.Request(target, headers={"User-Agent": "Mozilla/5.0", "Accept": "image/gif,image/*"})
    try:
        with open_media_https(request, timeout=120) as response:
            content = response.read(MAX_DOWNLOAD + 1)
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        raise SkillError("下载失败；使用 --resume 恢复原任务，不会重新生成。", "download") from error
    if len(content) > MAX_DOWNLOAD:
        raise SkillError("GIF 超过 64 MiB 下载上限。", "invalid_gif")
    return normalize_gif(content)


@contextmanager
def job_lock(path):
    lock = path.with_suffix(".lock")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as error:
        raise SkillError("该任务正被其他进程处理。若进程已退出，请核对后删除对应 .lock 再恢复。", "locked") from error
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(str(os.getpid()))
        yield
    finally:
        lock.unlink(missing_ok=True)


def wait_for_result(job, task, path, key, deadline, poll_interval):
    task_id = task["task_id"]
    previous = None
    last_notice = 0
    while not task.get("result_url"):
        if time.monotonic() >= deadline:
            raise SkillError("等待超时；可使用 --resume 继续原任务。", "timeout")
        try:
            result = request_json(API + "/videos/" + urllib.parse.quote(task_id, safe=""), "007ai", key)
        except SkillError as error:
            if error.code not in {"network", "provider", "rate_limit"}:
                raise
            now = time.monotonic()
            if now - last_notice > 30:
                emit({"event": "query_delayed", "task_id": task_id, "code": error.code})
                last_notice = now
            time.sleep(min(poll_interval, max(0, deadline - now)))
            continue
        status = str(result.get("status", "")).lower()
        progress = result.get("progress")
        if status != previous or time.monotonic() - last_notice > 30:
            emit({"event": "progress", "task_id": task_id, "status": status, "progress": progress})
            last_notice, previous = time.monotonic(), status
        if status in {"failed", "cancelled", "canceled"}:
            task["status"] = "failed"
            raise SkillError("服务端任务失败；请在服务端检查原因，未自动重提。", "generation_failed")
        if status == "completed":
            task["result_url"] = find_url(result)
            if not task["result_url"]:
                raise SkillError("任务已完成但没有输出地址。", "bad_response")
            task["status"] = "downloading"
            write_json(path, job)
            break
        if status not in {"queued", "pending", "processing", "in_progress"}:
            raise SkillError("服务端返回未知任务状态。", "bad_response")
        time.sleep(min(poll_interval, max(0, deadline - time.monotonic())))


def finish_task(job, task, path, key, wait_timeout, poll_interval):
    deadline = time.monotonic() + wait_timeout
    for attempt in range(2):
        wait_for_result(job, task, path, key, deadline, poll_interval)
        try:
            content, metadata = download_gif(task["result_url"])
            break
        except SkillError as error:
            if error.code not in {"download", "invalid_gif"}:
                raise
            # Never submit again: invalidate the cached URL and query the same ID.
            task.pop("result_url", None)
            task["status"] = "waiting"
            write_json(path, job)
            if attempt:
                raise
            emit({"event": "refreshing_result", "task_id": task["task_id"]})
    suffix = "-r" + str(task["regeneration_count"]) if task.get("regeneration_count") else ""
    destination = path.parent / (path.stem + "-" + str(task["index"]) + suffix + ".gif")
    fd, temp_name = tempfile.mkstemp(prefix=".gif-download-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        os.replace(temp_name, destination)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    task.update({"status": "saved", "file": str(destination), "gif": metadata,
                 "error": None, "review_status": "pending"})
    task.pop("review_evidence", None)
    write_json(path, job)
    emit({"event": "saved", "file": str(destination), "gif": metadata})


def repair_policy(job):
    return job.get("regeneration", {"mode": "confirm", "per_item": 1, "total": 2})


def delivery_ready(job):
    return all(task["status"] == "saved" and task.get("review_status") == "passed"
               and Path(task.get("file", "")).is_file() for task in job["tasks"])


def repair_allowed(job, task):
    policy = repair_policy(job)
    return (task.get("regeneration_count", 0) < policy["per_item"]
            and sum(item.get("regeneration_count", 0) for item in job["tasks"]) < policy["total"])


def propose_repair(job, task, path, defect, evidence):
    if defect not in REPAIR_GUIDANCE or not isinstance(evidence, str) or not evidence.strip():
        raise SkillError("缺陷类型或证据无效。", "invalid_review")
    if not repair_allowed(job, task):
        task["status"] = "repair_limit"
        write_json(path, job)
        emit({"event": "repair_limit", "index": task["index"], "defect": defect})
        return
    base = task.get("base_prompt") or task.get("prompt") or job.get("prompt")
    if not isinstance(base, str) or not base.strip():
        raise SkillError("缺少原始提交提示词，无法安全重生。", "invalid_job")
    task["base_prompt"] = base
    caption = task.get("caption", job.get("caption"))
    if defect == "caption_mismatch" and (not isinstance(caption, str) or not caption.strip()):
        raise SkillError("缺少原始配字，无法自动修正。", "invalid_review")
    guidance = REPAIR_GUIDANCE[defect]
    if defect == "caption_mismatch":
        guidance += " 正确配字：" + json.dumps(caption, ensure_ascii=False)
    task["repair"] = {"defect": defect, "evidence": evidence.strip(),
                      "prompt": base + "\n\n" + guidance}
    task["status"] = "repair_pending"
    write_json(path, job)
    emit({"event": "repair_proposed", "index": task["index"], "defect": defect,
          "evidence": evidence.strip(), "prompt": task["repair"]["prompt"],
          "mode": repair_policy(job)["mode"]})


def reserve_regeneration(job, task, path):
    if task["status"] != "repair_pending" or not repair_allowed(job, task):
        raise SkillError("该项没有可用的已审核重生额度。", "repair_limit")
    if (job.get("ratio") not in RATIOS or not isinstance(job.get("images"), list)
            or not isinstance(task.get("repair", {}).get("prompt"), str)
            or not task.get("task_id")):
        raise SkillError("任务缺少原始请求参数，无法安全重生。", "invalid_job")
    previous = {key: task[key] for key in ("task_id", "file", "prompt", "gif", "error")
                if key in task}
    previous.update({"defect": task["repair"]["defect"],
                     "evidence": task["repair"]["evidence"]})
    task.setdefault("history", []).append(previous)
    task["regeneration_count"] = task.get("regeneration_count", 0) + 1
    task["prompt"] = task["repair"]["prompt"]
    for key in ("task_id", "result_url", "file", "gif", "error", "repair",
                "review_status", "review_evidence"):
        task.pop(key, None)
    task["status"] = "submission_unconfirmed"
    write_json(path, job)


def reveal_file(file):
    destination = Path(file).resolve()
    system = platform.system()
    try:
        if not destination.is_file():
            raise OSError("Saved GIF does not exist.")
        if system == "Windows":
            command = ["explorer.exe", "/select,", str(destination)]
        elif system == "Darwin":
            command = ["open", "-R", str(destination)]
        else:
            raise OSError("Automatic file reveal requires Windows or macOS.")
        subprocess.run(command, check=True, timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except (OSError, subprocess.SubprocessError) as error:
        first_error = error
    try:
        if not destination.is_file():
            raise OSError("Saved GIF does not exist.")
        if system == "Windows":
            subprocess.run(["explorer.exe", str(destination.parent)], check=True, timeout=10,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif system == "Darwin":
            subprocess.run(["open", str(destination.parent)], check=True, timeout=10,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            raise OSError("Automatic file reveal requires Windows or macOS.")
        return True
    except (OSError, subprocess.SubprocessError) as error:
        emit({"event": "file_location_warning", "file": str(destination),
              "message": "GIF 已保存，但无法自动打开其所在文件夹。",
              "detail": safe_message(error), "select_error": safe_message(first_error)})
        return False


def execute(job, path, keys, resume=False, wait_timeout=1800, poll_interval=5,
            only_index=None, submit_reserved=False, regeneration_config=None,
            continue_unsubmitted=False):
    with job_lock(path):
        if "plan_status" in job:
            # Reload under the lock so concurrent confirmations cannot erase task IDs.
            job = validate_job(read_json(path))
            if resume and job["plan_status"] == "awaiting_confirmation":
                raise SkillError("该规划尚未确认。", "plan_confirmation_required")
            if not resume and job["plan_status"] == "awaiting_confirmation":
                validate_story_groups({
                    "type": job.get("sticker_type"),
                    "story_count_override": job.get("story_count_override"),
                    "story_count_request": job.get("story_count_request"),
                }, job["tasks"])
                if regeneration_config is not None:
                    job["regeneration"] = regeneration_config
                job["plan_status"] = "confirmed"
                job["confirmed_at"] = time.time()
                write_json(path, job)
            elif not resume and job["plan_status"] == "confirmed":
                # Another invocation may have confirmed while this one loaded keys.
                # Treat that race as resume so no unsent item can be submitted.
                resume = True
            elif regeneration_config and regeneration_config["mode"] == "auto":
                raise SkillError("已确认的规划不能追认自动重生授权。", "invalid_plan_state")
        elif continue_unsubmitted:
            job = validate_job(read_json(path))
            if any(task["status"] == "submission_unconfirmed" and not task.get("task_id")
                   for task in job["tasks"]):
                raise SkillError("存在提交结果不明的项目；请先核对服务端任务和账单。",
                                 "submission_unconfirmed")
            if not any(task["status"] == "not_submitted" for task in job["tasks"]):
                raise SkillError("没有可续跑的未提交项目。", "nothing_to_continue")
        if not resume and only_index is None:
            # Resolve all references before any chargeable task is submitted.
            references = job.pop("references", None)
            if references is not None:
                job["images"] = [
                    prepare_reference(info, keys["imgbb"]) for info in references
                ]
                write_json(path, job)
            elif not isinstance(job.get("images"), list):
                raise SkillError("规划缺少参考图状态，无法安全启动或恢复。", "invalid_job")
        for task in job["tasks"]:
            if only_index is not None and task["index"] != only_index:
                continue
            if continue_unsubmitted and task["status"] != "not_submitted":
                continue
            if task["status"] == "saved" and Path(task.get("file", "")).is_file():
                continue
            if (task["status"] == "submission_unconfirmed" and not task.get("task_id")
                    and not (submit_reserved and task["index"] == only_index)):
                continue
            if (not task.get("task_id") and task["status"] != "not_submitted"
                    and not (submit_reserved and task["index"] == only_index)):
                continue
            if task["status"] in {"failed", "repair_pending", "repair_limit"}:
                continue
            if resume and (not task.get("task_id") or task["status"] == "failed"):
                continue
            while True:
                try:
                    if not task.get("task_id"):
                        task["status"] = "submission_unconfirmed"
                        write_json(path, job)
                        result = request_json(
                            API + "/videos", "007ai", keys["007ai"],
                            payload=build_payload(
                                task.get("prompt", job["prompt"]), job["ratio"], job["images"],
                                task.get("background", job.get("background")),
                            ),
                        )
                        task_id = result.get("id") or result.get("task_id")
                        if not isinstance(task_id, str) or not task_id:
                            raise SkillError("未取得任务 ID；提交结果不确定，请核对服务端，勿自动重提。", "submission_unconfirmed")
                        task.update({"task_id": task_id, "status": "submitted"})
                        write_json(path, job)
                        emit({"event": "submitted", "task_id": task_id, "job": str(path)})
                    finish_task(job, task, path, keys["007ai"], wait_timeout, poll_interval)
                    reveal_file(task["file"])
                    break
                except SkillError as error:
                    task["error"] = {"code": error.code, "message": safe_message(error)}
                    if not task.get("task_id"):
                        task["status"] = "rejected" if error.code in {
                            "invalid_key", "permission", "billing", "request_rejected", "rate_limit"
                        } else "submission_unconfirmed"
                    elif task["status"] != "failed":
                        task["status"] = "output_invalid" if error.code == "invalid_gif" else "waiting"
                    write_json(path, job)
                    emit({"event": "task_error", "index": task["index"], **task["error"]})
                    if error.code == "invalid_gif" and task.get("task_id"):
                        propose_repair(job, task, path, "invalid_gif", str(error))
                        if (task["status"] == "repair_pending"
                                and repair_policy(job)["mode"] == "auto"):
                            reserve_regeneration(job, task, path)
                            continue
                    # Stop on an uncertain submission or unavailable original task.
                    break
            if task["status"] != "saved":
                break
    ok = all(task["status"] == "saved" and Path(task.get("file", "")).is_file() for task in job["tasks"])
    emit({"ok": ok, "delivery_ready": delivery_ready(job),
          "job": str(path), "tasks": job["tasks"]})
    return 0 if ok else 2


def validate_story_groups(data, items):
    if "剧情反转" not in str(data.get("type", "")):
        return
    counts = {}
    for item in items:
        group = item.get("story_group")
        if not ((type(group) is int and group > 0)
                or (isinstance(group, str) and group.strip())):
            raise SkillError("剧情反转每项必须包含有效的故事分组。", "invalid_story_groups")
        group = str(group).strip()
        counts[group] = counts.get(group, 0) + 1
    override = data.get("story_count_override")
    if override is not None:
        request = data.get("story_count_request")
        if (type(override) is not int or not 1 <= override <= len(items)
                or not isinstance(request, str) or not request.strip()
                or len(counts) != override):
            raise SkillError("自定义故事数量须匹配分组，并记录用户明确指定的原话。",
                             "invalid_story_groups")
        return
    size = len(items)
    if size <= 5:
        valid = len(counts) == 1
    elif size <= 10:
        valid = len(counts) == 2 and min(counts.values()) >= 3
    else:
        valid = all(3 <= count <= 5 for count in counts.values())
    if not valid:
        raise SkillError("剧情分组不符合默认规则：1–5 张一个故事，6–10 张两个故事"
                         "且每组至少 3 张，11–20 张每组 3–5 张；请先重写规划。",
                         "invalid_story_groups")


def validate_plan(data):
    def invalid():
        raise SkillError("规划文件无效：需要 1–20 个包含提示词、非空配字和动作的项目。",
                         "invalid_plan")

    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        invalid()
    items = data["items"]
    if not 1 <= len(items) <= 20:
        invalid()
    for item in items:
        if not isinstance(item, dict):
            invalid()
        if (not isinstance(item.get("prompt"), str) or not item["prompt"].strip()
                or not isinstance(item.get("caption"), str) or not item["caption"].strip()
                or not isinstance(item.get("action"), str) or not item["action"].strip()):
            invalid()
        mode = item.get("background")
        description = item.get("background_description")
        if mode is not None and (
                not isinstance(mode, str) or mode not in {"transparent", "custom", "preserve"}):
            invalid()
        if description is not None and not isinstance(description, str):
            invalid()
    validate_story_groups(data, items)
    return data


def validate_job(job):
    def invalid():
        raise SkillError("任务记录损坏或字段不完整，无法恢复；请核对原始任务记录。", "invalid_job")

    if (not isinstance(job, dict) or type(job.get("schema")) is not int
            or job["schema"] != 1 or job.get("model") != MODEL
            or job.get("output_format", "gif") != "gif"):
        invalid()
    tasks = job.get("tasks")
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= 20:
        invalid()
    policy = job.get("regeneration")
    if policy is not None and (not isinstance(policy, dict)
            or policy.get("mode") not in {"confirm", "auto"}
            or type(policy.get("per_item")) is not int or not 1 <= policy["per_item"] <= 3
            or type(policy.get("total")) is not int or not 1 <= policy["total"] <= 20):
        invalid()
    if "plan_status" in job:
        if (not isinstance(job["plan_status"], str)
                or job["plan_status"] not in {"awaiting_confirmation", "confirmed"}
                or job.get("ratio") not in RATIOS
                or not isinstance(job.get("prompt"), str) or not job["prompt"].strip()
                or not isinstance(job.get("images"), list)):
            invalid()
        if "references" not in job and job["plan_status"] == "awaiting_confirmation":
            invalid()
    statuses = {
        "not_submitted", "submission_unconfirmed", "submitted", "downloading",
        "saved", "failed", "rejected", "output_invalid", "waiting",
        "repair_pending", "repair_limit",
    }
    needs_id = {"submitted", "downloading", "saved", "failed", "output_invalid",
                "waiting", "repair_pending", "repair_limit"}
    indices = set()
    for task in tasks:
        if not isinstance(task, dict):
            invalid()
        index, status = task.get("index"), task.get("status")
        if (type(index) is not int or not 1 <= index <= 20 or index in indices
                or not isinstance(status, str) or status not in statuses):
            invalid()
        indices.add(index)
        task_id = task.get("task_id")
        if "task_id" in task and (not isinstance(task_id, str) or not task_id.strip()):
            invalid()
        if status in needs_id and not task_id:
            invalid()
        # Older records can contain null after a completed response without a URL.
        if task.get("result_url") is not None:
            if not isinstance(task["result_url"], str):
                invalid()
            try:
                public_https(task["result_url"])
            except (SkillError, ValueError):
                invalid()
        if "file" in task and (
                not isinstance(task["file"], str) or not task["file"] or "\0" in task["file"]):
            invalid()
        if status == "saved" and not task.get("file"):
            invalid()
        if "prompt" in task and (not isinstance(task["prompt"], str) or not task["prompt"].strip()):
            invalid()
        if "caption" in task and (not isinstance(task["caption"], str) or not task["caption"].strip()):
            invalid()
        if "plan_status" in job and (
                not isinstance(task.get("prompt"), str) or not task["prompt"].strip()
                or not isinstance(task.get("caption"), str) or not task["caption"].strip()):
            invalid()
        count = task.get("regeneration_count", 0)
        if type(count) is not int or not 0 <= count <= 3:
            invalid()
        if "history" in task and (not isinstance(task["history"], list)
                or len(task["history"]) != count
                or any(not isinstance(entry, dict)
                       or not isinstance(entry.get("task_id"), str)
                       or not entry["task_id"].strip() for entry in task["history"])):
            invalid()
        if count and ("history" not in task or not isinstance(task.get("prompt"), str)):
            invalid()
        if status == "repair_pending":
            repair = task.get("repair")
            if (not isinstance(repair, dict)
                    or repair.get("defect") not in REPAIR_GUIDANCE
                    or not isinstance(repair.get("evidence"), str)
                    or not repair["evidence"].strip()
                    or not isinstance(repair.get("prompt"), str)
                    or not repair["prompt"].strip()):
                invalid()
    if policy and sum(task.get("regeneration_count", 0) for task in tasks) > policy["total"]:
        invalid()
    for task in tasks:
        if task.get("review_status") not in (None, "pending", "passed", "failed"):
            invalid()
        if "review_evidence" in task and (
                not isinstance(task["review_evidence"], str)
                or not task["review_evidence"].strip()):
            invalid()
    return job


def main():
    parser = argparse.ArgumentParser(description="007AI GIF-only chat stickers")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--check", action="store_true")
    actions.add_argument("--configure-stdin", action="store_true")
    actions.add_argument("--configure", action="store_true")
    actions.add_argument("--check-remote", action="store_true")
    actions.add_argument("--resume", type=Path)
    actions.add_argument("--continue-unsubmitted", type=Path)
    actions.add_argument("--plan-file", type=Path)
    actions.add_argument("--confirm-plan", type=Path)
    actions.add_argument("--report-defect", type=Path)
    actions.add_argument("--approve-regeneration", type=Path)
    actions.add_argument("--accept-review", type=Path)
    parser.add_argument("--prompt-file", type=Path)
    parser.add_argument("--image", action="append", default=[])
    parser.add_argument("--ratio", choices=RATIOS, default="1:1")
    parser.add_argument("--background", choices=["transparent", "custom", "preserve", "none"])
    parser.add_argument("--background-description")
    parser.add_argument("--caption")
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--index", type=int)
    parser.add_argument("--defect", choices=["caption_mismatch", "visual_mismatch"])
    parser.add_argument("--evidence")
    parser.add_argument("--auto-regenerate", action="store_true")
    parser.add_argument("--max-regenerations-per-item", type=int, default=1)
    parser.add_argument("--max-regenerations-total", type=int, default=2)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--wait-timeout", type=float, default=1800)
    parser.add_argument("--poll-interval", type=float, default=5)
    args = parser.parse_args()
    if args.check:
        keys = key_status()
        emit({"environment_ok": True, "python": platform.python_version(), "os": platform.system(),
              "architecture": platform.machine(), "model": MODEL, "output_format": "gif",
              "missing_keys": [name for name, value in keys.items() if not value],
              "invalid_keys": invalid_providers(keys), "remote_validated": False})
        return 0
    if args.configure_stdin:
        emit(configure(json.load(sys.stdin)))
        return 0
    if args.configure:
        current = key_status()
        invalid = invalid_providers(current)
        values = {}
        for provider, field in (("007ai", "api_key"), ("imgbb", "imgbb_api_key")):
            if not current[provider] or provider in invalid:
                values[field] = getpass.getpass(provider + " API Key: ")
        emit(configure(values) if values else {"configured": [], "message": "两个 Key 已存在。"})
        return 0
    if not (1 <= args.count <= 20) or len(args.image) > 8:
        raise SkillError("数量需为 1–20，参考图最多 8 张。")
    if not 0 < args.wait_timeout <= 7200 or not 1 <= args.poll_interval <= 30:
        raise SkillError("等待时间需在 0–7200 秒内，轮询间隔为 1–30 秒。")
    if (not 1 <= args.max_regenerations_per_item <= 3
            or not 1 <= args.max_regenerations_total <= 20):
        raise SkillError("重生上限须为每项 1–3 次、整批 1–20 次。")
    defaults = args.max_regenerations_per_item == 1 and args.max_regenerations_total == 2
    if not args.auto_regenerate and not defaults:
        raise SkillError("设置重生上限时必须明确启用 --auto-regenerate。")
    if (args.resume or args.continue_unsubmitted or args.report_defect
            or args.approve_regeneration or args.accept_review or args.plan_file) and (
            args.auto_regenerate or not defaults):
        raise SkillError("该命令不能重新设置重生授权。")
    if args.index is not None and not (args.report_defect or args.approve_regeneration
                                        or args.accept_review):
        raise SkillError("--index 仅用于视觉验收或重生操作。")
    if args.report_defect or args.approve_regeneration or args.accept_review:
        if (args.prompt_file or args.image or args.dry_run or args.background
                or args.background_description or args.caption or args.count != 1
                or args.output_dir or args.ratio != "1:1"
                or args.index is None or args.index < 1):
            raise SkillError("缺陷操作只能指定任务记录、--index 和对应缺陷参数。")
        if args.report_defect and (not args.defect or not args.evidence or not args.evidence.strip()):
            raise SkillError("请提供缺陷类型和具体视觉证据。", "invalid_review")
        if args.approve_regeneration and (args.defect or args.evidence):
            raise SkillError("批准操作不能修改已保存的缺陷或提示词。", "invalid_review")
        if args.accept_review and (
                args.defect or not args.evidence or not args.evidence.strip()):
            raise SkillError("视觉验收通过需要具体的检查依据。", "invalid_review")
        path = (args.report_defect or args.approve_regeneration or args.accept_review).expanduser().resolve()
        with job_lock(path):
            job = validate_job(read_json(path))
            if job.get("plan_status") == "awaiting_confirmation":
                raise SkillError("规划尚未确认。", "plan_confirmation_required")
            task = next((item for item in job["tasks"] if item["index"] == args.index), None)
            if task is None:
                raise SkillError("找不到对应序号。", "invalid_review")
            if args.accept_review:
                if task["status"] != "saved" or not Path(task["file"]).is_file():
                    raise SkillError("只能验收已保存的 GIF。", "invalid_review")
                task.update(review_status="passed", review_evidence=args.evidence.strip())
                write_json(path, job)
                emit({"event": "review_passed", "index": task["index"],
                      "file": task["file"], "delivery_ready": delivery_ready(job)})
                return 0
            if args.report_defect:
                if task["status"] != "saved" or not Path(task["file"]).is_file():
                    raise SkillError("仅可对已保存的 GIF 报告视觉缺陷。", "invalid_review")
                task["review_status"] = "failed"
                propose_repair(job, task, path, args.defect, args.evidence)
                if task["status"] != "repair_pending" or repair_policy(job)["mode"] != "auto":
                    return 0
            elif task["status"] != "repair_pending":
                raise SkillError("该项没有待确认的重生提案。", "invalid_review")
            keys = require_keys(("007ai",))
            reserve_regeneration(job, task, path)
        return execute(job, path, keys, wait_timeout=args.wait_timeout,
                       poll_interval=args.poll_interval, only_index=args.index,
                       submit_reserved=True)
    if args.check_remote:
        keys = require_keys(("007ai",))
        emit(remote_check(keys["007ai"]))
        return 0
    if args.resume:
        if (args.dry_run or args.prompt_file or args.image or args.background is not None
                or args.background_description is not None or args.caption is not None):
            raise SkillError("--resume 不得和生成参数或 --dry-run 一起使用。")
        path = args.resume.expanduser().resolve()
        job = validate_job(read_json(path))
        if job.get("plan_status") == "awaiting_confirmation":
            raise SkillError("该规划尚未确认；确认后使用 --confirm-plan 启动生成。",
                             "plan_confirmation_required")
        keys = require_keys(("007ai",))
        return execute(job, path, keys, True, args.wait_timeout, args.poll_interval)
    if args.continue_unsubmitted:
        if (args.dry_run or args.prompt_file or args.image or args.background is not None
                or args.background_description is not None or args.caption is not None
                or args.output_dir or args.count != 1 or args.ratio != "1:1"):
            raise SkillError("--continue-unsubmitted 不得混用生成参数。")
        path = args.continue_unsubmitted.expanduser().resolve()
        job = validate_job(read_json(path))
        if "plan_status" in job:
            raise SkillError("规划任务请使用 --confirm-plan 继续。", "invalid_plan_state")
        keys = require_keys()
        return execute(job, path, keys, wait_timeout=args.wait_timeout,
                       poll_interval=args.poll_interval, continue_unsubmitted=True)
    if args.confirm_plan:
        if (args.dry_run or args.prompt_file or args.image or args.background is not None
                or args.background_description is not None or args.caption is not None):
            raise SkillError("--confirm-plan 不得和新生成参数或 --dry-run 一起使用。")
        path = args.confirm_plan.expanduser().resolve()
        job = validate_job(read_json(path))
        if job.get("plan_status") != "awaiting_confirmation":
            raise SkillError("该规划已确认或状态无效；请使用 --resume 恢复已有任务。",
                             "invalid_plan_state")
        validate_story_groups({
            "type": job.get("sticker_type"),
            "story_count_override": job.get("story_count_override"),
            "story_count_request": job.get("story_count_request"),
        }, job["tasks"])
        keys = require_keys()
        return execute(job, path, keys, False, args.wait_timeout, args.poll_interval,
                       regeneration_config={
                           "mode": "auto" if args.auto_regenerate else "confirm",
                           "per_item": args.max_regenerations_per_item,
                           "total": args.max_regenerations_total,
                       })
    if args.plan_file:
        if (args.prompt_file or args.count != 1 or args.caption is not None
                or args.background is not None or args.background_description is not None
                or args.dry_run):
            raise SkillError("--plan-file 使用规划文件中的逐项提示词；不能混用单条生成选项。")
        data = validate_plan(read_json(args.plan_file.expanduser().resolve()))
        if not args.output_dir:
            raise SkillError("保存待确认规划时必须提供 --output-dir。")
        prepared_items = []
        for index, item in enumerate(data["items"], 1):
            if item.get("background") == "preserve" and not args.image:
                raise SkillError("保留参考图背景需要至少一张参考图。", "invalid_plan")
            prepared = prepare_prompt(
                item["prompt"].strip() + "\nGIF 动作：" + item["action"].strip(),
                item.get("background"), item.get("background_description"), item["caption"],
            )
            prepared_items.append({
                "index": index, **prepared,
                "action": item["action"].strip(),
                "scene": item.get("scene", ""),
                "story_group": item.get("story_group"),
                "story_role": item.get("story_role", ""),
            })
        output = args.output_dir.expanduser().resolve()
        output.mkdir(parents=True, exist_ok=True)
        path = output / ("plan-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:12] + ".json")
        job = {
            "schema": 1, "model": MODEL, "output_format": "gif",
            "plan_status": "awaiting_confirmation",
            "sticker_type": str(data.get("type", "")).strip(),
            "story_format": data.get("story_format", ""),
            "story_count_override": data.get("story_count_override"),
            "story_count_request": data.get("story_count_request"),
            "prompt_template_version": PROMPT_TEMPLATE_VERSION,
            "prompt": prepared_items[0]["prompt"],
            "original_prompt": prepared_items[0]["original_prompt"],
            "background_mode": "transparent", "background": "transparent",
            "ratio": args.ratio, "references": [reference_info(value) for value in args.image],
            "images": [], "created_at": time.time(),
            "tasks": [{
                **item, "status": "not_submitted",
            } for item in prepared_items],
        }
        write_json(path, job)
        emit({
            "event": "plan_saved", "plan_status": job["plan_status"], "plan": str(path),
            "type": job["sticker_type"], "story_format": job["story_format"],
            "items": [{
                "index": item["index"], "story_group": item["story_group"],
                "story_role": item["story_role"], "caption": item["caption"],
                "scene": item["scene"], "action": item["action"],
                "prompt": item["original_prompt"],
            } for item in prepared_items],
        })
        return 0
    keys = require_keys()
    if not args.prompt_file:
        raise SkillError("请提供 UTF-8 提示词文件 --prompt-file。")
    prompt = args.prompt_file.expanduser().read_text(encoding="utf-8-sig").strip()
    if not prompt:
        raise SkillError("提示词不能为空。")
    if args.background == "preserve" and not args.image:
        raise SkillError("保留参考图背景需要至少一张参考图。")
    prepared = prepare_prompt(prompt, args.background, args.background_description, args.caption)
    infos = [reference_info(value) for value in args.image]
    if args.dry_run:
        emit({"dry_run": True, **prepared, "request": build_payload(prepared["prompt"], args.ratio, [
            "<imgbb-HTTPS-reference>" if item["upload"] else item["url"] for item in infos
        ], prepared["background"]), "count": args.count,
              "uploads": sum(item["upload"] for item in infos)})
        return 0
    if not args.output_dir:
        raise SkillError("请明确提供工作区输出目录 --output-dir。")
    output = args.output_dir.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    path = output / ("job-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:12] + ".json")
    job = {"schema": 1, "model": MODEL, "output_format": "gif", **prepared,
           "ratio": args.ratio, "references": infos, "created_at": time.time(),
           "regeneration": {
               "mode": "auto" if args.auto_regenerate else "confirm",
               "per_item": args.max_regenerations_per_item,
               "total": args.max_regenerations_total,
           },
           "tasks": [{"index": i + 1, "status": "not_submitted"} for i in range(args.count)]}
    write_json(path, job)
    emit({"event": "job_created", "job": str(path)})
    return execute(job, path, keys, False, args.wait_timeout, args.poll_interval)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (SkillError, OSError, ValueError, EOFError) as error:
        emit({"ok": False, "code": getattr(error, "code", "local_error"), "error": safe_message(error)})
        raise SystemExit(1)
    except KeyboardInterrupt:
        emit({"ok": False, "error": "操作中断。已有任务请通过 --resume 恢复，勿重新生成。"})
        raise SystemExit(130)
