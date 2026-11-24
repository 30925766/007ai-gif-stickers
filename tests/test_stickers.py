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
OPEN_HTTPS = stickers.open_https
REVEAL_FILE = stickers.reveal_file


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
        if hasattr(stickers, "open_https"):
            self.start_patch(patch.object(
                stickers, "open_https",
                side_effect=AssertionError("Unexpected real network request"),
            ))
        self.start_patch(patch.object(
            stickers, "open_media_https",
            side_effect=AssertionError("Unexpected real media request"),
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
            capture_output=True, encoding="utf-8", errors="replace", check=False,
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
        prepared = stickers.prepare_prompt("红色衣服，白色配字，挥手。", caption="嗨")
        payload = stickers.build_payload(prepared["prompt"], "1:1", [], prepared["background"])
        self.assertEqual(prepared["background_mode"], "transparent")
        self.assertNotIn("background", payload)
        self.assertEqual(payload["output_format"], "gif")
        self.assertIn(stickers.GIF_REQUIREMENTS, payload["prompt"])
        self.assertNotIn("背景要求：", payload["prompt"])
        self.assertIn("透明背景", payload["prompt"])
        self.assertEqual(prepared["prompt_template_version"], stickers.PROMPT_TEMPLATE_VERSION)
        self.assertEqual(prepared["original_prompt"], "红色衣服，白色配字，挥手。")

    def test_custom_and_preserved_backgrounds_do_not_receive_transparency(self):
        for mode, description in (
            ("custom", "白色背景"), ("custom", "公园背景"),
            ("preserve", None), ("preserve", "保留参考图的公园"),
            (None, "粉色背景"), ("none", None),
        ):
            with self.subTest(mode=mode, description=description):
                prepared = stickers.prepare_prompt("挥手", mode, description, caption="嗨")
                payload = stickers.build_payload(
                    prepared["prompt"], "1:1", ["https://example.com/ref.png"], prepared["background"],
                )
                self.assertNotIn("background", payload)
                self.assertIn(stickers.GIF_REQUIREMENTS, payload["prompt"])
                if description:
                    self.assertIn(description, payload["prompt"])

    def test_conflicting_or_incomplete_background_options_are_rejected(self):
        for mode, description in (("transparent", "白色背景"), ("custom", None), ("custom", " ")):
            with self.subTest(mode=mode), self.assertRaises(stickers.SkillError):
                stickers.prepare_prompt("挥手", mode, description, caption="嗨")
        with self.assertRaises(stickers.SkillError):
            self.generate_with_options(["--background", "preserve"])

    def test_caption_is_kept_verbatim_and_separate_from_requirements(self):
        caption = ' 嗨！"你好"\nGIF '
        prepared = stickers.prepare_prompt("开心挥手", caption=caption)
        self.assertEqual(prepared["caption"], caption)
        self.assertIn(json.dumps(caption, ensure_ascii=False), prepared["prompt"])
        self.assertEqual(prepared["prompt"].count(stickers.GIF_REQUIREMENTS), 1)
        self.assertNotIn("背景要求：", prepared["prompt"])

    def test_missing_and_blank_captions_stop_before_network_or_job_creation(self):
        for caption in (None, "", " \n\t"):
            with self.subTest(caption=caption), patch.object(stickers, "request_json") as request:
                options = [] if caption is None else ["--caption", caption]
                with self.assertRaises(stickers.SkillError) as error:
                    self.generate_with_options(options, dry_run=False)
                self.assertEqual(error.exception.code, "caption_required")
                request.assert_not_called()
                self.assertFalse((self.root / "output").exists())

    def test_batch_reveals_each_file_before_processing_next_item(self):
        self.job["tasks"] = [
            {"index": index, "task_id": "task-" + str(index), "status": "waiting"}
            for index in range(1, 4)
        ]
        events = []

        def finish(job, task, path, *args):
            events.append(("finish", task["index"]))
            destination = self.root / (str(task["index"]) + ".gif")
            destination.write_bytes(self.gif)
            task.update(status="saved", file=str(destination))

        def reveal(file):
            events.append(("reveal", int(Path(file).stem)))

        with patch.object(stickers, "finish_task", side_effect=finish), patch.object(
                stickers, "reveal_file", side_effect=reveal), patch.object(
                stickers, "request_json") as request:
            self.assertEqual(self.resume(), 0)
            self.assertEqual(events, [
                ("finish", 1), ("reveal", 1), ("finish", 2), ("reveal", 2),
                ("finish", 3), ("reveal", 3),
            ])
            events.clear()
            self.assertEqual(self.resume(), 0)
            self.assertEqual(events, [])
            request.assert_not_called()

    def test_successful_file_is_revealed_even_when_next_task_fails(self):
        self.job["tasks"].append({"index": 2, "task_id": "second-id", "status": "waiting"})

        def finish(job, task, path, *args):
            if task["index"] == 2:
                raise stickers.SkillError("download failed", "download")
            destination = self.root / "saved.gif"
            destination.write_bytes(self.gif)
            task.update(status="saved", file=str(destination))

        with patch.object(stickers, "finish_task", side_effect=finish), patch.object(
                stickers, "reveal_file") as reveal:
            self.assertEqual(self.resume(), 2)
            reveal.assert_called_once_with(str(self.root / "saved.gif"))

    def test_explicit_transparent_mode_uses_prompt_without_api_background_field(self):
        prepared = stickers.prepare_prompt("开心挥手", background="transparent", caption="嗨")
        payload = stickers.build_payload(prepared["prompt"], "1:1", [], prepared["background"])
        self.assertNotIn("background", payload)
        self.assertEqual(payload["output_format"], "gif")
        self.assertIn("透明背景", payload["prompt"])
        self.assertNotIn("背景要求：", payload["prompt"])

    def test_user_transparency_description_is_preserved_verbatim(self):
        original = "挥手，透明背景"
        prepared = stickers.prepare_prompt(original, caption="嗨")
        self.assertEqual(prepared["original_prompt"], original)
        self.assertIn(original, prepared["prompt"])
        self.assertEqual(prepared["prompt"].count("透明背景"), 1)
        self.assertNotIn("背景要求：", prepared["prompt"])

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

    def test_gif_validation_accepts_different_backgrounds(self):
        for content in (self.gif, self.transparent_gif(),
                        self.transparent_gif(second_frame_opaque=True)):
            with self.subTest(content_size=len(content)):
                metadata = stickers.inspect_gif(content)
                self.assertEqual(metadata["format"], "GIF")
                self.assertEqual(metadata["frames"], 2)

    def test_opaque_output_in_transparent_mode_is_saved_without_regeneration(self):
        self.job["background"] = "transparent"
        self.job["tasks"][0]["result_url"] = "https://example.com/opaque.gif"
        with patch.object(stickers, "request_json") as request, patch.object(
                stickers, "download_gif", return_value=self.download) as download:
            self.assertEqual(self.resume(), 0)
        request.assert_not_called()
        self.assertEqual(download.call_count, 1)
        task = stickers.read_json(self.path)["tasks"][0]
        self.assertEqual(task["status"], "saved")
        self.assertIsNone(task["error"])
        self.assertEqual(Path(task["file"]).read_bytes(), self.gif)
        self.assertEqual(task["task_id"], "original-id")

    def test_valid_transparent_output_is_saved(self):
        self.job["background"] = "transparent"
        self.job["tasks"][0]["result_url"] = "https://example.com/transparent.gif"
        content = self.transparent_gif()
        with patch.object(stickers, "download_gif", return_value=(
                content, stickers.inspect_gif(content))):
            self.assertEqual(self.resume(), 0)
        task = stickers.read_json(self.path)["tasks"][0]
        self.assertEqual(task["gif"]["format"], "GIF")
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

    def write_plan_source(self, items=None):
        plan_source = self.root / "creative-plan.json"
        plan_source.write_text(json.dumps({
            "type": "剧情反转", "story_format": "multi_gif",
            "items": [{**item, "story_group": item.get("story_group", 1)} for item in items] if items else [
                {"story_group": 1, "story_role": "铺垫", "caption": "今天开始减肥！",
                 "scene": "宣布减肥", "action": "握拳并点头",
                 "prompt": "角色坚定地握拳，宣布开始减肥。"},
                {"story_group": 1, "story_role": "反转", "caption": "先吃完这顿",
                 "scene": "被食物吸引", "action": "转头闻香并开心开吃",
                 "prompt": "角色闻到香味，转头发现食物并立刻开心开吃。"},
            ],
        }, ensure_ascii=False), encoding="utf-8")
        return plan_source

    def create_pending_plan(self, items=None):
        source = self.write_plan_source(items)
        output = self.root / "plans"
        with patch.object(sys, "argv", [
                "stickers.py", "--plan-file", str(source), "--output-dir", str(output)]):
            self.assertEqual(stickers.main(), 0)
        return next(output.glob("plan-*.json"))

    def test_plan_creation_is_offline_keyless_and_waits_for_confirmation(self):
        source = self.write_plan_source()
        image_path = self.root / "reference.png"
        Image.new("RGB", (8, 8), "red").save(image_path)
        with patch.object(sys, "argv", [
                "stickers.py", "--plan-file", str(source), "--image", str(image_path),
                "--output-dir", str(self.root / "plans")]), patch.object(
                stickers, "require_keys", side_effect=AssertionError("Planning needs no keys")), patch.object(
                stickers, "request_json") as request:
            self.assertEqual(stickers.main(), 0)
        request.assert_not_called()
        path = next((self.root / "plans").glob("plan-*.json"))
        plan = stickers.read_json(path)
        self.assertEqual(plan["plan_status"], "awaiting_confirmation")
        self.assertEqual(plan["sticker_type"], "剧情反转")
        self.assertEqual(plan["story_format"], "multi_gif")
        self.assertEqual([task["caption"] for task in plan["tasks"]], ["今天开始减肥！", "先吃完这顿"])
        self.assertEqual([task["story_role"] for task in plan["tasks"]], ["铺垫", "反转"])
        self.assertEqual(
            plan["references"],
            [{"path": str(image_path.resolve()), "upload": True}],
        )
        self.assertEqual(plan["images"], [])
        self.assertTrue(all(task["status"] == "not_submitted" for task in plan["tasks"]))
        self.assertTrue(all(task["prompt"].count(stickers.GIF_REQUIREMENTS) == 1
                            for task in plan["tasks"]))
        self.assertTrue(all(task["caption"] in task["prompt"] for task in plan["tasks"]))

    def test_plan_schema_rejects_missing_caption_prompt_action_and_bad_counts(self):
        good = {"prompt": "动作", "caption": "你好", "action": "挥手"}
        for source in (
            None, {}, {"items": []}, {"items": [good] * 21},
            {"items": [None]},
            {"items": [{**good, "caption": " "}]},
            {"items": [{**good, "prompt": ""}]},
            {"items": [{**good, "action": None}]},
        ):
            with self.subTest(source=source), self.assertRaises(stickers.SkillError) as error:
                stickers.validate_plan(source)
            self.assertEqual(error.exception.code, "invalid_plan")

    def test_story_groups_reject_five_item_split_and_validate_boundaries(self):
        item = {"prompt": "角色挥手", "caption": "嗨", "action": "挥手"}
        for groups, valid in (
            ([1], True), ([1] * 5, True), ([1] * 3 + [2] * 2, False),
            ([1] * 6, False), ([1] * 3 + [2] * 3, True),
            ([1] * 5 + [2] * 2, False), ([1] * 3 + [2] * 4, True),
            ([1] * 5 + [2] * 5, True),
            ([1] * 4 + [2] * 4 + [3] * 3, True),
        ):
            plan = {"type": "搞笑剧情反转",
                    "items": [{**item, "story_group": group} for group in groups]}
            with self.subTest(groups=groups):
                if valid:
                    stickers.validate_plan(plan)
                else:
                    with self.assertRaises(stickers.SkillError) as error:
                        stickers.validate_plan(plan)
                    self.assertEqual(error.exception.code, "invalid_story_groups")

    def test_story_group_override_requires_user_request_and_matching_count(self):
        plan = {"type": "剧情反转", "story_count_override": 2,
                "items": [{"prompt": "角色挥手", "caption": "嗨", "action": "挥手",
                           "story_group": group} for group in [1, 1, 1, 2, 2]]}
        with self.assertRaises(stickers.SkillError):
            stickers.validate_plan(plan)
        plan["story_count_request"] = "这五张分成两个故事，三张加两张"
        stickers.validate_plan(plan)
        plan["story_count_override"] = 3
        with self.assertRaises(stickers.SkillError):
            stickers.validate_plan(plan)

    def test_confirmation_rechecks_pending_story_groups_before_keys_or_network(self):
        path = self.create_pending_plan([
            {"prompt": "角色挥手", "caption": "嗨", "action": "挥手"} for _ in range(5)])
        job = stickers.read_json(path)
        for task in job["tasks"][3:]:
            task["story_group"] = 2
        stickers.write_json(path, job)
        with patch.object(sys, "argv", ["stickers.py", "--confirm-plan", str(path)]), patch.object(
                stickers, "require_keys") as keys, patch.object(stickers, "request_json") as request:
            with self.assertRaises(stickers.SkillError) as error:
                stickers.main()
        self.assertEqual(error.exception.code, "invalid_story_groups")
        keys.assert_not_called()
        request.assert_not_called()

    def test_pending_plan_cannot_resume_but_confirmation_can_wait_for_credentials(self):
        path = self.create_pending_plan()
        for action in ("--resume", "--confirm-plan"):
            with self.subTest(action=action), patch.object(sys, "argv", [
                    "stickers.py", action, str(path)]), patch.object(
                    stickers, "require_keys", side_effect=stickers.SkillError(
                        "缺少 Key", "credentials_required")), patch.object(
                    stickers, "request_json") as request:
                if action == "--resume":
                    with self.assertRaises(stickers.SkillError) as error:
                        stickers.main()
                    self.assertEqual(error.exception.code, "plan_confirmation_required")
                else:
                    with self.assertRaises(stickers.SkillError) as error:
                        stickers.main()
                    self.assertEqual(error.exception.code, "credentials_required")
                request.assert_not_called()
        self.assertEqual(stickers.read_json(path)["plan_status"], "awaiting_confirmation")

    def test_confirmed_plan_submits_each_distinct_item_and_can_resume_without_resubmission(self):
        path = self.create_pending_plan()
        self.save_keys("fake-imgbb")
        submitted = []
        next_id = iter(("paid-task-1", "paid-task-2"))

        def submit(url, provider, key, payload=None, form=None):
            submitted.append(payload)
            return {"id": next(next_id)}

        def finish(job, task, job_path, *args):
            destination = self.root / ("saved-" + str(task["index"]) + ".gif")
            destination.write_bytes(self.gif)
            task.update(status="saved", file=str(destination), error=None)
            stickers.write_json(job_path, job)

        with patch.object(sys, "argv", ["stickers.py", "--confirm-plan", str(path)]), patch.object(
                stickers, "require_keys", return_value=self.keys), patch.object(
                stickers, "request_json", side_effect=submit) as request, patch.object(
                stickers, "finish_task", side_effect=finish), patch.object(
                stickers, "reveal_file"):
            self.assertEqual(stickers.main(), 0)
        self.assertEqual(request.call_count, 2)
        self.assertNotEqual(submitted[0]["prompt"], submitted[1]["prompt"])
        self.assertIn("今天开始减肥！", submitted[0]["prompt"])
        self.assertIn("先吃完这顿", submitted[1]["prompt"])
        self.assertTrue(all(payload["output_format"] == "gif" for payload in submitted))
        saved = stickers.read_json(path)
        self.assertEqual(saved["plan_status"], "confirmed")
        self.assertTrue(all(task["status"] == "saved" for task in saved["tasks"]))
        with patch.object(sys, "argv", ["stickers.py", "--confirm-plan", str(path)]), patch.object(
                stickers, "require_keys", return_value=self.keys), patch.object(
                stickers, "request_json") as request, patch.object(
                stickers, "reveal_file"):
            with self.assertRaises(stickers.SkillError) as error:
                stickers.main()
        self.assertEqual(error.exception.code, "invalid_plan_state")
        request.assert_not_called()

    def test_confirmation_never_retries_uncertain_items_but_continues_unsubmitted_ones(self):
        path = self.create_pending_plan()
        job = stickers.read_json(path)
        job["plan_status"] = "confirmed"
        job["tasks"][0]["status"] = "submission_unconfirmed"
        stickers.write_json(path, job)
        self.save_keys("fake-imgbb")
        with patch.object(sys, "argv", ["stickers.py", "--confirm-plan", str(path)]), patch.object(
                stickers, "require_keys", return_value=self.keys), patch.object(
                stickers, "request_json") as request:
            with self.assertRaises(stickers.SkillError) as error:
                stickers.main()
        self.assertEqual(error.exception.code, "invalid_plan_state")
        request.assert_not_called()
        saved = stickers.read_json(path)
        self.assertEqual(saved["tasks"][0]["status"], "submission_unconfirmed")
        self.assertEqual(saved["tasks"][1]["status"], "not_submitted")

    def test_plan_respects_per_item_background_and_includes_action(self):
        path = self.create_pending_plan([
            {"prompt": "角色开心", "caption": "嗨", "action": "挥手三次",
             "background": "custom", "background_description": "粉色背景"},
            {"prompt": "角色点头", "caption": "收到", "action": "点头两次"},
        ])
        planned = stickers.read_json(path)
        self.assertIsNone(planned["tasks"][0]["background"])
        self.assertIn("粉色背景", planned["tasks"][0]["prompt"])
        self.assertIn("挥手三次", planned["tasks"][0]["prompt"])
        self.assertEqual(planned["tasks"][1]["background"], "transparent")
        submitted = []

        def submit(url, provider, key, payload=None):
            submitted.append(payload)
            return {"id": "task-" + str(len(submitted))}

        def finish(job, task, job_path, *args):
            destination = self.root / (str(task["index"]) + ".gif")
            destination.write_bytes(self.gif)
            task.update(status="saved", file=str(destination))
            stickers.write_json(job_path, job)

        with patch.object(sys, "argv", ["stickers.py", "--confirm-plan", str(path)]), patch.object(
                stickers, "require_keys", return_value=self.keys), patch.object(
                stickers, "request_json", side_effect=submit), patch.object(
                stickers, "finish_task", side_effect=finish):
            self.assertEqual(stickers.main(), 0)
        self.assertNotIn("background", submitted[0])
        self.assertNotIn("background", submitted[1])
        self.assertIn("透明背景", submitted[1]["prompt"])

    def test_confirm_plan_does_not_resubmit_rejected_items(self):
        path = self.create_pending_plan()
        job = stickers.read_json(path)
        job["plan_status"] = "confirmed"
        for task in job["tasks"]:
            task["status"] = "rejected"
        stickers.write_json(path, job)
        with patch.object(sys, "argv", ["stickers.py", "--confirm-plan", str(path)]), patch.object(
                stickers, "require_keys", return_value=self.keys), patch.object(
                stickers, "request_json") as request:
            with self.assertRaises(stickers.SkillError) as error:
                stickers.main()
        self.assertEqual(error.exception.code, "invalid_plan_state")
        request.assert_not_called()

    def test_invalid_gif_creates_reviewable_repair_without_paid_retry(self):
        self.job.update(prompt="original prompt", ratio="1:1", images=[], caption="嗨")
        self.job["tasks"].append({"index": 2, "status": "not_submitted"})
        self.job["tasks"][0]["result_url"] = "https://example.com/not-gif.png"
        self.job["regeneration"] = {"mode": "confirm", "per_item": 1, "total": 2}
        with patch.object(stickers, "request_json", side_effect=[
                {"status": "completed", "url": "https://example.com/not-gif.png"},
        ]) as request, patch.object(stickers, "download_gif", side_effect=
                                   stickers.SkillError("not GIF", "invalid_gif")):
            self.assertEqual(self.resume(), 2)
        self.assert_query_only(request)
        task = stickers.read_json(self.path)["tasks"][0]
        self.assertEqual(task["status"], "repair_pending")
        self.assertEqual(task["repair"]["defect"], "invalid_gif")
        self.assertIn("GIF", task["repair"]["prompt"])
        self.assertEqual(task["task_id"], "original-id")
        self.assertEqual(stickers.read_json(self.path)["tasks"][1]["status"], "not_submitted")

    def test_auto_regeneration_submits_once_and_preserves_original_task(self):
        self.job.update(prompt="original prompt", ratio="1:1", images=[], caption="嗨")
        self.job["tasks"][0]["result_url"] = "https://example.com/not-gif.png"
        self.job["regeneration"] = {"mode": "auto", "per_item": 1, "total": 1}

        def request(url, provider, key, payload=None):
            if payload:
                self.assertEqual(payload["output_format"], "gif")
                self.assertIn("original prompt", payload["prompt"])
                return {"id": "replacement-id"}
            return {"status": "completed", "url": "https://example.com/not-gif.png"}

        with patch.object(stickers, "request_json", side_effect=request) as submit, patch.object(
                stickers, "download_gif", side_effect=[
                    stickers.SkillError("bad format", "invalid_gif"),
                    stickers.SkillError("bad format", "invalid_gif"),
                    self.download,
                ]):
            self.assertEqual(self.resume(), 0)
        posts = [call for call in submit.call_args_list if call.kwargs.get("payload")]
        self.assertEqual(len(posts), 1)
        task = stickers.read_json(self.path)["tasks"][0]
        self.assertEqual(task["status"], "saved")
        self.assertEqual(task["task_id"], "replacement-id")
        self.assertEqual(task["regeneration_count"], 1)
        self.assertEqual(task["history"][0]["task_id"], "original-id")
        self.assertIn("-r1.gif", task["file"])

    def test_reviewed_caption_defect_needs_approval_and_keeps_old_file(self):
        self.job.update(prompt="original prompt", ratio="1:1", images=[], caption="嗨")
        old_file = self.root / "old.gif"
        old_file.write_bytes(self.gif)
        self.job["tasks"][0].update(status="saved", file=str(old_file),
                                    prompt="original prompt", caption="嗨")
        stickers.write_json(self.path, self.job)
        self.save_keys("fake-imgbb")
        with patch.object(sys, "argv", [
                "stickers.py", "--report-defect", str(self.path), "--index", "1",
                "--defect", "caption_mismatch", "--evidence", "帧中只有'哈'，目标为'嗨'",
        ]), patch.object(stickers, "request_json") as request:
            self.assertEqual(stickers.main(), 0)
        request.assert_not_called()
        pending = stickers.read_json(self.path)["tasks"][0]
        self.assertEqual(pending["status"], "repair_pending")
        self.assertEqual(pending["task_id"], "original-id")
        self.assertIn('"嗨"', pending["repair"]["prompt"])
        with patch.object(stickers, "request_json") as request:
            self.assertEqual(stickers.execute(stickers.read_json(self.path), self.path,
                                               self.keys, resume=True), 2)
        request.assert_not_called()
        with patch.object(sys, "argv", [
                "stickers.py", "--approve-regeneration", str(self.path), "--index", "1",
        ]), patch.object(stickers, "request_json", return_value={"id": "replacement-id"}) as request, patch.object(
                stickers, "download_gif", return_value=self.download), patch.object(
                stickers, "wait_for_result", side_effect=lambda job, task, *args: task.update(
                    result_url="https://example.com/replacement.gif")):
            self.assertEqual(stickers.main(), 0)
        self.assertEqual(request.call_count, 1)
        final = stickers.read_json(self.path)["tasks"][0]
        self.assertEqual(final["history"][0]["file"], str(old_file))
        self.assertEqual(old_file.read_bytes(), self.gif)
        self.assertEqual(final["regeneration_count"], 1)

    def test_uncertain_regeneration_submission_never_reposts_on_resume(self):
        self.job.update(prompt="original prompt", ratio="1:1", images=[], caption="嗨")
        self.job["regeneration"] = {"mode": "auto", "per_item": 1, "total": 1}
        self.job["tasks"][0]["result_url"] = "https://example.com/bad.gif"
        with patch.object(stickers, "download_gif", side_effect=
                          stickers.SkillError("invalid", "invalid_gif")), patch.object(
                stickers, "request_json", side_effect=[
                    {"status": "completed", "url": "https://example.com/bad.gif"},
                    stickers.SkillError("network", "network"),
                ]) as request:
            self.assertEqual(self.resume(), 2)
        self.assertEqual(request.call_count, 2)
        task = stickers.read_json(self.path)["tasks"][0]
        self.assertEqual(task["status"], "submission_unconfirmed")
        self.assertEqual(task["history"][0]["task_id"], "original-id")
        with patch.object(stickers, "request_json") as request:
            self.assertEqual(stickers.execute(stickers.read_json(self.path), self.path,
                                               self.keys, resume=True), 2)
        request.assert_not_called()

    def test_download_error_does_not_trigger_paid_regeneration(self):
        self.job.update(prompt="original prompt", ratio="1:1", images=[], caption="嗨")
        self.job["regeneration"] = {"mode": "auto", "per_item": 1, "total": 1}
        self.job["tasks"][0]["result_url"] = "https://example.com/missing.gif"
        with patch.object(stickers, "request_json", return_value={
                "status": "completed", "url": "https://example.com/missing.gif",
        }) as request, patch.object(stickers, "download_gif", side_effect=
                                   stickers.SkillError("unavailable", "download")):
            self.assertEqual(self.resume(), 2)
        self.assert_query_only(request)
        self.assertNotIn("repair", stickers.read_json(self.path)["tasks"][0])

    def test_auto_regeneration_requires_explicit_flag_and_respects_limit(self):
        self.save_keys("fake-imgbb")
        prompt = self.root / "prompt.txt"
        prompt.write_text("挥手", encoding="utf-8")
        with patch.object(sys, "argv", [
                "stickers.py", "--prompt-file", str(prompt), "--caption", "嗨",
                "--output-dir", str(self.root / "output"), "--max-regenerations-total", "1",
        ]), patch.object(stickers, "request_json") as request:
            with self.assertRaises(stickers.SkillError):
                stickers.main()
        request.assert_not_called()
        self.job.update(prompt="挥手", ratio="1:1", images=[], caption="嗨")
        self.job["regeneration"] = {"mode": "auto", "per_item": 1, "total": 1}
        self.job["tasks"][0]["result_url"] = "https://example.com/bad.gif"
        with patch.object(stickers, "request_json", side_effect=[
                {"status": "completed", "url": "https://example.com/bad.gif"},
                {"id": "replacement-id"},
                {"status": "completed", "url": "https://example.com/also-bad.gif"},
                {"status": "completed", "url": "https://example.com/also-bad.gif"},
        ]) as request, patch.object(stickers, "download_gif", side_effect=
                                   stickers.SkillError("invalid", "invalid_gif")):
            self.assertEqual(self.resume(), 2)
        posts = [call for call in request.call_args_list if call.kwargs.get("payload")]
        self.assertEqual(len(posts), 1)
        task = stickers.read_json(self.path)["tasks"][0]
        self.assertEqual(task["status"], "repair_limit")
        self.assertEqual(task["regeneration_count"], 1)
        self.assertEqual(task["history"][0]["task_id"], "original-id")
        with patch.object(stickers, "request_json") as request:
            self.assertEqual(stickers.execute(stickers.read_json(self.path), self.path,
                                               self.keys, resume=True), 2)
        request.assert_not_called()

    def test_approval_without_key_does_not_reserve_or_charge(self):
        self.job.update(prompt="original prompt", ratio="1:1", images=[], caption="嗨")
        self.job["tasks"][0]["result_url"] = "https://example.com/bad.gif"
        stickers.propose_repair(self.job, self.job["tasks"][0], self.path,
                                "invalid_gif", "两次下载均为静态 PNG")
        before = stickers.read_json(self.path)
        with patch.object(sys, "argv", [
                "stickers.py", "--approve-regeneration", str(self.path), "--index", "1",
        ]), patch.object(stickers, "request_json") as request:
            with self.assertRaises(stickers.SkillError) as error:
                stickers.main()
        self.assertEqual(error.exception.code, "credentials_required")
        self.assertEqual(stickers.read_json(self.path), before)
        request.assert_not_called()

    def test_auto_mode_reports_visual_defect_and_regenerates_only_that_item(self):
        self.job.update(prompt="original prompt", ratio="1:1", images=[], caption="嗨",
                        regeneration={"mode": "auto", "per_item": 1, "total": 1})
        old_file = self.root / "old.gif"
        old_file.write_bytes(self.gif)
        self.job["tasks"][0].update(status="saved", file=str(old_file))
        self.job["tasks"].append({"index": 2, "status": "not_submitted"})
        stickers.write_json(self.path, self.job)
        self.save_keys()
        with patch.object(sys, "argv", [
                "stickers.py", "--report-defect", str(self.path), "--index", "1",
                "--defect", "visual_mismatch", "--evidence", "参考角色是红色衣服，生成结果为蓝色衣服",
        ]), patch.object(stickers, "request_json", return_value={"id": "replacement-id"}) as request, patch.object(
                stickers, "download_gif", return_value=self.download), patch.object(
                stickers, "wait_for_result", side_effect=lambda job, task, *args: task.update(
                    result_url="https://example.com/replacement.gif")):
            self.assertEqual(stickers.main(), 2)
        self.assertEqual(request.call_count, 1)
        final = stickers.read_json(self.path)
        self.assertEqual(final["tasks"][0]["status"], "saved")
        self.assertEqual(final["tasks"][0]["history"][0]["file"], str(old_file))
        self.assertEqual(final["tasks"][1]["status"], "not_submitted")
        self.assertTrue(old_file.exists())

    def test_confirmed_plan_records_explicit_auto_limits(self):
        path = self.create_pending_plan()
        self.save_keys("fake-imgbb")
        with patch.object(sys, "argv", [
                "stickers.py", "--confirm-plan", str(path), "--auto-regenerate",
                "--max-regenerations-per-item", "2", "--max-regenerations-total", "3",
        ]), patch.object(stickers, "request_json", return_value={"id": "task-1"}), patch.object(
                stickers, "finish_task", side_effect=stickers.SkillError("wait", "timeout")):
            self.assertEqual(stickers.main(), 2)
        job = stickers.read_json(path)
        self.assertEqual(job["regeneration"], {"mode": "auto", "per_item": 2, "total": 3})
        with patch.object(sys, "argv", [
                "stickers.py", "--confirm-plan", str(path), "--auto-regenerate",
        ]), patch.object(stickers, "request_json") as request:
            with self.assertRaises(stickers.SkillError) as error:
                stickers.main()
        self.assertEqual(error.exception.code, "invalid_plan_state")
        request.assert_not_called()

    def test_reconfirming_confirmed_plan_never_submits_unsubmitted_items(self):
        path = self.create_pending_plan()
        self.save_keys("fake-imgbb")
        with patch.object(sys, "argv", ["stickers.py", "--confirm-plan", str(path)]), patch.object(
                stickers, "request_json", return_value={"id": "first-paid"}), patch.object(
                stickers, "finish_task", side_effect=stickers.SkillError("wait", "timeout")):
            self.assertEqual(stickers.main(), 2)
        record = stickers.read_json(path)
        record["tasks"].append({
            "index": 3, "status": "not_submitted", "caption": "下一张",
            "prompt": "继续剧情", "action": "挥手",
        })
        stickers.write_json(path, record)
        with patch.object(sys, "argv", ["stickers.py", "--confirm-plan", str(path)]), patch.object(
                stickers, "request_json") as request:
            with self.assertRaises(stickers.SkillError) as error:
                stickers.main()
        self.assertEqual(error.exception.code, "invalid_plan_state")
        request.assert_not_called()
        self.assertEqual(stickers.read_json(path)["tasks"][-1]["status"], "not_submitted")

    def test_invalid_repair_history_is_rejected_before_resume(self):
        self.job["tasks"][0].update(
            regeneration_count=1, history=[{"task_id": ""}], prompt="retry prompt",
        )
        with self.assertRaises(stickers.SkillError) as error:
            stickers.validate_job(self.job)
        self.assertEqual(error.exception.code, "invalid_job")

    def test_authenticated_requests_still_reject_redirects(self):
        from urllib.request import Request
        request = Request("https://example.com/api", headers={
            "Authorization": "Bearer dummy",
        })
        self.assertIsNone(stickers.NoRedirect().redirect_request(
            request, None, 302, "Found", {}, "https://other.example/api"))
        with patch.object(stickers.urllib.request, "build_opener") as build:
            OPEN_HTTPS(Request("https://example.com/file.gif"), timeout=5)
        self.assertTrue(any(isinstance(handler, stickers.NoRedirect)
                            for handler in build.call_args.args))

    def test_media_redirect_requires_public_https_and_strips_sensitive_headers(self):
        from urllib.request import Request
        handler = stickers.ValidatedMediaRedirect()
        request = Request("https://media.example/file.gif", headers={
            "User-Agent": "test-agent", "Authorization": "Bearer secret",
            "Cookie": "session=secret",
        })
        dns_result = [(None, None, None, None, ("93.184.216.34", 0))]
        with patch.object(stickers.socket, "getaddrinfo", return_value=dns_result):
            redirected = handler.redirect_request(
                request, None, 302, "Found", {}, "https://cdn.example/file.gif")
        self.assertEqual(redirected.full_url, "https://cdn.example/file.gif")
        self.assertEqual(redirected.get_header("User-agent"), "test-agent")
        self.assertIsNone(redirected.get_header("Authorization"))
        self.assertIsNone(redirected.get_header("Cookie"))

    def test_media_redirect_rejects_insecure_private_and_excessive_redirects(self):
        from urllib.request import Request
        request = Request("https://media.example/file.gif")
        handler = stickers.ValidatedMediaRedirect(max_redirects=1)
        public_dns = [(None, None, None, None, ("93.184.216.34", 0))]
        private_dns = [(None, None, None, None, ("127.0.0.1", 0))]
        with patch.object(stickers.socket, "getaddrinfo", return_value=public_dns):
            handler.redirect_request(
                request, None, 302, "Found", {}, "https://cdn.example/one.gif")
            with self.assertRaises(stickers.urllib.error.HTTPError):
                handler.redirect_request(
                    request, None, 302, "Found", {}, "https://cdn.example/two.gif")
        for destination, dns in (
            ("http://cdn.example/file.gif", public_dns),
            ("https://cdn.example/private.gif", private_dns),
        ):
            with self.subTest(destination=destination), patch.object(
                    stickers.socket, "getaddrinfo", return_value=dns):
                with self.assertRaises(stickers.urllib.error.HTTPError):
                    stickers.ValidatedMediaRedirect().redirect_request(
                        request, None, 302, "Found", {}, destination)

    def test_reveal_failure_falls_back_to_parent_folder_without_warning_output(self):
        saved = self.root / "saved.gif"
        saved.write_bytes(self.gif)
        with patch.object(stickers.platform, "system", return_value="Windows"), patch.object(
                stickers.subprocess, "run", side_effect=[OSError("select failed"), None]) as run, patch.object(
                stickers, "emit") as emit:
            self.assertTrue(REVEAL_FILE(saved))
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].args[0], ["explorer.exe", "/select,", str(saved.resolve())])
        self.assertEqual(run.call_args_list[1].args[0], ["explorer.exe", str(saved.resolve().parent)])
        emit.assert_not_called()

    def test_media_download_uses_separate_opener_without_credentials(self):
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.read.return_value = self.gif
        with patch.object(stickers, "validate_public_https_target") as validate, patch.object(
                stickers, "open_media_https", return_value=response) as download:
            content, metadata = stickers.download_gif("https://cdn.example/result.gif")
        validate.assert_called_once_with("https://cdn.example/result.gif")
        request = download.call_args.args[0]
        self.assertIsNone(request.get_header("Authorization"))
        self.assertIsNone(request.get_header("Cookie"))
        self.assertEqual(metadata["format"], "GIF")
        self.assertTrue(content.startswith(b"GIF"))

    def test_reveal_success_is_silent_on_windows_and_macos(self):
        saved = self.root / "saved.gif"
        saved.write_bytes(self.gif)
        for system in ("Windows", "Darwin"):
            with self.subTest(system=system), patch.object(
                    stickers.platform, "system", return_value=system), patch.object(
                    stickers.subprocess, "run") as run, patch.object(stickers, "emit") as emit:
                self.assertTrue(REVEAL_FILE(saved))
                run.assert_called_once()
                emit.assert_not_called()

    def test_reveal_total_failure_reports_saved_file_without_regenerating(self):
        saved = self.root / "saved.gif"
        saved.write_bytes(self.gif)
        with patch.object(stickers.platform, "system", return_value="Windows"), patch.object(
                stickers.subprocess, "run", side_effect=OSError("cannot open")), patch.object(
                stickers, "emit") as emit, patch.object(stickers, "request_json") as request:
            self.assertFalse(REVEAL_FILE(saved))
        self.assertEqual(emit.call_args.args[0]["file"], str(saved.resolve()))
        self.assertEqual(emit.call_args.args[0]["event"], "file_location_warning")
        self.assertTrue(saved.is_file())
        request.assert_not_called()

    def test_confirmation_does_not_overwrite_concurrently_saved_task_id(self):
        path = self.create_pending_plan()
        self.save_keys("fake-imgbb")

        def concurrent_submit(*args, **kwargs):
            current = stickers.read_json(path)
            current["plan_status"] = "confirmed"
            current["tasks"][0].update(status="submitted", task_id="already-paid")
            stickers.write_json(path, current)
            return self.keys

        with patch.object(sys, "argv", ["stickers.py", "--confirm-plan", str(path)]), patch.object(
                stickers, "require_keys", side_effect=concurrent_submit), patch.object(
                stickers, "request_json") as request, patch.object(
                stickers, "finish_task", side_effect=stickers.SkillError("timeout", "timeout")):
            self.assertEqual(stickers.main(), 2)
        request.assert_not_called()
        self.assertEqual(stickers.read_json(path)["tasks"][0]["task_id"], "already-paid")

    def test_continue_single_batch_submits_only_confirmed_unsent_items(self):
        self.job.update(prompt="original prompt", ratio="1:1", images=[], caption="嗨")
        self.job["tasks"].append({"index": 2, "status": "not_submitted"})
        stickers.write_json(self.path, self.job)
        self.save_keys("fake-imgbb")
        with patch.object(sys, "argv", [
                "stickers.py", "--continue-unsubmitted", str(self.path),
        ]), patch.object(stickers, "request_json", return_value={"id": "second-paid"}) as request, patch.object(
                stickers, "finish_task", side_effect=stickers.SkillError("timeout", "timeout")):
            self.assertEqual(stickers.main(), 2)
        self.assertEqual(request.call_count, 1)
        record = stickers.read_json(self.path)
        self.assertEqual(record["tasks"][0]["task_id"], "original-id")
        self.assertEqual(record["tasks"][1]["task_id"], "second-paid")

    def test_continue_single_batch_refuses_uncertain_submission(self):
        self.job.update(prompt="original prompt", ratio="1:1", images=[], caption="嗨")
        self.job["tasks"][0] = {"index": 1, "status": "submission_unconfirmed"}
        self.job["tasks"].append({"index": 2, "status": "not_submitted"})
        stickers.write_json(self.path, self.job)
        self.save_keys("fake-imgbb")
        with patch.object(sys, "argv", [
                "stickers.py", "--continue-unsubmitted", str(self.path),
        ]), patch.object(stickers, "request_json") as request:
            with self.assertRaises(stickers.SkillError) as error:
                stickers.main()
        self.assertEqual(error.exception.code, "submission_unconfirmed")
        request.assert_not_called()

    def test_saved_gif_requires_separate_visual_acceptance(self):
        self.job["tasks"][0]["result_url"] = "https://example.com/good.gif"
        with patch.object(stickers, "download_gif", return_value=self.download):
            self.assertEqual(self.resume(), 0)
        saved = stickers.read_json(self.path)
        self.assertEqual(saved["tasks"][0]["review_status"], "pending")
        self.assertFalse(stickers.delivery_ready(saved))
        self.save_keys("fake-imgbb")
        with patch.object(sys, "argv", [
                "stickers.py", "--accept-review", str(self.path), "--index", "1",
                "--evidence", "抽查开头中段末段，配字、角色和动作符合规划",
        ]), patch.object(stickers, "request_json") as request:
            self.assertEqual(stickers.main(), 0)
        request.assert_not_called()
        reviewed = stickers.read_json(self.path)["tasks"][0]
        self.assertEqual(reviewed["review_status"], "passed")
        self.assertIn("开头", reviewed["review_evidence"])
        self.assertTrue(stickers.delivery_ready(stickers.read_json(self.path)))


if __name__ == "__main__":
    unittest.main()
