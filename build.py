"""Сборка rdesk 2.0: серверный установщик EXE и MSI для host/viewer.

Запуск:
    python packaging/build.py            — всё
    python packaging/build.py server     — только серверный EXE
    python packaging/build.py host       — только host MSI
    python packaging/build.py viewer     — только viewer MSI
"""

from __future__ import annotations

import os
import py_compile
import shutil
import subprocess
import sys
import uuid
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BUILD = os.path.join(ROOT, "build")
DIST = os.path.join(ROOT, "dist")
PY_ROOT = sys.base_prefix
WINDIR = os.environ.get("SystemRoot", r"C:\Windows")
WIX = os.path.join(ROOT, "tools", "wix314")
CSC_CANDIDATES = [
    os.path.join(WINDIR, "Microsoft.NET", "Framework64", "v4.0.30319", "csc.exe"),
    os.path.join(WINDIR, "Microsoft.NET", "Framework", "v4.0.30319", "csc.exe"),
]
VERSION = "2.0.0"

ROOT_FILES = ["python.exe", "pythonw.exe", "python3.dll", "python312.dll",
              "vcruntime140.dll", "vcruntime140_1.dll", "LICENSE.txt"]
DLL_EXCLUDE = {"sqlite3.dll", "_sqlite3.pyd", "libssl-3.dll", "_ssl.pyd"}
LIB_EXCLUDE = {"test", "tests", "site-packages", "idlelib", "ensurepip", "venv",
               "lib2to3", "turtledemo", "distutils", "__pycache__", "antigravity.py",
               "this.py", "turtle.py"}

ROLES = {
    "host": {
        "title": "rdesk host",
        "entry": "host.py",
        "exe": "rdesk-host.exe",
        "upgrade": "{A1B2C3D4-1111-4A2B-9C3D-100000000001}",
        "shortcut_guid": "{A1B2C3D4-1111-4A2B-9C3D-100000000003}",
        "runkey_guid": "{A1B2C3D4-1111-4A2B-9C3D-100000000005}",
        "desktop": False,
        "autostart": True,
    },
    "viewer": {
        "title": "rdesk viewer",
        "entry": "viewer.py",
        "exe": "rdesk-viewer.exe",
        "upgrade": "{A1B2C3D4-1111-4A2B-9C3D-100000000002}",
        "shortcut_guid": "{A1B2C3D4-1111-4A2B-9C3D-100000000004}",
        "runkey_guid": "",
        "desktop": True,
        "autostart": False,
    },
    "server": {
        "title": "rdesk server",
        "entry": "server.py",
        "exe": "rdesk-server.exe",
        "upgrade": "",
        "shortcut_guid": "",
        "runkey_guid": "",
        "desktop": True,
        "autostart": True,
    },
}


def say(message: str) -> None:
    print(message, flush=True)


def build_env() -> dict:
    env = os.environ.copy()
    temp = os.path.join(BUILD, "temp")
    os.makedirs(temp, exist_ok=True)
    env["TEMP"] = temp
    env["TMP"] = temp
    return env


def find_csc() -> str | None:
    for path in CSC_CANDIDATES:
        if os.path.exists(path):
            return path
    return None


def ignore_factory(excluded: set[str]):
    def _ignore(directory: str, names: list[str]) -> set[str]:
        result = set()
        for name in names:
            if name in excluded or name.endswith(".pyc") or name == "__pycache__":
                result.add(name)
        return result

    return _ignore


def copy_tree(source: str, target: str, exclude: set[str]) -> None:
    if os.path.isdir(target):
        shutil.rmtree(target)
    shutil.copytree(source, target, ignore=ignore_factory(exclude))


def stage_runtime(dest: str) -> None:
    os.makedirs(dest, exist_ok=True)
    for name in ROOT_FILES:
        source = os.path.join(PY_ROOT, name)
        if os.path.exists(source):
            shutil.copy2(source, os.path.join(dest, name))
    copy_tree(os.path.join(PY_ROOT, "DLLs"), os.path.join(dest, "DLLs"), DLL_EXCLUDE)
    copy_tree(os.path.join(PY_ROOT, "Lib"), os.path.join(dest, "Lib"), LIB_EXCLUDE)
    copy_tree(os.path.join(PY_ROOT, "tcl"), os.path.join(dest, "tcl"), set())


