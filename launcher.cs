// Тонкий запускатор rdesk. Компилируется csc.exe, внешних зависимостей нет.
using System;
using System.Diagnostics;
using System.IO;
using System.Text;
using System.Windows.Forms;

[assembly: System.Reflection.AssemblyTitle("@TITLE@")]
[assembly: System.Reflection.AssemblyProduct("rdesk")]
[assembly: System.Reflection.AssemblyVersion("2.0.0.0")]

internal static class Program
{
    [STAThread]
    private static int Main(string[] args)
    {
        string baseDir = AppDomain.CurrentDomain.BaseDirectory.TrimEnd('\\');
        string python = Path.Combine(baseDir, Path.Combine("runtime", "pythonw.exe"));
        string script = Path.Combine(baseDir, Path.Combine("app", "@ENTRY@"));
        string log = Path.Combine(Path.GetTempPath(), "rdesk-@ROLE@-launch.log");
        if (!File.Exists(python) || !File.Exists(script))
        {
            string message = "Не найдены файлы rdesk:\n" + python + "\n" + script;
            try { File.AppendAllText(log, message + "\r\n"); } catch (Exception) { }
            MessageBox.Show(message, "@TITLE@", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
        StringBuilder command = new StringBuilder();
        command.Append('"').Append(script).Append('"');
        foreach (string arg in args)
        {
            command.Append(' ');
            if (arg.IndexOf(' ') >= 0)
            {
                command.Append('"').Append(arg.Replace("\"", "\\\"")).Append('"');
            }
            else
            {
                command.Append(arg);
            }
        }
        ProcessStartInfo info = new ProcessStartInfo(python, command.ToString());
        info.WorkingDirectory = Path.Combine(baseDir, "app");
        info.UseShellExecute = false;
        info.CreateNoWindow = true;
        info.EnvironmentVariables["PYTHONUTF8"] = "1";
        try
        {
            Process.Start(info);
            return 0;
        }
        catch (Exception error)
        {
            string message = "Не удалось запустить rdesk:\n" + error.Message;
            try { File.AppendAllText(log, message + "\r\n"); } catch (Exception) { }
            MessageBox.Show(message, "@TITLE@", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
}
