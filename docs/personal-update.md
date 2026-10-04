# 个人定制版更新（macOS）

维护 `iamvicliu/cockpit-tools` 的 `personal/cockpit` 分支。原功能 PR 分支保持独立。
同步的是官方**稳定 release 标签**，不是上游尚未发布的 main。
合并保留个人提交；以后官方包含功能时，仍需审查是否有重复实现，不自动删除个人代码。

## 命令

在此仓库运行，或使用已生成的 `cockpit-maintenance/cockpit-update` 入口：

```sh
python3 scripts/personal-update.py plan
# 默认重新编译代理组件，需要 Go。只构建、验证和推送，不安装。
python3 scripts/personal-update.py update
# 本机已初始化代理组件缓存；仅源码、构建脚本、架构和摘要完全一致时允许复用。
python3 scripts/personal-update.py update --sidecar reuse
# 一次完成同步、测试、构建、推送、备份和安装。
python3 scripts/personal-update.py update --sidecar reuse --install
# 也可以审查构建日志和结果后单独安装。
python3 scripts/personal-update.py install
# 回退到运行时打印的备份路径；当前版本也会被备份，不删除任何 App。
python3 scripts/personal-update.py rollback --backup '/完整路径/backups/时间/Cockpit Tools.app'
```

可用 `--tag v1.3.65` 指定稳定版。最新稳定版无新提交时仍能重建已验证的个人分支。
默认工作目录：`~/artifacts/codex-generated/cockpit-maintenance`，可用 `--root` 指定。

## 前提和边界

- 本机 macOS、Git/GitHub CLI 已认证，拥有个人 fork 的写权限；Node/npm、Rust/Cargo、Xcode SDK，重新构建代理组件时还需要 Go。脚本不全局安装或升级工具。
- 在 App 设置中关闭“自动安装更新”，不要通过官方更新按钮安装官方包，否则定制功能仍会被覆盖。脚本不修改 App 的账号数据或系统设置。
- 当前使用与已验证本地 App 一致的 debug 构建（未优化），采用本地 ad-hoc 签名。不是官方签名、公证或公开分发包，不能用来接管官方自动更新通道。
- 更新先独立克隆个人分支，再合并官方稳定标签，执行类型检查、全量 TS 测试、发布工具测试、Rust 核心测试、前端和桌面构建。重新编译代理组件时也运行 Go 测试。成功后才普通推送个人分支；不强推、不影响原 PR。
- 合并冲突、测试失败、缓存源码变化、签名失败或远程并发更新时停止，保留现场和日志。失败后不自动清理目录；后续需审查运行目录。
- 显式 `--install` 或 `install` 才退出原 App、备份并替换。替换使用同一文件系统中的重命名；复制或签名失败不会覆盖当前 App，替换失败会恢复备份。
- `runs/` 存放每次完整源码、日志、构建包及清单；`backups/` 存放被替换的 App；`installs/` 记录安装来源。目录和备份不会自动删除，请审查磁盘占用后再决定清理。
- `latest-build.json` 是最近成功构建的清单；每个 App 内有 `Contents/Resources/personal-build.json` 标记源码提交、上游标签和构建时间。
- 构建和签名通过不代表所有 UI 流程都验证通过。安装后检查账号数据、重置卡到期提示、代理功能；异常可回退。回退仅还原 App，不回退 App 运行后产生的数据变化。

安全回归测试：`python3 scripts/personal-update.test.py`。测试只使用本地 Git 仓库和模拟 App，验证冲突/测试失败不推送、缓存变化拒绝复用、安装失败恢复及备份保留；测试目录保留供审查。
