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


def require_keys():
    keys = key_status()
    missing = [name for name, key in keys.items() if not key]
    invalid = invalid_providers(keys)
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
    if status == 402 or any(word in message for word in ("insufficient", "balance", "余额", "quota")):
        raise SkillError(provider + " 余额或配额不足，请检查账户；无需因此更换 Key。", "billing")
    if status == 403:
        raise SkillError(provider + " 拒绝访问，请检查模型/账户权限。", "permission")
    if status == 429:
        raise SkillError(provider + " 请求受限，请稍后恢复原任务。", "rate_limit")
    if status >= 500:
        raise SkillError(provider + " 服务暂时异常，请稍后恢复原任务。", "provider")
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
        with urllib.request.urlopen(request, context=tls_context(), timeout=90) as response:
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


def build_payload(prompt, ratio, images, background=None):
    payload = {"model": MODEL, "prompt": prompt, "aspect_ratio": ratio,
               "image_size": "1K", "output_format": "gif"}
    if background:
        payload["background"] = background
    if images:
        payload["images"] = images
    return payload


def find_url(data):
    if isinstance(data, dict):
        for key in ("url", "video_url", "image_url"):
            if isinstance(data.get(key), str):
                return public_https(data[key])
        for value in data.values():
            found = find_url(value)
            if found:
                return found
    elif isinstance(data, list):
        for value in data:
            found = find_url(value)
            if found:
                return found
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
                hashes.add(hashlib.sha256(image.convert("RGBA").tobytes()).digest())
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
    request = urllib.request.Request(public_https(url), headers={"User-Agent": "Mozilla/5.0", "Accept": "image/gif,image/*"})
    try:
        with urllib.request.urlopen(request, context=tls_context(), timeout=120) as response:
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


def finish_task(job, task, path, key, wait_timeout, poll_interval):
    task_id = task["task_id"]
    deadline = time.monotonic() + wait_timeout
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
    content, metadata = download_gif(task["result_url"])
    destination = path.parent / (path.stem + "-" + str(task["index"]) + ".gif")
    fd, temp_name = tempfile.mkstemp(prefix=".gif-download-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        os.replace(temp_name, destination)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    task.update({"status": "saved", "file": str(destination), "gif": metadata, "error": None})
    write_json(path, job)
    emit({"event": "saved", "file": str(destination), "gif": metadata})


def reveal_file(file):
    destination = Path(file).resolve()
    try:
        if not destination.is_file():
            raise OSError("Saved GIF does not exist.")
        system = platform.system()
        if system == "Windows":
            command = ["explorer.exe", "/select,", str(destination)]
        elif system == "Darwin":
            command = ["open", "-R", str(destination)]
        else:
            raise OSError("Automatic file reveal requires Windows or macOS.")
        subprocess.run(command, check=True, timeout=10)
        emit({"event": "file_location_opened", "file": str(destination)})
    except (OSError, subprocess.SubprocessError) as error:
        emit({"event": "file_location_warning", "file": str(destination),
              "message": "无法自动打开文件位置，请通过文件链接打开。", "detail": safe_message(error)})


def execute(job, path, keys, resume=False, wait_timeout=1800, poll_interval=5):
    saved_files = []
    with job_lock(path):
        if not resume:
            # Resolve all references before any chargeable task is submitted.
            images = [prepare_reference(info, keys["imgbb"]) for info in job.pop("references")]
            job["images"] = images
            write_json(path, job)
        for task in job["tasks"]:
            if task["status"] == "saved" and Path(task.get("file", "")).is_file():
                continue
            if resume and (not task.get("task_id") or task["status"] == "failed"):
                continue
            try:
                if not task.get("task_id"):
                    task["status"] = "submission_unconfirmed"
                    write_json(path, job)
                    result = request_json(
                        API + "/videos", "007ai", keys["007ai"],
                        payload=build_payload(job["prompt"], job["ratio"], job["images"], job.get("background")),
                    )
                    task_id = result.get("id") or result.get("task_id")
                    if not isinstance(task_id, str) or not task_id:
                        raise SkillError("未取得任务 ID；提交结果不确定，请核对服务端，勿自动重提。", "submission_unconfirmed")
                    task.update({"task_id": task_id, "status": "submitted"})
                    write_json(path, job)
                    emit({"event": "submitted", "task_id": task_id, "job": str(path)})
                finish_task(job, task, path, keys["007ai"], wait_timeout, poll_interval)
                saved_files.append(task["file"])
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
                # Do not spend more money after any failed/unconfirmed item.
                break
    if saved_files:
        reveal_file(saved_files[0])
    ok = all(task["status"] == "saved" and Path(task.get("file", "")).is_file() for task in job["tasks"])
    emit({"ok": ok, "job": str(path), "tasks": job["tasks"]})
    return 0 if ok else 2


def main():
    parser = argparse.ArgumentParser(description="007AI GIF-only chat stickers")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--check", action="store_true")
    actions.add_argument("--configure-stdin", action="store_true")
    actions.add_argument("--configure", action="store_true")
    actions.add_argument("--check-remote", action="store_true")
    actions.add_argument("--resume", type=Path)
    parser.add_argument("--prompt-file", type=Path)
    parser.add_argument("--image", action="append", default=[])
    parser.add_argument("--ratio", choices=RATIOS, default="1:1")
    parser.add_argument("--background", choices=["transparent", "none"])
    parser.add_argument("--count", type=int, default=1)
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
    keys = require_keys()
    if args.check_remote:
        emit(remote_check(keys["007ai"]))
        return 0
    if args.resume:
        if args.dry_run or args.prompt_file or args.image:
            raise SkillError("--resume 不得和生成参数或 --dry-run 一起使用。")
        path = args.resume.expanduser().resolve()
        job = read_json(path)
        if not isinstance(job, dict) or job.get("schema") != 1 or job.get("model") != MODEL:
            raise SkillError("无法读取该 Skill 的任务记录。")
        return execute(job, path, keys, True, args.wait_timeout, args.poll_interval)
    if not args.prompt_file:
        raise SkillError("请提供 UTF-8 提示词文件 --prompt-file。")
    prompt = args.prompt_file.expanduser().read_text(encoding="utf-8-sig").strip()
    if not prompt:
        raise SkillError("提示词不能为空。")
    infos = [reference_info(value) for value in args.image]
    if args.dry_run:
        emit({"dry_run": True, "request": build_payload(prompt, args.ratio, [
            "<imgbb-HTTPS-reference>" if item["upload"] else item["url"] for item in infos
        ], args.background), "count": args.count, "uploads": sum(item["upload"] for item in infos)})
        return 0
    if not args.output_dir:
        raise SkillError("请明确提供工作区输出目录 --output-dir。")
    output = args.output_dir.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    path = output / ("job-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:12] + ".json")
    job = {"schema": 1, "model": MODEL, "output_format": "gif", "prompt": prompt,
           "ratio": args.ratio, "background": args.background, "references": infos, "created_at": time.time(),
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
