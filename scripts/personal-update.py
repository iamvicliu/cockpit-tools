#!/usr/bin/env python3
"""Build the personal macOS fork; install only after validation and backup."""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from uuid import uuid4

FORK = "https://github.com/iamvicliu/cockpit-tools.git"
UPSTREAM = "https://github.com/jlcodes99/cockpit-tools.git"
BRANCH = "personal/cockpit"
IDENTIFIER = "com.jlcodes.cockpit-tools"
DEFAULT_ROOT = Path.home() / "artifacts/codex-generated/cockpit-maintenance"
SIDECAR_INPUTS = ("sidecars/cockpit-cliproxy", "src-tauri/build.rs")


def run(args, cwd=None, env=None, log=None):
    if log:
        with Path(log).open("a") as output:
            process = subprocess.Popen(args, cwd=cwd, env=env, stdout=output,
                                       stderr=subprocess.STDOUT, start_new_session=True)
            timeout = 3600 if args[0] == "node" else 600
            try:
                status = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                raise RuntimeError(f"命令超时，已停止该命令及子进程；诊断日志：{log}") from None
        if status:
            raise RuntimeError(f"命令失败：{args[0]}；诊断日志：{log}")
        return ""
    return subprocess.check_output(args, cwd=cwd, env=env, text=True, timeout=120).strip()


def save(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic publication; previous run artifacts remain in their run directory.
    temp = path.with_name(path.name + "." + uuid4().hex)
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    os.replace(temp, path)


def digest(path):
    checksum = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def sidecar_inputs(repo):
    return {p: run(["git", "rev-parse", "HEAD:" + p], cwd=repo)
            for p in SIDECAR_INPUTS}


def validate_cache(repo, metadata):
    if metadata["machine"] != platform.machine():
        raise RuntimeError("代理组件缓存的架构不匹配；请使用 --sidecar build。")
    if metadata["inputs"] != sidecar_inputs(repo):
        raise RuntimeError("上游代理组件源码或构建脚本已变化，禁止复用旧组件；需要 Go 后使用 --sidecar build。")
    for item in metadata["files"]:
        if digest(item["path"]) != item["sha256"]:
            raise RuntimeError("代理组件缓存校验失败；停止构建。")


def bundle_info(bundle):
    with (Path(bundle) / "Contents/Info.plist").open("rb") as stream:
        info = plistlib.load(stream)
    if info.get("CFBundleIdentifier") != IDENTIFIER:
        raise RuntimeError("App 标识不符，拒绝替换。")
    executable = Path(bundle) / "Contents/MacOS" / info["CFBundleExecutable"]
    if not executable.is_file():
        raise RuntimeError("App 主程序缺失。")
    return info


def stop_app(target):
    executable = str(target / "Contents/MacOS" / bundle_info(target)["CFBundleExecutable"])
    processes = run(["ps", "-axo", "pid=,comm="])
    pids = [int(line.strip().split(None, 1)[0]) for line in processes.splitlines()
            if len(line.strip().split(None, 1)) == 2
            and line.strip().split(None, 1)[1] == executable]
    for pid in pids:
        os.kill(pid, signal.SIGTERM)
    for _ in range(100):
        alive = []
        for pid in pids:
            try:
                os.kill(pid, 0)
                alive.append(pid)
            except ProcessLookupError:
                pass
        if not alive:
            return
        time.sleep(0.1)
    raise RuntimeError("App 未正常退出，停止替换；请先退出 Cockpit Tools。")


def swap_bundle(staged, target, backup):
    """Rename only: no deletion, partial copy, or overwriting an existing backup."""
    if backup.exists():
        raise RuntimeError("备份路径已存在，拒绝覆盖。")
    if staged.stat().st_dev != target.parent.stat().st_dev:
        raise RuntimeError("暂存 App 和目标不在同一文件系统，无法安全替换。")
    if backup.parent.stat().st_dev != target.parent.stat().st_dev:
        raise RuntimeError("备份和目标不在同一文件系统，无法安全备份。")
    existed = target.exists()
    if existed:
        os.rename(target, backup)
    try:
        os.rename(staged, target)
    except OSError:
        if existed:
            os.rename(backup, target)
        raise
    return existed


def install(source, root, expected_source=None):
    target = Path("/Applications/Cockpit Tools.app")
    source = Path(source).resolve()
    bundle_info(source)
    run(["codesign", "--verify", "--deep", "--strict", str(source)])
    if expected_source:
        recorded = json.loads((source / "Contents/Resources/personal-build.json").read_text())
        if recorded != expected_source:
            raise RuntimeError("构建标记与清单不匹配，拒绝安装。")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8]
    staged = root / "staging" / stamp / "Cockpit Tools.app"
    staged.parent.mkdir(parents=True)
    run(["ditto", str(source), str(staged)])
    run(["codesign", "--verify", "--deep", "--strict", str(staged)])
    backup = root / "backups" / stamp / "Cockpit Tools.app"
    backup.parent.mkdir(parents=True)
    if target.exists():
        stop_app(target)
    existed = swap_bundle(staged, target, backup)
    save(root / "installs" / (stamp + ".json"), {
        "source": str(source), "installed": str(target),
        "backup": str(backup) if existed else None,
    })
    print(f"已安装：{target}\n备份：{backup if existed else '此前未安装'}", flush=True)
    run(["open", str(target)])
    print("已启动 App；请在界面确认账号、到期提示和代理功能。")


