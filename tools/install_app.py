"""Resumable first-run wizard. No Go, pip packages, secret arguments or full test run."""
from __future__ import annotations

import sys

if sys.version_info < (3, 11):
    raise SystemExit('需要 Python 3.11 或更新版本，请更新 Python 后重新运行 install.cmd。')

import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import time
import tomllib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src/codex_desktop_weixin'))
import configure
from fetch_gateway import BINARY_SHA256, RELEASE_TAG, ensure_gateway
from installer import build_hook_command, hooks_feature_enabled, install_hooks, merge_hooks_document
from runtime_io import acquire_file_lock, default_data_dir
from setup_process import PairingGateway, wait_gateway_ready
from setup_diagnostics import InstallFailure, record_failure, run_logged

GATEWAY_RELATIVE = Path('vendor/cc-connect/v1.5.0-desktop-control/bin/cc-connect-desktop-control.exe')
QQ_PLATFORM_URL = 'https://q.qq.com/'
QQ_BOT_DOCS_URL = 'https://bot.q.qq.com/wiki/'


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def setup_state(root: Path, runtime: Path) -> str:
    """Only classify; never print config contents or repair/overwrite an install."""
    settings = runtime / 'settings.json'
    pending = runtime / 'setup-pending.json'
    if settings.exists():
        if pending.exists():
            if configure.pending_phase(root, runtime) == 'binding':
                return 'finalizing'
            raise ValueError('正式配置与未完成绑定同时存在，请检查原安装；未清空或覆盖任何配置。')
        state = json.loads(settings.read_text(encoding='utf-8-sig'))
        if (not isinstance(state, dict) or state.get('mode') != 'cc-connect'
                or Path(state.get('executable', '')).resolve() != (root / GATEWAY_RELATIVE).resolve()
                or Path(state.get('config_path', '')).resolve() != (runtime / 'cc-connect.toml').resolve()
                or not Path(state.get('config_path', '')).is_file()):
            raise ValueError('已有安装的路径或模式与当前目录不同，请按更新说明接续原安装，不要重复绑定。')
        return 'configured'
    if pending.exists():
        phase = configure.pending_phase(root, runtime)
        return {'preparing': 'preparing', 'binding': 'finalizing', 'pairing': 'pairing'}[phase]
    if (runtime / 'cc-connect.toml').exists():
        raise ValueError('发现没有进度记录的配置，已保留；请先核实它属于哪次安装。')
    return 'new'


def launcher_ready(root: Path) -> bool:
    try:
        binary = root / 'bin/CodexQQ助手.exe'
        state = json.loads((root / 'bin/app-runtime.json').read_text(encoding='utf-8-sig'))
        return (state.get('version') == 1
                and Path(state.get('pythonw', '')).resolve() == Path(sys.executable).with_name('pythonw.exe').resolve()
                and state.get('source_sha256') == digest(root / 'tools/ResidentLauncher.cs')
                and state.get('launcher_sha256') == digest(binary))
    except (OSError, ValueError, AttributeError, TypeError):
        return False


def check(root: Path, runtime: Path, codex_home: Path) -> dict:
    """Read-only, compact agent entry; no process launch, directories, downloads or secrets."""
    gateway = root / GATEWAY_RELATIVE
    compiler = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    launcher = launcher_ready(root)
    arch = os.environ.get('PROCESSOR_ARCHITEW6432') or platform.machine()
    try:
        state = setup_state(root, runtime)
        problem = None
    except (ValueError, OSError, KeyError, TypeError, AttributeError):
        state, problem = 'conflict', '现有配置无法安全接续；请查看 docs/troubleshooting.md，保留原文件。'
    try:
        config_ok = (codex_home / 'config.toml').is_file()
        if config_ok:
            tomllib.loads((codex_home / 'config.toml').read_text(encoding='utf-8-sig'))
    except (ValueError, OSError):
        config_ok = False
    verified = gateway.is_file() and digest(gateway) == BINARY_SHA256
    return {'windows_x64': os.name == 'nt' and arch.lower() in ('amd64', 'x86_64'),
            'python_ok': sys.version_info >= (3, 11),
            'pythonw_ok': Path(sys.executable).with_name('pythonw.exe').is_file(),
            'launcher_prerequisite_ok': launcher or compiler.is_file(),
            'codex_config_ok': config_ok, 'setup_state': state, 'problem': problem,
            'gateway': 'verified' if verified else ('other-version' if gateway.exists() else 'missing'),
            'gateway_release': RELEASE_TAG, 'launcher_ready': launcher,
            'source_directory': str(root), 'runtime_directory': str(runtime),
            'next': 'install.cmd' if state != 'conflict' else '检查现有安装，不要清空配置'}


