import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from PIL import Image

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


app = load("stickers")
pack = load("package_skill")


def gif(loop=0, same=False):
    frames = [Image.new("RGBA", (16, 16), color) for color in ("red", "red" if same else "blue")]
    output = io.BytesIO()
    options = {} if loop is None else {"loop": loop}
    frames[0].save(output, "GIF", save_all=True, append_images=frames[1:],
                   duration=[100, 200], **options)
    return output.getvalue()


class StickerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gif-skill-test-")
        self.root = Path(self.temp.name)
        self.patch = patch.object(app, "ROOT", self.root)
        self.patch.start()
        app.SECRETS.clear()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def keys(self):
        app.configure({"api_key": "fake-provider-key", "imgbb_api_key": "fake-host-key"})
        return app.require_keys()

    def test_reveal_file_platforms_and_failure(self):
        destination = self.root / "sticker with spaces.gif"
        destination.write_bytes(gif())
        for system, command in (
            ("Windows", ["explorer.exe", "/select,", str(destination.resolve())]),
            ("Darwin", ["open", "-R", str(destination.resolve())]),
        ):
            with patch.object(app.platform, "system", return_value=system), \
                    patch.object(app.subprocess, "run") as launch, patch.object(app, "emit"):
                app.reveal_file(destination)
                launch.assert_called_once_with(command, check=True, timeout=10)
        with patch.object(app.platform, "system", return_value="Windows"), \
                patch.object(app.subprocess, "run", side_effect=OSError("unavailable")), \
                patch.object(app, "emit") as emit:
            app.reveal_file(destination)
            self.assertEqual(emit.call_args.args[0]["event"], "file_location_warning")
        self.assertTrue(destination.is_file())

    def job(self, status="not_submitted", task_id=None):
        task = {"index": 1, "status": status}
        if task_id:
            task["task_id"] = task_id
        job = {"schema": 1, "model": app.MODEL, "output_format": "gif",
               "prompt": "test", "ratio": "1:1", "references": [], "images": [],
               "tasks": [task]}
        path = self.root / "job-test.json"
        app.write_json(path, job)
        return job, path

    def test_individual_key_updates_and_invalid_marker(self):
        keys = self.keys()
        app.invalidate("007ai", keys["007ai"])
        self.assertEqual(app.invalid_providers(keys), ["007ai"])
        app.configure({"api_key": "replacement"})
        self.assertEqual(app.require_keys()["imgbb"], "fake-host-key")
        self.assertEqual(app.invalid_providers(app.key_status()), [])
        self.assertEqual(json.loads((self.root / "007ai-image-key.json").read_text()),
                         {"api_key": "replacement"})

    def test_corrupt_config_is_missing(self):
        (self.root / "007ai-image-key.json").write_text("broken")
        self.assertEqual(app.key_status()["007ai"], "")
        with self.assertRaises(app.SkillError):
            app.require_keys()

    def test_missing_keys_have_no_environment_or_builtin_fallback(self):
        with patch.dict(app.os.environ, {
            "OPENAI_API_KEY": "fake-environment-key",
            "007AI_API_KEY": "fake-environment-key",
            "IMGBB_API_KEY": "fake-environment-key",
        }), patch.object(app, "request_json") as request:
            self.assertEqual(app.key_status(), {"007ai": "", "imgbb": ""})
            with self.assertRaises(app.SkillError) as caught:
                app.require_keys()
            self.assertEqual(caught.exception.code, "credentials_required")
            request.assert_not_called()

    def test_blank_configuration_does_not_partially_replace_keys(self):
        self.keys()
        with self.assertRaises(app.SkillError):
            app.configure({"api_key": "replacement", "imgbb_api_key": ""})
        self.assertEqual(app.key_status()["007ai"], "fake-provider-key")

    def test_gif_contract_is_fixed(self):
        data = app.build_payload("hello", "1:1", ["https://example.com/a.png"])
        self.assertEqual(data["output_format"], "gif")
        self.assertEqual(data["image_size"], "1K")
        self.assertEqual(data["model"], "gptimage2.6_gr")
        self.assertNotIn("metadata", data)
        self.assertNotIn("images", app.build_payload("hello", "1:1", []))

    def test_static_and_fake_gif_rejected(self):
        for content in (b"\x89PNG\r\n", gif(same=True)):
            with self.assertRaises(app.SkillError):
                app.inspect_gif(content)

    def test_gif_loop_repair_preserves_pixels_and_timing(self):
        for loop in (None, 3, 0):
            original = gif(loop)
            result, info = app.normalize_gif(original)
            self.assertEqual(info["loop"], 0)
            self.assertEqual(info["frames"], 2)
            self.assertEqual(info["duration_ms"], 300)
            before = Image.open(io.BytesIO(original))
            after = Image.open(io.BytesIO(result))
            for index in range(2):
                before.seek(index)
                after.seek(index)
                self.assertEqual(before.convert("RGBA").tobytes(), after.convert("RGBA").tobytes())

    def test_key_failures_do_not_confuse_permissions_or_billing(self):
        keys = self.keys()
        for status, code in ((403, "permission"), (402, "billing"), (429, "rate_limit"), (503, "provider")):
            with self.assertRaises(app.SkillError) as caught:
                app.provider_error("007ai", keys["007ai"], status, {})
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(app.invalid_providers(keys), [])
        with self.assertRaises(app.SkillError):
            app.provider_error("007ai", keys["007ai"], 401, {})
        self.assertEqual(app.invalid_providers(keys), ["007ai"])

    def test_https_reference_validation(self):
        for value in ("http://example.com/a", "data:image/png;base64,A", "https://127.0.0.1/a",
                      "https://localhost/a", "https://user:password@example.com/a"):
            with self.assertRaises(app.SkillError):
                app.public_https(value)
        self.assertEqual(app.public_https("https://example.com/a"), "https://example.com/a")

    def test_reference_unicode_and_upload_cache(self):
        source = self.root / "\u5c0f\u80e1 \u53c2\u8003.png"
        Image.new("RGB", (16, 16), "red").save(source)
        info = app.reference_info(str(source))
        response = {"success": True, "data": {"url": "https://example.com/a.png"}}
        with patch.object(app, "request_json", return_value=response) as request:
            first = app.prepare_reference(info, "fake-host-key")
            second = app.prepare_reference(info, "fake-host-key")
        self.assertEqual(first, second)
        self.assertEqual(request.call_count, 1)

    def test_uncertain_submission_is_never_resubmitted(self):
        keys = self.keys()
        job, path = self.job()
        with patch.object(app, "request_json", side_effect=app.SkillError("timeout", "network")) as request:
            with patch.object(app, "emit"):
                self.assertEqual(app.execute(job, path, keys), 2)
                self.assertEqual(app.execute(job, path, keys, resume=True), 2)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(job["tasks"][0]["status"], "submission_unconfirmed")

    def test_resume_existing_task_only_gets_and_saves_validated_gif(self):
        keys = self.keys()
        job, path = self.job("submitted", "task_test")
        result = {"status": "completed", "url": "https://example.com/output"}
        with patch.object(app, "request_json", return_value=result) as request:
            with patch.object(app, "download_gif", return_value=app.normalize_gif(gif())):
                with patch.object(app, "emit"):
                    self.assertEqual(app.execute(job, path, keys, resume=True), 0)
        self.assertEqual(request.call_count, 1)
        self.assertNotIn("payload", request.call_args.kwargs)
        self.assertEqual(job["tasks"][0]["status"], "saved")
        self.assertTrue(Path(job["tasks"][0]["file"]).is_file())
        self.assertNotIn("fake-provider-key", path.read_text())

    def test_archive_excludes_credentials_and_runtime(self):
        with tempfile.TemporaryDirectory() as source_dir:
            source = Path(source_dir) / "007ai-gif-stickers"
            source.mkdir()
            for relative in pack.FILES:
                target = source / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("safe", encoding="utf-8")
            (source / "007ai-image-key.json").write_text('{"api_key":"secret"}')
            (source / "imgbb-api-key.json").write_text('{"api_key":"host-secret"}')
            (source / ".runtime").mkdir()
            (source / ".runtime/cache.json").write_text("private")
            with patch.object(pack, "ROOT", source):
                archive_path = pack.package(self.root / "skill.zip")
            with zipfile.ZipFile(archive_path) as archive:
                self.assertEqual(len(archive.namelist()), len(pack.FILES))
                self.assertFalse(any("key.json" in name or ".runtime" in name for name in archive.namelist()))
                self.assertTrue(all(b"secret" not in archive.read(name) for name in archive.namelist()))


if __name__ == "__main__":
    unittest.main()
