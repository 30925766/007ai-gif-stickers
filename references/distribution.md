# 环境与分发

当前分发版本：V1.0.0（`VERSION` 文件记录为 `1.0.0`）。

## 安装

把 ZIP 中整个 `007ai-gif-stickers` 文件夹放入支持 Skill 的软件技能目录；
Codex 默认目录为用户主目录下 `.codex/skills/`。软件重新发现技能后调用即可。
首次调用才运行初始化；Skill 没有后台常驻或安装后自动执行钩子。

## 环境

- Windows：PowerShell 5.1+；优先复用 Python 3.10+。缺少时尝试 winget
  `Python.Python.3.12` 用户级安装；没有 winget 时使用 Python 官方 3.12.10
  安装器，按 ARM64/x64/x86 选包，安装于用户本地应用目录。
- macOS：bash；检测 PATH、Homebrew Intel/Apple Silicon 路径和 Python.framework。
  缺少时用现有 Homebrew 安装 `python@3.12`；无 Homebrew 时下载 Python 官方
  3.12.10 universal2 安装包，已有非交互管理员授权时运行 installer。
  不自动安装 Homebrew，不读取管理员密码，不关闭 TLS 或系统安全设置。
  需要授权时保留安装器并说明路径，用户完成安装后重新调用。
- 两个平台都在 Skill 内创建 `.venv`，只在该环境安装 Pillow。缺少 wheel 时
  安装失败会报告，不自行安装编译工具。环境重建使用新目录以保留旧环境。
- 网络请求使用系统/宿主代理配置和证书验证。Python 缺少根证书导致连接失败时，
  在独立环境使用 certifi 根证书，不关闭验证。企业代理需按组织要求配置证书。
- Python 兼容性检查覆盖 3.10+ 的语言能力；目标系统实际支持范围取决于其
  Python/Pillow 发行版。安装器 URL 固定、通过 HTTPS 从 python.org 获取；
  正式长期维护时应定期更新固定版本并做两平台实机回归。

来源： https://docs.python.org/3/library/venv.html
https://formulae.brew.sh/formula/python@3.12
https://www.python.org/downloads/release/python-31210/

## 凭据

根目录两个 JSON 以明文保存，macOS 尽可能限制为当前用户读写；
Windows 继承所在用户目录的 ACL。不要把已配置目录直接压缩分发。
自动打包脚本只包含 SKILL.md、agents/openai.yaml、.gitignore、
VERSION、scripts 中指定源文件和 references 中指定说明。不会包含用户密钥。
没有内置 Key 或兜底 Key，也不会读取环境变量、其他 Skill、宿主配置中的密钥。
每位使用者需要自行配置 007AI 和 imgBB 两个 Key；缺失时不会提交生成任务。

## 维护与验证

`<run> --check` 不检查网络；`<run> --check-remote` 只访问 007AI 模型列表，
不会创建付费任务。imgBB 在上传时验证。

运行 `python -m unittest discover -s tests -v` 做离线行为测试。测试不读取
真实密钥，不上传、不创建收费任务。分发包不含开发测试和运行环境。
跨平台安装逻辑的自动测试不能代替 macOS 实机安装验证。