def prepare_files(root: Path, logs: Path | None = None) -> None:
    logs = logs or default_data_dir() / 'logs'
    print('正在准备配套 QQ 网关（已校验的文件会直接复用）……', flush=True)
    try:
        result = ensure_gateway(root / GATEWAY_RELATIVE)
    except Exception as error:
        stage = getattr(error, 'stage', 'QQ 网关产物验证' if isinstance(error, ValueError) else 'QQ 网关准备')
        raise record_failure(logs, stage, error) from None
    print('QQ 网关：' + ('已复用' if result['cached'] else '已下载并通过校验'))
    if not launcher_ready(root):
        run_logged([sys.executable, '-X', 'utf8', str(root / 'tools/build_resident_app.py')],
                   stage='启动器编译与发布', logs=logs)
        if not launcher_ready(root):
            raise record_failure(logs, '启动器产物验证', ValueError('子进程成功退出，但启动入口及构建记录未通过校验。'))
    print('应用启动入口：已就绪')


def print_qq_bot_setup_guide() -> None:
    """Give first-time users the platform steps before asking for secrets."""
    print(f'''
第一次使用 QQ 机器人？先在浏览器完成下面步骤；已有机器人可直接从第 3 步开始：
  1. 打开 QQ 开放平台 {QQ_PLATFORM_URL}，登录或注册并进入控制台。
  2. 选择“创建机器人”，填写机器人名称、简介等资料，完成平台要求的账号/主体认证。
  3. 打开刚创建的机器人，在“开发”→“开发设置”（有的页面称“开发管理”）中：
     - 复制 AppID；
     - 点击“生成/查看”获取 AppSecret。
  4. 在“沙箱配置”或“服务范围/内部体验号码”中加入你自己的 QQ 号，开启私信测试；
     如果平台显示测试成员或消息列表配置，按页面把自己的 QQ 加进去。
  5. 回到这个安装窗口，按提示粘贴 AppID 和 AppSecret，再选择是否使用沙箱。

AppID 是机器人 ID，AppSecret 是机器人密钥；它们都不是普通 QQ 号或 QQ 登录密码。
密钥只在本机的隐藏输入框中填写，不要发给智能体、QQ 或 GitHub。
官方入门说明：{QQ_BOT_DOCS_URL}
''')


def pair(root: Path, runtime: Path, *, ask=input, hidden=getpass.getpass) -> bool:
    state = setup_state(root, runtime)
    if state in ('preparing', 'finalizing'):
        print('正在核对并接续上次中断的配置事务……')
        configure.recover_setup(root, runtime)
        state = setup_state(root, runtime)
    if state == 'configured':
        print('已有 QQ 绑定，直接保留，不再索取密钥。')
        return True
    if state == 'new':
        print_qq_bot_setup_guide()
        app_id = ask('机器人 AppID：').strip()
        secret = hidden('机器人 AppSecret（输入不显示）：')
        sandbox = ask('平台是否使用沙箱？使用沙箱输入 y；否则直接回车：').strip().lower() == 'y'
        cc_data = Path(os.environ.get('LOCALAPPDATA', str(runtime.parent))) / 'CodexCC'
        configure.prepare(root, runtime, cc_data, app_id, secret, sandbox)
        del secret
    else:
        print('发现上次未完成的绑定，继续当前进度，无需再次填写密钥。')
    configure.load_pending(root, runtime)
    logs = runtime / 'logs'
    logs.mkdir(parents=True, exist_ok=True)
    log_path = logs / 'setup-gateway.log'
    openid = ''
    with log_path.open('ab') as log:
        offset = log.tell()
        print('正在连接 QQ，等待 WebSocket READY（最多 45 秒，可按 Ctrl+C 取消）……', flush=True)
        with PairingGateway([str(root / GATEWAY_RELATIVE), '--config', str(runtime / 'cc-connect.toml')],
                            cwd=root, log=log) as child:
            wait_gateway_ready(child, log_path, offset=offset)
            print('\n请用自己的 QQ 私聊机器人，发送“绑定”。此阶段只查询身份，不连接 Codex。')
            print('将机器人返回的 OpenID 填到这里（不是普通 QQ 号）；输入“取消”可保留进度退出。')
            while True:
                code = child.poll()
                if code is not None:
                    raise ValueError(f'配置网关已停止（退出码 {code}）；请在本机检查 {log_path}，不要上传含密钥的配置。')
                candidate = ask('OpenID（还未收到可回车检查）：').strip()
                if candidate == '取消':
                    return False
                if not candidate:
                    print('尚未绑定。日志位置：' + str(log_path) + '；没有事件不能直接判定是 QQ 平台拦截。')
                    continue
                if not re.fullmatch(r'[A-Za-z0-9_-]{8,128}', candidate) or candidate.isdecimal():
                    print('请填写机器人返回的 OpenID，不是 QQ 号码。')
                    continue
                if ask('只允许这个 QQ 身份控制电脑，确认请输入 yes：').strip() != 'yes':
                    print('已取消，进度已保留。')
                    return False
                openid = candidate
                break
    # The temporary identity gateway must be gone before replacing its config.
    configure.bind(root, runtime, openid)
    print('QQ 绑定已保存。')
    return True


def start_application(codex_home: Path, runtime: Path) -> bool:
    from launcher import cc_connect_api_status, read_cc_connect_pid, read_monitor_pid
    from resident_app import launch_app
    if not read_monitor_pid(runtime, require_ready=True):
        launch_app(codex_home, runtime)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if read_monitor_pid(runtime, require_ready=True) and read_cc_connect_pid(runtime) and cc_connect_api_status(runtime) == 'ready':
            return True
        time.sleep(0.25)
    return False


