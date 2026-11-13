"""Offline regressions; no credentials, uploads, or paid requests."""
import copy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("stickers", ROOT / "scripts/stickers.py")
stickers = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(stickers)


class StickerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for target, options in (
            ("ROOT", {"new": self.root}),
            ("emit", {}),
            ("reveal_file", {}),
        ):
            self.start_patch(patch.object(stickers, target, **options))
        self.start_patch(patch.object(
            stickers.urllib.request, "urlopen",
            side_effect=AssertionError("Unexpected real network request"),
        ))
        self.job = {
            "schema": 1, "model": stickers.MODEL, "output_format": "gif",
            "tasks": [{"index": 1, "task_id": "original-id", "status": "waiting"}],
        }
        self.path = self.root / "job-test.json"
        self.keys = {"007ai": "fake-api", "imgbb": "fake-imgbb"}
        stream = io.BytesIO()
        Image.new("RGB", (16, 16), "red").save(
            stream, format="GIF", save_all=True,
            append_images=[Image.new("RGB", (16, 16), "blue")],
            duration=100, loop=0,
        )
        self.gif = stream.getvalue()
        self.download = (self.gif, stickers.inspect_gif(self.gif))

    def start_patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def save_keys(self, imgbb=None):
        stickers.write_json(self.root / stickers.KEY_FILES["007ai"], {"api_key": "fake-api"})
        if imgbb:
            stickers.write_json(self.root / stickers.KEY_FILES["imgbb"], {"api_key": imgbb})

    def resume(self):
        return stickers.execute(self.job, self.path, self.keys, resume=True)

    def assert_query_only(self, request):
        for call in request.call_args_list:
            self.assertEqual(call.args[0], stickers.API + "/videos/original-id")
            self.assertNotIn("payload", call.kwargs)
            self.assertNotIn("form", call.kwargs)

    def test_expired_link_refreshes_existing_task_and_saves(self):
        old, new = "https://example.com/expired.gif", "https://example.com/fresh.gif"
        self.job["tasks"][0]["result_url"] = old
        with patch.object(stickers, "request_json", return_value={
            "status": "completed", "output": {"url": new},
        }) as request, patch.object(stickers, "download_gif", side_effect=[
            stickers.SkillError("expired", "download"), self.download,
        ]) as download:
            self.assertEqual(self.resume(), 0)
        self.assertEqual([call.args[0] for call in download.call_args_list], [old, new])
        self.assertEqual(request.call_count, 1)
        self.assert_query_only(request)
        task = stickers.read_json(self.path)["tasks"][0]
        self.assertEqual(task["task_id"], "original-id")
        self.assertEqual(task["status"], "saved")
        self.assertEqual(Path(task["file"]).read_bytes(), self.gif)

    def test_download_retries_are_bounded_and_stale_link_is_removed(self):
        self.job["tasks"][0]["result_url"] = "https://example.com/expired.gif"
        with patch.object(stickers, "request_json", return_value={
            "status": "completed", "url": "https://example.com/still-expired.gif",
        }) as request, patch.object(stickers, "download_gif", side_effect=
                                   stickers.SkillError("expired", "download")) as download:
            self.assertEqual(self.resume(), 2)
        self.assertEqual(download.call_count, 2)
        self.assertEqual(request.call_count, 1)
        self.assert_query_only(request)
        task = stickers.read_json(self.path)["tasks"][0]
        self.assertNotIn("result_url", task)
        self.assertEqual(task["task_id"], "original-id")
        self.assertEqual(task["status"], "waiting")

    def test_invalid_gif_refreshes_existing_task(self):
        self.job["tasks"][0]["result_url"] = "https://example.com/wrong.png"
        with patch.object(stickers, "request_json", return_value={
            "status": "completed", "url": "https://example.com/correct.gif",
        }) as request, patch.object(stickers, "download_gif", side_effect=[
            stickers.SkillError("not GIF", "invalid_gif"), self.download,
        ]):
            self.assertEqual(self.resume(), 0)
        self.assert_query_only(request)

    def test_valid_cached_link_does_not_query_again(self):
        self.job["tasks"][0]["result_url"] = "https://example.com/valid.gif"
        with patch.object(stickers, "request_json") as request, patch.object(
                stickers, "download_gif", return_value=self.download):
            self.assertEqual(self.resume(), 0)
        request.assert_not_called()

    def test_refresh_query_failure_keeps_task_for_later_resume(self):
        self.job["tasks"][0]["result_url"] = "https://example.com/expired.gif"
        with patch.object(stickers, "request_json", side_effect=
                          stickers.SkillError("denied", "permission")) as request, patch.object(
                stickers, "download_gif", side_effect=stickers.SkillError("expired", "download")):
            self.assertEqual(self.resume(), 2)
        self.assert_query_only(request)
        task = stickers.read_json(self.path)["tasks"][0]
        self.assertEqual(task["task_id"], "original-id")
        self.assertEqual(task["error"]["code"], "permission")
        self.assertNotIn("result_url", task)

    def test_result_url_ignores_inputs_references_and_metadata(self):
        result = {
            "input": {"images": [{"url": "https://example.com/input.png"}]},
            "references": [{"url": "https://example.com/reference.png"}],
            "metadata": {"url": "https://example.com/metadata.json"},
            "output": {"url": "https://example.com/output.gif"},
        }
        self.assertEqual(stickers.find_url(result), "https://example.com/output.gif")
        del result["output"]
        self.assertIsNone(stickers.find_url(result))

    def test_supported_result_shapes(self):
        url = "https://example.com/result.gif"
        for value in (
            {"url": url}, {"video_url": url}, {"image_url": url},
            {"output": url}, {"outputs": [{"url": url}]},
            {"result": {"video_url": url}}, {"results": [{"image_url": url}]},
            {"data": {"output": {"url": url}}},
        ):
            with self.subTest(value=value):
                self.assertEqual(stickers.find_url(value), url)

    def test_result_url_still_requires_public_https(self):
        with self.assertRaises(stickers.SkillError):
            stickers.find_url({"output": {"url": "http://localhost/result.gif"}})

    def test_http_error_status_precedes_quota_text(self):
        for status, expected in (
            (402, "billing"), (403, "permission"), (429, "rate_limit"),
            (500, "provider"), (400, "billing"),
        ):
            with self.subTest(status=status):
                with self.assertRaises(stickers.SkillError) as error:
                    stickers.provider_error("007ai", "fake-api", status, {
                        "error": {"message": "Quota exceeded: requests per minute"},
                    })
                self.assertEqual(error.exception.code, expected)
        self.assertFalse((self.root / ".runtime/invalid-keys.json").exists())

    def test_rate_limit_is_retried_during_polling(self):
        def response(*args, **kwargs):
            if request.call_count == 1:
                stickers.provider_error("007ai", "fake-api", 429, {
                    "error": {"message": "Quota exceeded: requests per minute"},
                })
            return {"status": "completed", "url": "https://example.com/result.gif"}

        with patch.object(stickers, "request_json", side_effect=response) as request, patch.object(
                stickers.time, "sleep") as sleep, patch.object(
                stickers, "download_gif", return_value=self.download):
            self.assertEqual(self.resume(), 0)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(sleep.call_count, 1)
        self.assert_query_only(request)

    def test_resume_needs_no_imgbb_key_even_if_marked_invalid(self):
        for imgbb in (None, "invalid-imgbb"):
            with self.subTest(imgbb=imgbb):
                self.save_keys(imgbb)
                if imgbb:
                    stickers.invalidate("imgbb", imgbb)
                stickers.write_json(self.path, self.job)
                with patch.object(sys, "argv", ["stickers.py", "--resume", str(self.path)]), patch.object(
                        stickers, "request_json", return_value={
                            "status": "completed", "url": "https://example.com/result.gif",
                        }) as request, patch.object(
                        stickers, "download_gif", return_value=self.download):
                    self.assertEqual(stickers.main(), 0)
                self.assert_query_only(request)

    def test_generation_still_requires_both_keys(self):
        self.save_keys()
        with self.assertRaises(stickers.SkillError) as error:
            stickers.require_keys()
        self.assertEqual(error.exception.code, "credentials_required")
        self.assertIn("imgbb", str(error.exception))

    def test_resume_still_requires_valid_007ai_key(self):
        self.save_keys()
        stickers.invalidate("007ai", "fake-api")
        stickers.write_json(self.path, self.job)
        with patch.object(sys, "argv", ["stickers.py", "--resume", str(self.path)]):
            with self.assertRaises(stickers.SkillError) as error:
                stickers.main()
        self.assertEqual(error.exception.code, "credentials_required")

    def test_remote_check_needs_only_007ai_key(self):
        self.save_keys()
        with patch.object(sys, "argv", ["stickers.py", "--check-remote"]), patch.object(
                stickers, "request_json", return_value={"data": [{"id": stickers.MODEL}]}):
            self.assertEqual(stickers.main(), 0)

    def test_invalid_job_shapes_are_rejected(self):
        cases = [
            None, [], {}, {"schema": 1, "model": stickers.MODEL},
            {**self.job, "tasks": []},
            {**self.job, "tasks": {}},
            {**self.job, "tasks": [None]},
            {**self.job, "tasks": [{"index": 1, "status": "waiting"}]},
            {**self.job, "tasks": [self.job["tasks"][0]] * 2},
            {**self.job, "tasks": [self.job["tasks"][0]] * 21},
            {**self.job, "schema": True},
            {**self.job, "output_format": "png"},
        ]
        for field, values in (
            ("index", (None, True, 0, -1, "../escape", 21)),
            ("status", (None, [], "unknown")),
            ("task_id", (None, "", 123, [], "   ")),
            ("result_url", ([], 123, "", "http://localhost/a.gif", "https://[")),
            ("file", (None, [], "", "bad\0path")),
        ):
            for value in values:
                task = {**self.job["tasks"][0], field: value}
                cases.append({**self.job, "tasks": [task]})
        cases.append({**self.job, "tasks": [{
            **self.job["tasks"][0], "status": "saved",
        }]})
        for job in cases:
            with self.subTest(job=job), self.assertRaises(stickers.SkillError) as error:
                stickers.validate_job(job)
            self.assertEqual(error.exception.code, "invalid_job")

    def test_valid_job_states_and_original_data_are_preserved(self):
        for status in ("not_submitted", "submission_unconfirmed", "rejected"):
            job = {**self.job, "tasks": [{"index": 1, "status": status}]}
            self.assertIs(stickers.validate_job(job), job)
        for status in ("submitted", "downloading", "failed", "output_invalid", "waiting", "saved"):
            task = {**self.job["tasks"][0], "status": status}
            if status == "saved":
                task["file"] = str(self.root / "missing.gif")
            job = {**self.job, "tasks": [task]}
            before = copy.deepcopy(job)
            self.assertIs(stickers.validate_job(job), job)
            self.assertEqual(job, before)

    def test_legacy_null_result_url_can_be_resumed(self):
        self.job["tasks"][0]["result_url"] = None
        stickers.validate_job(self.job)
        with patch.object(stickers, "request_json", return_value={
            "status": "completed", "url": "https://example.com/result.gif",
        }) as request, patch.object(stickers, "download_gif", return_value=self.download):
            self.assertEqual(self.resume(), 0)
        self.assert_query_only(request)

    def test_invalid_job_cli_returns_json_instead_of_traceback(self):
        self.path.write_text(json.dumps({"schema": 1, "model": stickers.MODEL}), encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-B", str(ROOT / "scripts/stickers.py"), "--resume", str(self.path)],
            capture_output=True, encoding="utf-8", check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["code"], "invalid_job")
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(self.path.with_suffix(".lock").exists())

    def test_resume_does_not_submit_unconfirmed_or_unsubmitted_items(self):
        self.job["tasks"] = [
            {"index": 1, "status": "submission_unconfirmed"},
            {"index": 2, "status": "not_submitted"},
        ]
        with patch.object(stickers, "request_json") as request:
            self.assertEqual(self.resume(), 2)
        request.assert_not_called()

    def generate_with_options(self, options, dry_run=True):
        self.save_keys("fake-imgbb")
        prompt = self.root / "prompt.txt"
        prompt.write_text("红色衣服，白色配字，挥手打招呼。", encoding="utf-8")
        argv = [
            "stickers.py", "--prompt-file", str(prompt),
            "--output-dir", str(self.root / "output"), *options,
        ]
        if dry_run:
            argv.append("--dry-run")
        with patch.object(sys, "argv", argv):
            return stickers.main()

    def test_default_prompt_requires_gif_and_transparency(self):
        prepared = stickers.prepare_prompt("红色衣服，白色配字，挥手。")
        payload = stickers.build_payload(prepared["prompt"], "1:1", [], prepared["background"])
        self.assertEqual(prepared["background_mode"], "transparent")
        self.assertEqual(payload["background"], "transparent")
        self.assertEqual(payload["output_format"], "gif")
        self.assertIn(stickers.GIF_REQUIREMENTS, payload["prompt"])
        self.assertIn(stickers.TRANSPARENT_REQUIREMENTS, payload["prompt"])
        self.assertEqual(prepared["original_prompt"], "红色衣服，白色配字，挥手。")

    def test_custom_and_preserved_backgrounds_do_not_receive_transparency(self):
        for mode, description in (
            ("custom", "白色背景"), ("custom", "公园背景"),
            ("preserve", None), ("preserve", "保留参考图的公园"),
            (None, "粉色背景"), ("none", None),
        ):
            with self.subTest(mode=mode, description=description):
                prepared = stickers.prepare_prompt("挥手", mode, description)
                payload = stickers.build_payload(
                    prepared["prompt"], "1:1", ["https://example.com/ref.png"], prepared["background"],
                )
                self.assertNotIn("background", payload)
                self.assertIn(stickers.GIF_REQUIREMENTS, payload["prompt"])
                self.assertNotIn(stickers.TRANSPARENT_REQUIREMENTS, payload["prompt"])
                if description:
                    self.assertIn(description, payload["prompt"])

    def test_conflicting_or_incomplete_background_options_are_rejected(self):
        for mode, description in (("transparent", "白色背景"), ("custom", None), ("custom", " ")):
            with self.subTest(mode=mode), self.assertRaises(stickers.SkillError):
                stickers.prepare_prompt("挥手", mode, description)
        with self.assertRaises(stickers.SkillError):
            self.generate_with_options(["--background", "preserve"])

    def test_caption_is_kept_verbatim_and_separate_from_requirements(self):
        caption = ' 嗨！"你好"\nGIF '
        prepared = stickers.prepare_prompt("开心挥手", caption=caption)
        self.assertEqual(prepared["caption"], caption)
        self.assertIn(json.dumps(caption, ensure_ascii=False), prepared["prompt"])
        self.assertEqual(prepared["prompt"].count(stickers.GIF_REQUIREMENTS), 1)
        self.assertEqual(prepared["prompt"].count(stickers.TRANSPARENT_REQUIREMENTS), 1)

    def test_dry_run_and_submission_use_identical_prepared_payload(self):
        for options in (["--caption", "嗨！"], [
            "--background", "custom", "--background-description", "白色背景", "--caption", "你好",
        ]):
            with self.subTest(options=options):
                with patch.object(stickers, "request_json") as request:
                    self.assertEqual(self.generate_with_options(options), 0)
                    request.assert_not_called()
                preview = stickers.emit.call_args.args[0]
                with patch.object(stickers, "request_json", return_value={"id": "new-id"}) as request, patch.object(
                        stickers, "finish_task", side_effect=stickers.SkillError("wait", "timeout")):
                    self.assertEqual(self.generate_with_options(options, dry_run=False), 2)
                payload = request.call_args.kwargs["payload"]
                self.assertEqual(payload, preview["request"])
                record = stickers.read_json(Path(stickers.emit.call_args.args[0]["job"]))
                for field in (
                    "original_prompt", "prompt", "prompt_template_version",
                    "background_mode", "background_description", "caption", "background",
                ):
                    self.assertEqual(record[field], preview[field])
                self.assertEqual(record["prompt"].count(stickers.GIF_REQUIREMENTS), 1)
                self.assertEqual((self.root / "prompt.txt").read_text(encoding="utf-8"),
                                 record["original_prompt"])
                self.assertEqual(request.call_count, 1)

    def transparent_gif(self, second_frame_opaque=False):
        frames = []
        for index, color in enumerate(("red", "blue")):
            frame = Image.new("RGBA", (16, 16), (0, 0, 0, 0))
            if second_frame_opaque and index:
                frame = Image.new("RGBA", (16, 16), "white")
            for x in range(4, 12):
                for y in range(4, 12):
                    frame.putpixel((x, y), (255, 0, 0, 255) if color == "red" else (0, 0, 255, 255))
            frames.append(frame)
        output = io.BytesIO()
        frames[0].save(output, format="GIF", save_all=True, append_images=frames[1:],
                       duration=100, loop=0, disposal=2, optimize=False)
        return output.getvalue()

    def test_transparency_checks_every_composited_frame(self):
        content = self.transparent_gif()
        metadata = stickers.inspect_gif(content, require_transparent=True)
        self.assertEqual(len(metadata["transparent_pixels"]), 2)
        self.assertTrue(metadata["all_frames_have_transparency"])
        self.assertTrue(all(0 < value < 256 for value in metadata["transparent_pixels"]))
        for content in (self.gif, self.transparent_gif(second_frame_opaque=True)):
            with self.subTest(content_size=len(content)), self.assertRaises(stickers.SkillError) as error:
                stickers.inspect_gif(content, require_transparent=True)
            self.assertEqual(error.exception.code, "background_not_transparent")

    def test_opaque_output_in_transparent_mode_is_not_saved_or_regenerated(self):
        self.job["background"] = "transparent"
        self.job["tasks"][0]["result_url"] = "https://example.com/opaque.gif"
        with patch.object(stickers, "request_json") as request, patch.object(
                stickers, "download_gif", return_value=self.download) as download:
            self.assertEqual(self.resume(), 2)
        request.assert_not_called()
        self.assertEqual(download.call_count, 1)
        self.assertFalse(list(self.root.glob("*.gif")))
        task = stickers.read_json(self.path)["tasks"][0]
        self.assertEqual(task["status"], "output_invalid")
        self.assertEqual(task["error"]["code"], "background_not_transparent")
        self.assertEqual(task["task_id"], "original-id")

    def test_valid_transparent_output_is_saved(self):
        self.job["background"] = "transparent"
        self.job["tasks"][0]["result_url"] = "https://example.com/transparent.gif"
        content = self.transparent_gif()
        with patch.object(stickers, "download_gif", return_value=(
                content, stickers.inspect_gif(content))):
            self.assertEqual(self.resume(), 0)
        task = stickers.read_json(self.path)["tasks"][0]
        self.assertTrue(task["gif"]["all_frames_have_transparency"])
        self.assertEqual(Path(task["file"]).read_bytes(), content)

    def test_non_gif_and_static_images_are_rejected(self):
        for format_name in ("PNG", "GIF"):
            output = io.BytesIO()
            Image.new("RGB", (16, 16), "white").save(output, format=format_name)
            with self.subTest(format=format_name), self.assertRaises(stickers.SkillError) as error:
                stickers.inspect_gif(output.getvalue())
            self.assertEqual(error.exception.code, "invalid_gif")

    def test_resume_never_rebuilds_prompt_or_applies_new_defaults(self):
        self.save_keys()
        self.job.update({"prompt": "legacy prompt", "background": None})
        self.job["tasks"][0]["result_url"] = "https://example.com/original.gif"
        stickers.write_json(self.path, self.job)
        with patch.object(sys, "argv", ["stickers.py", "--resume", str(self.path)]), patch.object(
                stickers, "prepare_prompt", side_effect=AssertionError("Must not rebuild")), patch.object(
                stickers, "request_json") as request, patch.object(
                stickers, "download_gif", return_value=self.download):
            self.assertEqual(stickers.main(), 0)
        request.assert_not_called()
        record = stickers.read_json(self.path)
        self.assertEqual(record["prompt"], "legacy prompt")
        self.assertIsNone(record["background"])
        self.assertNotIn("prompt_template_version", record)

    def test_resume_rejects_new_prompt_options(self):
        for options in (
            ["--caption", "嗨"], ["--background", "transparent"],
            ["--background-description", "白色背景"],
        ):
            with self.subTest(options=options), patch.object(
                    sys, "argv", ["stickers.py", "--resume", str(self.path), *options]):
                with self.assertRaises(stickers.SkillError):
                    stickers.main()


if __name__ == "__main__":
    unittest.main()
