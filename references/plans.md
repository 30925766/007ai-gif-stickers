# Batch Plan Format

The assistant creates this JSON after the user selects a type and before generation.
Each `items` entry becomes one GIF with its own caption and prompt. `prompt` contains
the creative description and action, not the generated caption. The script adds the
required GIF instructions and caption without making a network request.

```json
{
  "type": "剧情反转",
  "story_format": "multi_gif",
  "items": [
    {
      "story_group": 1,
      "story_role": "铺垫",
      "caption": "今天开始认真减肥！",
      "scene": "角色宣布开始减肥",
      "action": "握拳宣誓，认真点头",
      "prompt": "保持参考角色外观，站直并握拳，神情坚定。"
    },
    {
      "story_group": 1,
      "story_role": "反转",
      "caption": "先把这顿吃完",
      "scene": "刚宣布减肥就被美食吸引",
      "action": "闻到香味，转头看向食物，立刻开心开吃",
      "prompt": "保持参考角色外观，先闻到香味，再转头看向食物并开心开吃。"
    }
  ]
}
```

Top-level `type` and `story_format` are optional descriptive metadata. `items` is required
and must contain 1 to 20 objects. Each item requires nonempty `prompt`, `caption`, and
`action`; `scene`, `story_group`, and `story_role` are optional. User-provided captions
must be preserved exactly.

For a type containing "剧情反转", every item must specify `story_group` as a
nonempty story name or positive integer. By default, 1–5 items share one group;
6–10 items have two groups of at least three items each; 11–20 items use groups
of three to five. When the user explicitly requests another grouping, set the
top-level `story_count_override` to that number and `story_count_request` to their
actual wording. Group count must match the override; never invent user approval.
The script validates grouping before saving and before confirming a pending plan.
Prepare local prompt drafts and run offline validation before displaying the table;
do not show full drafts or submit requests before confirmation. Review causality
separately: identical group labels alone do not make unrelated events one story.

Each item may set `background` to `transparent` (the default), `custom`, or `preserve`.
`custom` requires a nonempty `background_description`; `preserve` requires a reference.
These are local planning options, not API fields. Transparent mode adds "透明背景"
to the prompt; the API request omits `background` and retains `output_format=gif`.
The script appends `action` to the creative prompt so the planned motion reaches the model.
Before confirmation, show the concise planning table only; do not display full prompts.
After edits or a selection of items, create a new pending plan containing only the final
selected items, show it again, and obtain confirmation. Never edit submitted records.

Create an awaiting-confirmation plan record with:

```text
<run> --plan-file <plan.json> --image <reference.png> --output-dir <output-directory>
```

This command validates references locally but does not upload them, read credentials,
or call a provider. Show the resulting plan to the user. Only after explicit confirmation,
start execution with:

```text
<run> --confirm-plan <plan-*.json>
```

The plan record persists each item's final prompt, caption, story metadata, and task state.
Resume a confirmed, interrupted plan using `--resume`; never resubmit tasks with uncertain
submission status or missing task IDs.