def compile_launcher(role: str, csc: str) -> str:
    info = ROLES[role]
    work = os.path.join(BUILD, "launcher")
    os.makedirs(work, exist_ok=True)
    source = os.path.join(work, f"{role}.cs")
    with open(os.path.join(HERE, "launcher.cs"), "r", encoding="utf-8") as fh:
        code = fh.read()
    code = (code.replace("@ROLE@", role)
                .replace("@TITLE@", info["title"])
                .replace("@ENTRY@", info["entry"]))
    with open(source, "w", encoding="utf-8-sig") as fh:
        fh.write(code)
    target = os.path.join(work, info["exe"])
    icon = os.path.join(ROOT, "assets", "rdesk.ico")
    command = [
        csc, "/nologo", "/target:winexe", "/optimize+", "/platform:anycpu",
        f"/win32icon:{icon}", f"/out:{target}", source,
        "/r:System.Windows.Forms.dll", "/r:System.Drawing.dll",
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=120, env=build_env())
    if result.returncode != 0 or not os.path.exists(target):
        say(result.stdout[-3000:])
        say(result.stderr[-3000:])
        raise SystemExit(f"не удалось собрать {info['exe']}")
    return target


def stage_app(role: str, dest: str, launcher: str) -> None:
    info = ROLES[role]
    os.makedirs(dest, exist_ok=True)
    copy_tree(os.path.join(ROOT, "rdesk"), os.path.join(dest, "rdesk"), {"__pycache__"})
    shutil.copy2(os.path.join(ROOT, info["entry"]), os.path.join(dest, info["entry"]))
    copy_tree(os.path.join(ROOT, "assets"), os.path.join(dest, "assets"),
              {"__pycache__", "make_icon.py"})
    shutil.copy2(os.path.join(HERE, "setup.py"), os.path.join(dest, "setup.py"))
    shutil.copy2(os.path.join(ROOT, "README.md"), os.path.join(dest, "README.md"))
    shots = os.path.join(dest, "tests", "artifacts")
    os.makedirs(shots, exist_ok=True)
    for name in ("server-ui.png", "viewer-ui.png", "host-ui.png"):
        source = os.path.join(ROOT, "tests", "artifacts", name)
        if os.path.exists(source):
            shutil.copy2(source, os.path.join(shots, name))
    # Launcher лежит в корне установки, рядом с runtime и app.
    shutil.copy2(launcher, os.path.join(os.path.dirname(dest), info["exe"]))
    if role == "server":
        text = (
            "@echo off\r\n"
            "chcp 65001 >nul\r\n"
            "title rdesk server — удаление\r\n"
            "set \"DEST=%~dp0\"\r\n"
            "if \"%DEST:~-1%\"==\"\\\" set \"DEST=%DEST:~0,-1%\"\r\n"
            "echo Удаление rdesk server из %DEST%\r\n"
            "\"%DEST%\\runtime\\python.exe\" \"%DEST%\\app\\setup.py\" uninstall "
            "--role server --dest \"%DEST%\"\r\n"
            "pause\r\n"
        )
        with open(os.path.join(dest, "uninstall.cmd"), "w", encoding="utf-8",
                  newline="\r\n") as fh:
            fh.write(text)


def payload_zip(payload: str, archive: str) -> None:
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for base, _dirs, files in os.walk(payload):
            for name in files:
                full = os.path.join(base, name)
                relative = os.path.relpath(full, payload).replace("\\", "/")
                zf.write(full, relative)


def directory_size(path: str) -> int:
    total = 0
    for base, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(base, name))
            except OSError:
                pass
    return total


def stage_payload(role: str, csc: str) -> tuple[str, str]:
    info = ROLES[role]
    work = os.path.join(BUILD, role)
    if os.path.isdir(work):
        shutil.rmtree(work)
    payload = os.path.join(work, "payload")
    os.makedirs(payload, exist_ok=True)
    say(f"[{role}] python runtime…")
    stage_runtime(os.path.join(payload, "runtime"))
    say(f"[{role}] launcher…")
    launcher = compile_launcher(role, csc)
    say(f"[{role}] файлы программы…")
    stage_app(role, os.path.join(payload, "app"), launcher)
    say(f"[{role}] размер: {directory_size(payload) / 1024 / 1024:.1f} МБ")
    return work, payload