def release(tag):
    args = ["gh", "release", "view"] + ([tag] if tag else [])
    data = json.loads(run(args + ["--repo", "jlcodes99/cockpit-tools",
                                "--json", "tagName,isPrerelease,isDraft,publishedAt"]))
    if data["isPrerelease"] or data["isDraft"]:
        raise RuntimeError("只允许官方已发布的稳定版本。")
    return data


def prepare(root, tag, sidecar):
    tools = ["git", "gh", "node", "npm", "cargo", "rustc", "xcrun", "codesign", "ditto"]
    if sidecar == "build":
        tools.append("go")
    for tool in tools:
        if not shutil.which(tool):
            raise RuntimeError(f"缺少工具 {tool}，未自动安装依赖。")
    run(["xcrun", "--sdk", "macosx", "--show-sdk-path"])
    info = release(tag)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8]
    work = root / "runs" / stamp
    work.mkdir(parents=True)
    repo = work / "source"
    log = work / "build.log"
    print(f"同步官方稳定版 {info['tagName']}；日志：{log}", flush=True)
    run(["git", "clone", "--single-branch", "--branch", BRANCH, FORK, str(repo)], log=log)
    run(["git", "remote", "add", "upstream", UPSTREAM], cwd=repo)
    run(["git", "fetch", "upstream", "tag", info["tagName"]], cwd=repo, log=log)
    # Preserve the PR branch and all personal commits. Conflict leaves a reviewable run directory.
    run(["git", "merge", "--no-edit", "refs/tags/" + info["tagName"]], cwd=repo, log=log)
    env = dict(os.environ)
    env.update(SDKROOT=run(["xcrun", "--sdk", "macosx", "--show-sdk-path"]),
               CARGO_TARGET_DIR=str(root / "cargo-target"), CARGO_PROFILE_DEV_DEBUG="0",
               CARGO_INCREMENTAL="0", CARGO_BUILD_JOBS="4")
    # Never inherit a skip flag from another shell when rebuilding the component.
    env.pop("COCKPIT_SKIP_CLIPROXY_BUILD", None)
    if sidecar == "reuse":
        metadata = json.loads((root / "sidecar-cache/metadata.json").read_text())
        validate_cache(repo, metadata)
        dest = repo / "sidecars/cockpit-cliproxy/bin"
        dest.mkdir(parents=True, exist_ok=True)
        for item in metadata["files"]:
            shutil.copy2(item["path"], dest / Path(item["path"]).name)
        env["COCKPIT_SKIP_CLIPROXY_BUILD"] = "1"
        print("已核对代理组件源码、构建脚本、架构和 SHA256；本次显式复用缓存。", flush=True)
    checks = [
        ("更新脚本安全回归测试", [sys.executable, "scripts/personal-update.test.py"]),
        ("安装项目依赖", ["npm", "ci"]),
        ("类型检查", ["npm", "run", "typecheck"]),
        ("TypeScript 测试", ["npm", "test"]),
        ("发布工具测试", ["npm", "run", "test:release"]),
    ]
    if sidecar == "build":
        checks.append(("代理组件测试", ["npm", "run", "test:go"]))
    checks += [
        ("前端构建", ["npm", "run", "build"]),
        ("桌面构建", ["node", "node_modules/@tauri-apps/cli/tauri.js", "build", "--debug",
                         "--bundles", "app", "--config", json.dumps({
                             "build": {"beforeBuildCommand": ""},
                             "bundle": {"createUpdaterArtifacts": False}})]),
    ]
    for label, args in checks:
        print(label + "…", flush=True)
        run(args, cwd=repo, env=env, log=log)
    if run(["git", "status", "--porcelain"], cwd=repo):
        raise RuntimeError(f"构建修改了受版本控制的文件，请先审查：{repo}；未推送或安装。")
    bundle = root / "cargo-target/debug/bundle/macos/Cockpit Tools.app"
    artifact = work / "Cockpit Tools.app"
    run(["ditto", str(bundle), str(artifact)])
    bundle_info(artifact)
    build = {"revision": run(["git", "rev-parse", "HEAD"], cwd=repo),
             "upstreamTag": info["tagName"], "builtAt": stamp,
             "machine": platform.machine(), "profile": "debug"}
    save(artifact / "Contents/Resources/personal-build.json", build)
    run(["codesign", "--force", "--deep", "--sign", "-", str(artifact)], log=log)
    run(["codesign", "--verify", "--deep", "--strict", str(artifact)], log=log)
    # A normal push rejects concurrent branch updates; never force-push.
    run(["git", "push", "origin", "HEAD:refs/heads/" + BRANCH], cwd=repo, log=log)
    manifest = {"app": str(artifact), "source": str(repo), "build": build, "log": str(log)}
    save(work / "manifest.json", manifest)
    save(root / "latest-build.json", manifest)
    # Keep an independent entry point after a successful, validated update.
    runner = root / "personal-update.py"
    if runner.exists():
        shutil.copy2(runner, work / "previous-personal-update.py")
    shutil.copy2(repo / "scripts/personal-update.py", runner)
    print(f"验证和推送完成。待安装 App：{artifact}", flush=True)
    return manifest


