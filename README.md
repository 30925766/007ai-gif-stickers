# 007AI GIF Stickers V1.2.2

通过 007AI 与 imgBB，把参考图或文字制作成 GIF 动态聊天表情包。
固定使用 `gptimage2.6_gr`、`image_size=1K`、`output_format=gif`，默认生成一个 1:1 GIF。

## 安装

从本仓库 V1.2.2 Release 下载 `007ai-gif-stickers-V1.2.2.zip`，
将其中的 `007ai-gif-stickers` 文件夹放入软件的技能目录。
Codex 默认目录为 `~/.codex/skills/`。首次调用时才初始化独立 Python 环境。

支持 Windows 与 macOS；macOS 安装流程尚未进行本次实机验证。
具体环境要求见 [环境与分发](references/distribution.md)。

## GIF 动画与背景

每次新生成自动加入多帧 GIF 动画、连续动作和无限循环的内置要求。
每张表情包必须带配字；未指定时由助手根据表情和动作自动拟定。
每张 GIF 成功保存后立即自动打开文件位置，批量及恢复下载也逐张执行。
用户没有指定背景时默认透明；明确指定颜色、场景或保留参考图背景时遵循用户要求。
服装、文字的颜色不属于背景要求，参考图自带背景也不代表需要保留。

提示词、请求格式和文件验收共同约束 GIF 输出。不检查输出是否透明，
不因背景不透明而拒绝有效 GIF；仍需预览确认配字和角色一致性。
任务记录保存原始描述、最终提示词和模板版本；恢复任务不会重新拼接或提交生成。

## 多张表情包规划

用户提供人物形象并要求制作多张、但未指定风格时，先让用户从网络热梗、搞笑整活、
可爱卖萌、日常聊天、打工职场、情绪表达、鼓励治愈、混合实用、剧情反转等类型中选择，
也可接受自定义或组合方向。选择后先逐张展示配字、场景、表情动作与剧情作用；
每张都有独立创意和非空配字。剧情反转会按张数安排铺垫、转折和包袱。

用户提出修改时按编号调整并重新展示；用户明确确认后才执行生成。规划命令只在本地
保存待确认记录，不读取 Key、不上传参考图、不调用生成接口。生成前由用户确认一次：

```text
<run> --plan-file <planning.json> --image <reference.png> --output-dir <output-directory>
<run> --confirm-plan <plan-record.json>
```

确认后每项使用独立提示词和配字逐个提交。中断后用 `--resume` 恢复已有任务；
待确认规划不会被恢复命令启动，提交状态不确定的项目不会重提。规划 JSON 结构见
[references/plans.md](references/plans.md)。

## 自行配置 Key

分发包与仓库不含任何用户密钥或内置兜底 Key。
新生成任务须自行配置 007AI API Key 和 imgBB API Key；
恢复已有任务及远程鉴权检查只要求有效的 007AI Key。
程序只读取本 Skill 目录下自行配置的两个 JSON 文件，
不读取环境变量、其他 Skill 或宿主配置中的凭据；缺失必要 Key 时不会提交任务。

获取入口：[007AI](https://007.007ai.cc/) · [imgBB API](https://api.imgbb.com/)。
通过对话调用 Skill 时会引导配置，也可使用 `--configure` 隐藏输入。
请勿把 Key 放入命令行参数、提示词或 Git 提交。

Windows 自检：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/run.ps1 --check
```

macOS 自检：

```bash
bash scripts/run.sh --check
```

完整操作说明见 [SKILL.md](SKILL.md)，接口约定见 [references/api.md](references/api.md)。
本地参考图会上传至 imgBB，默认 24 小时到期。API 服务按各自账户规则计费。

## 验证与打包

在安装 Pillow 与 certifi 的 Python 3.10+ 环境中运行：

```bash
python -m unittest discover -s tests -v
python scripts/package_skill.py --output /path/outside/skill/007ai-gif-stickers-V1.2.2.zip
```

离线回归测试覆盖 GIF 验收、提示词合并、背景参数、任务恢复、规划确认及逐项生成。
打包采用固定文件白名单，不含 Key、虚拟环境、缓存、日志、任务记录、生成图片或开发测试。
离线测试不会上传参考图或提交收费任务。

批量规划格式见 [references/plans.md](references/plans.md)。
版本号见 [VERSION](VERSION)；最近发布标签为 `V1.2.2`。