# --------------------------------------------------------------------------
# серверный EXE
# --------------------------------------------------------------------------

def build_server(csc: str) -> str:
    work, payload = stage_payload("server", csc)
    archive = os.path.join(work, "payload.zip")
    say("[server] упаковка…")
    payload_zip(payload, archive)
    source = os.path.join(work, "installer.cs")
    shutil.copy2(os.path.join(HERE, "installer.cs"), source)
    os.makedirs(DIST, exist_ok=True)
    target = os.path.join(DIST, f"rdesk-server-{VERSION}-setup.exe")
    icon = os.path.join(ROOT, "assets", "rdesk.ico")
    command = [
        csc, "/nologo", "/target:winexe", "/optimize+", "/platform:anycpu",
        f"/win32icon:{icon}", f"/resource:{archive},payload.zip",
        "/r:System.Windows.Forms.dll", "/r:System.Drawing.dll",
        f"/out:{target}", source,
    ]
    say("[server] компиляция установщика…")
    result = subprocess.run(command, capture_output=True, text=True, timeout=300, env=build_env())
    if result.returncode != 0 or not os.path.exists(target):
        say(result.stdout[-4000:])
        say(result.stderr[-4000:])
        raise SystemExit("csc.exe не смог собрать серверный установщик")
    say(f"[server] готово: {target} ({os.path.getsize(target) / 1024 / 1024:.1f} МБ)")
    return target


# --------------------------------------------------------------------------
# MSI
# --------------------------------------------------------------------------

def make_license(path: str) -> None:
    text = (
        "rdesk — удалённый доступ\n\n"
        "Программа предназначена для администрирования собственных компьютеров "
        "и компьютеров, доступ к которым получен с согласия владельца.\n\n"
        "Устанавливая rdesk host, вы подтверждаете, что имеете право управлять "
        "этим компьютером. Скрытая установка на чужие компьютеры запрещена.\n\n"
        "Сервер и каналы rdesk используют пароль и шифрование, но требуют "
        "ответственного обращения: храните пароль сервера в тайне и не открывайте "
        "порт без необходимости.\n\n"
        "Программа поставляется «как есть», без гарантий. Автор не несёт "
        "ответственности за последствия использования.\n"
    )

    def escape(value: str) -> str:
        result = []
        for char in value:
            if char == "\\":
                result.append("\\\\")
            elif char == "{":
                result.append("\\{")
            elif char == "}":
                result.append("\\}")
            elif char == "\n":
                result.append("\\par\n")
            elif ord(char) < 128:
                result.append(char)
            else:
                result.append(f"\\u{ord(char)}?")
        return "".join(result)

    rtf = (
        "{\\rtf1\\ansi\\ansicpg1251\\deff0{\\fonttbl{\\f0\\fswiss Segoe UI;}}"
        "\\fs20\\f0 " + escape(text) + "}"
    )
    with open(path, "w", encoding="ascii") as fh:
        fh.write(rtf)


