// Ask the existing Explorer desktop to start pythonw, outside Codex's job tree.
using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Web.Script.Serialization;
using System.Collections.Generic;
using System.Runtime.InteropServices;

class ResidentLauncher {
    [DllImport("user32.dll", CharSet=CharSet.Unicode)]
    static extern int MessageBox(IntPtr window, string text, string title, uint flags);

    static string Quote(string text) {
        StringBuilder result = new StringBuilder("\"");
        int slashes = 0;
        foreach (char ch in text) {
            if (ch == '\\') { slashes++; continue; }
            if (ch == '"') result.Append('\\', slashes * 2 + 1);
            else result.Append('\\', slashes);
            slashes = 0;
            result.Append(ch);
        }
        result.Append('\\', slashes * 2);
        return result.Append('"').ToString();
    }

    [STAThread]
    static int Main(string[] args) {
        try {
            string bin = Path.GetDirectoryName(System.Reflection.Assembly.GetExecutingAssembly().Location);
            string root = Path.GetFullPath(Path.Combine(bin, ".."));
            string script = Path.Combine(root, "src", "codex_desktop_weixin", "cli.py");
            string configPath = Path.Combine(bin, "app-runtime.json");
            if (!File.Exists(configPath) || new FileInfo(configPath).Length > 4096)
                throw new InvalidOperationException("runtime-config-missing");
            var config = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(File.ReadAllText(configPath, Encoding.UTF8));
            string python = (string)config["pythonw"];
            if (Convert.ToInt32(config["version"]) != 1 || !Path.IsPathRooted(python) || !File.Exists(python) ||
                !String.Equals(Path.GetFileName(python), "pythonw.exe", StringComparison.OrdinalIgnoreCase) || !File.Exists(script))
                throw new InvalidOperationException("runtime-path-invalid");
            string parameters = "-X utf8 " + Quote(script) + " app --brokered";
            foreach (string argument in args) parameters += " " + Quote(argument);
            // CREATE_NO_WINDOW/detached console does not detach a Windows job.
            // Use the actual desktop's COM object, not a new Shell.Application
            // executing in this process. Never fall back to Process.Start here.
            object windows = null, desktop = null, document = null, shell = null;
            try {
                windows = Activator.CreateInstance(Type.GetTypeFromCLSID(
                    new Guid("9BA05972-F6A8-11CF-A442-00A0C90A8F39")));
                int window = 0;
                desktop = ((dynamic)windows).FindWindowSW(0, 0, 8, ref window, 1);
                if (desktop == null || window == 0)
                    throw new InvalidOperationException("windows-desktop-unavailable");
                document = ((dynamic)desktop).Document;
                shell = ((dynamic)document).Application;
                ((dynamic)shell).ShellExecute(python, parameters, root, "open", 0);
            } finally {
                foreach (object value in new object[] { shell, document, desktop, windows })
                    if (value != null && Marshal.IsComObject(value)) Marshal.ReleaseComObject(value);
            }
            return 0;
        } catch (Exception error) {
            MessageBox(IntPtr.Zero, "无法独立启动 Codex QQ 助手。请在 Windows 桌面就绪后重试，并检查应用目录和本机 Python。\n错误类型：" + error.GetType().Name,
                       "Codex QQ 助手", 0x10);
            return 1;
        }
    }
}
