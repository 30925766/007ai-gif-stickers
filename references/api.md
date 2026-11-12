# 007AI GIF adapter

Source: https://snskj3j8df.apifox.cn/531084030e0
Checked: 2026-11-11. These are third-party provider model IDs and capabilities;
do not infer an official OpenAI model identity from this alias.

Create: POST https://007.007ai.cc/v1/videos
Query: GET https://007.007ai.cc/v1/videos/{task_id}
Authorization: Bearer 007AI key

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
Ratios: auto, 1:1, 16:9, 9:16, 4:3, 3:4, 3:2, 2:3, 5:4, 4:5, 21:9,
9:21, 1:3, 3:1, 2:1, 1:2.

The creation response contains `id` or `task_id`. Query statuses include
queued/pending/processing/in_progress/completed/failed. Successful URLs may
use `url`, `video_url` or `image_url`; `media_type=video` does not prove that
the actual response is a video. Inspect downloaded bytes as a GIF.

imgBB: POST https://api.imgbb.com/1/upload, form fields `key`, base64 `image`,
`expiration=86400`, `name`. Read `data.url`; never use viewer/delete URLs.
Max reference size is 32 MiB. No Key in query parameters or request logs.
Source: https://api.imgbb.com/

Errors: explicit invalid-key messages or HTTP 401 mark the corresponding key
invalid. Generic HTTP 403 is permissions, 402 is billing, 429 is rate limiting,
5xx is provider failure; none of these alone proves an invalid key.
Downloads do not receive either provider's credential.