def build_msi(role: str, csc: str) -> str:
    info = ROLES[role]
    work, payload = stage_payload(role, csc)
    heat = os.path.join(WIX, "heat.exe")
    candle = os.path.join(WIX, "candle.exe")
    light = os.path.join(WIX, "light.exe")
    for tool in (heat, candle, light):
        if not os.path.exists(tool):
            raise SystemExit(f"нет {tool}; распакуйте WiX 3.14 в tools/wix314")
    app_files = os.path.join(work, "AppFiles.wxs")
    say(f"[{role}] heat: инвентаризация файлов…")
    command = [
        heat, "dir", payload, "-cg", "AppFiles", "-dr", "INSTALLDIR",
        "-ag", "-g1", "-srd", "-sreg", "-scom", "-sfrag",
        "-var", "var.AppSource", "-platform", "x64", "-nologo",
        "-out", app_files,
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=600, env=build_env())
    if result.returncode != 0:
        say(result.stdout[-4000:])
        say(result.stderr[-4000:])
        raise SystemExit("heat не смог собрать список файлов")
    say(f"[{role}] файлов: {directory_size(payload) / 1024 / 1024:.1f} МБ")

    license_path = os.path.join(BUILD, "license.rtf")
    make_license(license_path)
    icon = os.path.join(ROOT, "assets", "rdesk.ico")
    desktop = ""
    if info["desktop"]:
        desktop = (
            f'<Shortcut Id="DesktopShortcut" Directory="DesktopFolder" '
            f'Name="{info["title"]}" Description="Удалённый доступ rdesk" '
            f'Target="[INSTALLDIR]{info["exe"]}" WorkingDirectory="INSTALLDIR" '
            f'Icon="RdeskIcon"/>'
        )
    runkey_component = ""
    runkey_ref = ""
    if info["autostart"]:
        runkey_component = f"""
      <Component Id="RunKey" Guid="{info['runkey_guid']}">
        <RegistryValue Root="HKCU" Key="Software\\Microsoft\\Windows\\CurrentVersion\\Run"
          Name="{info['title']}" Type="string"
          Value="&quot;[INSTALLDIR]{info['exe']}&quot; --tray" KeyPath="yes"/>
      </Component>"""
        runkey_ref = '<ComponentRef Id="RunKey"/>'

    product = f"""<?xml version="1.0" encoding="utf-8"?>
<Wix xmlns="http://schemas.microsoft.com/wix/2006/wi"
     xmlns:util="http://schemas.microsoft.com/wix/UtilExtension">
  <Product Id="*" Name="{info['title']}" Language="1049" Version="{VERSION}"
           Manufacturer="rdesk" UpgradeCode="{info['upgrade']}">
    <Package Id="*" InstallerVersion="500" Compressed="yes"
             InstallScope="perUser" InstallPrivileges="limited"
             Platform="x64" SummaryCodepage="1251"/>
    <MajorUpgrade DowngradeErrorMessage="Установлена более новая версия rdesk."
                  Schedule="afterInstallInitialize"/>
    <MediaTemplate EmbedCab="yes" CompressionLevel="high"/>
    <Property Id="ARPPRODUCTICON" Value="RdeskIcon"/>
    <Property Id="ARPCOMMENTS"
              Value="rdesk — удалённый доступ. Используйте только на своих компьютерах."/>
    <Icon Id="RdeskIcon" SourceFile="{icon}"/>
    <WixVariable Id="WixUILicenseRtf" Value="{license_path}"/>
    <UIRef Id="WixUI_Minimal"/>
    <Directory Id="TARGETDIR" Name="SourceDir">
      <Directory Id="LocalAppDataFolder">
        <Directory Id="RdeskRoot" Name="rdesk">
          <Directory Id="INSTALLDIR" Name="{role}"/>
        </Directory>
      </Directory>
      <Directory Id="ProgramMenuFolder">
        <Directory Id="RdeskMenu" Name="rdesk"/>
      </Directory>
      <Directory Id="DesktopFolder"/>
    </Directory>
    <DirectoryRef Id="INSTALLDIR">
      <Component Id="Shortcuts" Guid="{info['shortcut_guid']}">
        <Shortcut Id="StartMenuShortcut" Directory="RdeskMenu" Name="{info['title']}"
                  Description="rdesk — удалённый доступ"
                  Target="[INSTALLDIR]{info['exe']}" WorkingDirectory="INSTALLDIR"
                  Icon="RdeskIcon"/>
        {desktop}
        <RemoveFolder Id="RemoveInstallDir" Directory="INSTALLDIR" On="uninstall"/>
        <RemoveFolder Id="RemoveRdeskMenu" Directory="RdeskMenu" On="uninstall"/>
        <RegistryValue Root="HKCU" Key="Software\\rdesk\\{role}" Name="installed"
                       Type="integer" Value="1" KeyPath="yes"/>
      </Component>{runkey_component}
    </DirectoryRef>
    <Feature Id="MainFeature" Title="{info['title']}" Level="1">
      <ComponentGroupRef Id="AppFiles"/>
      <ComponentRef Id="Shortcuts"/>
      {runkey_ref}
    </Feature>
    <CustomAction Id="SetStopCmd" Property="StopApp"
      Value="&quot;[LocalAppDataFolder]rdesk\\{role}\\runtime\\python.exe&quot; &quot;[LocalAppDataFolder]rdesk\\{role}\\app\\setup.py&quot; stop --role {role} --dest &quot;[LocalAppDataFolder]rdesk\\{role}&quot;"
      Execute="immediate"/>
    <CustomAction Id="StopApp" BinaryKey="WixCA" DllEntry="WixQuietExec"
      Execute="deferred" Impersonate="yes" Return="ignore"/>
    <CustomAction Id="SetLaunchTarget" Property="WixShellExecTarget"
      Value="[INSTALLDIR]{info['exe']}" Execute="immediate"/>
    <CustomAction Id="LaunchApp" BinaryKey="WixCA" DllEntry="WixShellExec"
      Impersonate="yes"/>
    <CustomAction Id="SetCleanupCmd" Property="WixQuietExecCmdLine"
      Value="&quot;[%ComSpec]&quot; /c rmdir /s /q &quot;[INSTALLDIR]&quot;"
      Execute="immediate"/>
    <CustomAction Id="CleanupFiles" BinaryKey="WixCA" DllEntry="WixQuietExec"
      Execute="immediate" Return="ignore"/>
    <InstallExecuteSequence>
      <Custom Action="SetStopCmd" Before="InstallInitialize">NOT Installed OR REMOVE~="ALL" OR UPGRADINGPRODUCTCODE</Custom>
      <Custom Action="StopApp" Before="RemoveFiles">NOT Installed OR REMOVE~="ALL" OR UPGRADINGPRODUCTCODE</Custom>
      <Custom Action="SetLaunchTarget" Before="LaunchApp">UILevel &gt;= 3</Custom>
      <Custom Action="LaunchApp" After="InstallFinalize">UILevel &gt;= 3</Custom>
      <Custom Action="SetCleanupCmd" Before="CleanupFiles">REMOVE~="ALL" AND NOT UPGRADINGPRODUCTCODE</Custom>
      <Custom Action="CleanupFiles" After="InstallFinalize">REMOVE~="ALL" AND NOT UPGRADINGPRODUCTCODE</Custom>
    </InstallExecuteSequence>
  </Product>
</Wix>
"""
    product_path = os.path.join(work, "product.wxs")
    with open(product_path, "w", encoding="utf-8") as fh:
        fh.write(product)

    say(f"[{role}] candle…")
    command = [
        candle, "-nologo", "-arch", "x64", f"-dAppSource={payload}",
        "-ext", os.path.join(WIX, "WixUIExtension.dll"),
        "-ext", os.path.join(WIX, "WixUtilExtension.dll"),
        "-out", work + os.sep, app_files, product_path,
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=600, env=build_env())
    if result.returncode != 0:
        say(result.stdout[-5000:])
        say(result.stderr[-5000:])
        raise SystemExit("candle не смог скомпилировать MSI")
    say(f"[{role}] light…")
    target = os.path.join(DIST, f"rdesk-{role}-{VERSION}.msi")
    wxl = os.path.join(WIX, "WixUI_ru-ru.wxl")
    command = [
        light, "-nologo", "-sval", "-ext", os.path.join(WIX, "WixUIExtension.dll"),
        "-ext", os.path.join(WIX, "WixUtilExtension.dll"),
        "-cultures:ru-ru", "-loc", wxl,
        "-out", target,
        os.path.join(work, "AppFiles.wixobj"),
        os.path.join(work, "product.wixobj"),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=600, env=build_env())
    if result.returncode != 0:
        say(result.stdout[-6000:])
        say(result.stderr[-6000:])
        raise SystemExit("light не смог собрать MSI")
    say(f"[{role}] готово: {target} ({os.path.getsize(target) / 1024 / 1024:.1f} МБ)")
    return target


def build_msi_roles(roles: list[str], csc: str) -> list[str]:
    outputs = []
    for role in roles:
        outputs.append(build_msi(role, csc))
    return outputs


def main(argv: list[str]) -> int:
    csc = find_csc()
    if csc is None:
        say("Не найден csc.exe (.NET Framework 4.x).")
        return 1
    requested = argv[1:] or ["server", "host", "viewer"]
    outputs: list[str] = []
    if "server" in requested:
        outputs.append(build_server(csc))
    msi_roles = [role for role in ("host", "viewer") if role in requested]
    if msi_roles:
        outputs.extend(build_msi_roles(msi_roles, csc))
    say("\nСобрано:")
    for path in outputs:
        say(f"  {path}  ({os.path.getsize(path) / 1024 / 1024:.1f} МБ)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