@contextlib.contextmanager
def lock(root):
    root.mkdir(parents=True, exist_ok=True)
    with (root / "update.lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("已有更新任务在运行。") from None
        yield


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["plan", "update", "install", "rollback"])
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--tag", help="指定官方稳定版；默认最新稳定版")
    parser.add_argument("--sidecar", choices=["build", "reuse"], default="build")
    parser.add_argument("--install", action="store_true", help="update 验证完成后备份并替换 App")
    parser.add_argument("--backup", type=Path, help="rollback 的完整备份 App 路径")
    args = parser.parse_args()
    if platform.system() != "Darwin":
        parser.error("此脚本只支持本机 macOS 构建。")
    os.environ["PATH"] = str(Path.home() / ".cargo/bin") + os.pathsep + os.environ.get("PATH", "")
    root = args.root.expanduser().resolve()
    if args.action == "plan":
        info = release(args.tag)
        print(f"官方稳定版：{info['tagName']}\n个人分支：{BRANCH}\n工作目录：{root}\n"
              "流程：独立克隆 → 合并稳定版 → 检查测试 → 构建签名 → 普通推送 → 可选备份安装\n"
              "plan 不同步代码、不构建、不替换 App。")
        return
    with lock(root):
        if args.action == "update":
            manifest = prepare(root, args.tag, args.sidecar)
            if args.install:
                install(manifest["app"], root, manifest["build"])
        elif args.action == "install":
            manifest = json.loads((root / "latest-build.json").read_text())
            install(manifest["app"], root, manifest["build"])
        else:
            if args.backup is None:
                parser.error("rollback 需要 --backup 完整路径。")
            backup = args.backup.expanduser().resolve()
            if not backup.is_relative_to(root / "backups"):
                parser.error("只接受本脚本 backups 目录中的备份。")
            install(backup, root)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.SubprocessError, ValueError, KeyError) as error:
        print(f"停止：{error}\n运行目录和日志均已保留；若已经完成替换，可从 installs 安装记录找到备份。", file=sys.stderr)
        sys.exit(1)