def hooks_current(root: Path, codex_home: Path) -> bool:
    config = (codex_home / 'config.toml').read_text(encoding='utf-8-sig')
    path = codex_home / 'hooks.json'
    document = json.loads(path.read_text(encoding='utf-8-sig')) if path.is_file() else {}
    if not isinstance(document, dict):
        raise ValueError('现有 hooks.json 格式不正确，已保留；请先检查。')
    command = build_hook_command(root / 'src/codex_desktop_weixin/cli.py')
    return hooks_feature_enabled(config) and document == merge_hooks_document(document, command)


def finish(root: Path, runtime: Path, codex_home: Path, *, ask=input) -> bool:
    # The independent-mode barrier must precede installing hooks. Never launch
    # a CLI agent or close Desktop to make setup appear successful.
    if not start_application(codex_home, runtime):
        print('绑定已保留，但助手尚未就绪。请看本机日志；不要重复绑定或删除配置。')
        return False
    print('独立助手与本地 QQ 网关已就绪（不代表手机已收到消息）。')
    if not hooks_current(root, codex_home):
        print('需要合并提醒钩子，并仅将 Codex 配置的 [features] hooks 设为 true；其他配置保持原样。')
        if ask('允许这两项修改请输入 yes；否则回车稍后继续：').strip() != 'yes':
            print('尚未安装钩子，通知链路待完成。下次运行 install.cmd 可继续。')
            return False
        install_hooks(codex_home, root / 'src/codex_desktop_weixin/cli.py', data_dir=runtime)
    print('\n电脑端准备完成，未开启登录自启，也未关闭或重启 Codex。')
    print('请在 Codex 中检查并信任本应用钩子；如提示重启，请正常重启后再验收。')
    print('手机验收：发“帮助”→“对话”→引用列表选序号→引用回执发“设置”。')
    print('再用测试对话做一次任务，确认 QQ 聊天页面能看见完成正文。')
    print('开启/退出：bin/CodexQQ助手.exe；自启可在托盘右键菜单选择。')
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description='Codex-QQ 一次性安装向导；可中断后重新运行，不覆盖已有绑定')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--check', action='store_true', help='仅读取环境和进度，输出简短 JSON，零写入')
    group.add_argument('--prepare', action='store_true', help='只准备网关和启动入口，不询问密钥、不改 Codex、不启动服务')
    args = parser.parse_args()
    runtime = default_data_dir().resolve()
    codex_home = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))).resolve()
    try:
        status = check(ROOT, runtime, codex_home)
        if args.check:
            print(json.dumps(status, ensure_ascii=False, indent=2))
            return 0 if status['setup_state'] != 'conflict' else 1
        if not all(status[key] for key in ('windows_x64', 'python_ok', 'pythonw_ok', 'launcher_prerequisite_ok')):
            raise ValueError('需要 Windows x64、Python 3.11+（含 pythonw）与 Windows 自带 .NET 编译器。请运行 --check 查看缺项。')
        if status['setup_state'] == 'conflict':
            raise ValueError(status['problem'])
        if not args.prepare and not sys.stdin.isatty():
            raise ValueError('请在可输入的本机终端运行 install.cmd；智能体可先运行 --prepare，不要通过聊天传入密钥。')
        if not args.prepare and not status['codex_config_ok']:
            raise ValueError('未找到可解析的 Codex 配置，请先正常打开 Codex Desktop，或核实 CODEX_HOME；未创建替代配置。')
        lock = acquire_file_lock(runtime / 'state/setup.lock')
        if lock is None:
            raise ValueError('已有安装向导在运行，请回到原窗口继续。')
        try:
            prepare_files(ROOT, runtime / 'logs')
            if args.prepare:
                if status['setup_state'] == 'configured':
                    print('程序文件已就绪，已有 QQ 绑定保持不变；可运行 install.cmd 接续启动和钩子确认，无需重新绑定。')
                else:
                    print('程序文件已就绪。请让用户在本机终端运行 install.cmd 接续 QQ 绑定和钩子确认。')
                return 0
            if not pair(ROOT, runtime):
                return 0
            return 0 if finish(ROOT, runtime, codex_home) else 1
        finally:
            os.close(lock[0])
    except (KeyboardInterrupt, EOFError):
        print('\n已停止；已有配置和未完成的绑定进度均保留，下次运行 install.cmd 继续。')
        return 1
    except InstallFailure as error:
        print('安装未完成：' + str(error) + '\n配置与绑定进度已保留。', file=sys.stderr)
        return 1
    except Exception as error:
        # Parser/OS/HTTP bodies may carry config lines. Only our exact ValueError
        # messages are safe to show; do not dump a subprocess or a traceback.
        message = str(error) if type(error) is ValueError else type(error).__name__
        print('安装未完成：' + message + '\n未清空配置或绑定。排查见 docs/troubleshooting.md。', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
