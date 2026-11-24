# 007AI GIF adapter

Source: https://snskj3j8df.apifox.cn/531084030e0
Checked: 2026-11-11. These are third-party provider model IDs and capabilities;
do not infer an official OpenAI model identity from this alias.

Create: POST https://007.007ai.cc/v1/videos
Query: GET https://007.007ai.cc/v1/videos/{task_id}
Authorization: Bearer 007AI key
Authenticated API requests reject all HTTP redirects. Media downloads alone may
follow up to 3 redirects. Validate HTTPS, absence of userinfo, and all resolved
addresses being global before each target request. Never forward authorization,
cookies or other sensitive headers. Reject unsafe or unresolvable destinations;
preserve the existing task ID and never submit a replacement for a download failure.
After a valid GIF is saved, reveal it in Explorer/Finder, falling back to opening
its parent folder. Successful opening is silent; report only actual failures.

```json
{
  "model": "gptimage2.6_gr",
  "prompt": "A genuinely animated looping chat sticker...",
  "aspect_ratio": "1:1",
  "image_size": "1K",
  "output_format": "gif",
  "images": ["https://public-host.example/reference.png"]
}
```

Only add `images` when references exist. Up to 8 HTTPS references. Never send
metadata, input_reference, Base64 images, seconds, resolution or quality.
This dedicated GIF workflow does not offer model, size or format switches.
Every new job appends the versioned GIF animation instructions to the original
prompt once. The job stores original_prompt, prompt (the exact submitted text),
prompt_template_version, background_mode, background_description and caption.
Dry runs use the same preparation path; ordinary resume never rebuilds the
original prompt. Explicitly authorized regeneration derives a new repair
prompt from the recorded original rather than mutating the old task.
Prompt template version 3 requires a nonempty --caption for every new generation.
The assistant preserves user-specified wording or chooses a short caption from
the expression/action when omitted. No paid request is made without a caption.
Each successfully saved GIF is immediately revealed in Explorer/Finder,
including individual items within a batch and downloads of resumed tasks.
The calling assistant interprets background intent and passes structured options:
transparent (default), custom with --background-description, or preserve.
All modes omit the API background field. Starting with prompt template version 5,
transparent mode appends only "透明背景", unless the original creative description
already contains that phrase. Custom and preserved backgrounds use their respective
prompt descriptions. The original user description and caption remain intact.
The CLI background option and job background metadata remain local controls;
they are not forwarded as an API field.
The legacy CLI background=none option never sends a literal "none" API value.
Output validation checks GIF format, animation and decoding, not transparency.
A valid animated GIF is accepted regardless of its actual background.
Ratios: auto, 1:1, 16:9, 9:16, 4:3, 3:4, 3:2, 2:3, 5:4, 4:5, 21:9,
9:21, 1:3, 3:1, 2:1, 1:2.

The creation response contains `id` or `task_id`. Query statuses include
queued/pending/processing/in_progress/completed/failed. Successful URLs may
use `url`, `video_url` or `image_url`; `media_type=video` does not prove that
the actual response is a video. Inspect downloaded bytes as a GIF.
Read URL fields at the response root or inside output/outputs/result/results/data
containers only; never recursively search input, reference or metadata fields.
On download/validation failure, discard the cached URL and query the existing
task ID for a fresh result, with at most one automatic download retry.
If two downloaded results are confirmed invalid animated GIFs, create a local
repair proposal. Default mode requires per-item approval before a new POST.
Explicitly preauthorized auto mode permits at most the configured per-item and
batch counts; a request with an uncertain submission result is never reposted.
Download/network failures and completed tasks without a result URL are not
proof of invalid output and never trigger paid regeneration.
The job keeps prior task IDs, prompts, files, and defect evidence in `history`;
regenerated GIFs use a distinct `-rN.gif` suffix. Script `ok` means GIF format
validation only; saved files have `review_status=pending`. Inspect caption,
character and motion, then use `--accept-review` with concrete evidence for
each item. `delivery_ready=true` requires every saved file to be reviewed.
For an observed defect, report `caption_mismatch` or `visual_mismatch` with
specific evidence via `--report-defect`. An approved prompt is derived from
the original submitted prompt and exact caption, not from an accumulated retry.
For single-prompt `--count` batches, `--continue-unsubmitted` starts only
previously unsubmitted items after explicit authorization. It refuses to run
when any submission has an unknown outcome and no task ID.
`--confirm-plan` is accepted only while a plan is awaiting its first confirmation;
use `--resume` for a confirmed plan, which never submits previously unsubmitted items.

imgBB: POST https://api.imgbb.com/1/upload, form fields `key`, base64 `image`,
`expiration=86400`, `name`. Read `data.url`; never use viewer/delete URLs.
Max reference size is 32 MiB. No Key in query parameters or request logs.
Source: https://api.imgbb.com/

Errors: explicit invalid-key messages or HTTP 401 mark the corresponding key
invalid. Generic HTTP 403 is permissions, 402 is billing, 429 is rate limiting,
5xx is provider failure; none of these alone proves an invalid key.
Downloads do not receive either provider's credential.
