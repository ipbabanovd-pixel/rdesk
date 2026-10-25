// Установщик rdesk server: распаковывает встроенный payload.zip и запускает setup.py.
// Компилируется csc.exe из состава Windows, внешних зависимостей нет.
using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Windows.Forms;

[assembly: AssemblyTitle("rdesk server installer")]
[assembly: AssemblyProduct("rdesk")]
[assembly: AssemblyVersion("2.0.0.0")]

internal static class Program
{
    private const string Role = "server";
    private const string Title = "rdesk server";
    private static readonly bool Quiet = Environment.GetEnvironmentVariable("RDESK_NOMSG") == "1";
    private static readonly string LogPath =
        Path.Combine(Path.GetTempPath(), "rdesk-server-setup.log");

    private static void Log(string message)
    {
        try { File.AppendAllText(LogPath, message + "\r\n"); } catch (Exception) { }
    }

    [STAThread]
    private static int Main()
    {
        Application.EnableVisualStyles();
        try { File.Delete(LogPath); } catch (Exception) { }
        Log("=== " + Title + ": установка ===");
        if (!Environment.Is64BitOperatingSystem)
        {
            Fail("rdesk требует 64-битную Windows.");
            return 1;
        }
        string tempZip = Path.Combine(Path.GetTempPath(), "rdesk-server-payload.zip");
        string root = Environment.GetEnvironmentVariable("RDESK_ROOT");
        if (string.IsNullOrEmpty(root))
        {
            root = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "rdesk");
        }
        string dest = Path.Combine(root, Role);
        Log("папка установки: " + dest);

        InstallWindow window = new InstallWindow(Title);
        if (!Quiet)
        {
            window.Show();
            Application.DoEvents();
        }

        try
        {
            window.Step("Остановка предыдущей версии…");
            StopPrevious(dest);

            window.Step("Распаковка встроенных файлов…");
            if (!ExtractPayload(tempZip))
            {
                window.Close();
                Fail("Не удалось распаковать встроенный архив.");
                return 1;
            }

            Directory.CreateDirectory(dest);
            string output;
            int code = RunToLog("tar.exe", "-xf \"" + tempZip + "\" -C \"" + dest + "\"",
                                Path.Combine(Path.GetTempPath(), "rdesk-server-tar.log"), out output);
            if (code != 0)
            {
                // В старых Windows Server может не быть tar.exe — используем PowerShell.
                string ps = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command " +
                            "\"Expand-Archive -LiteralPath '" + tempZip.Replace("'", "''") +
                            "' -DestinationPath '" + dest.Replace("'", "''") + "' -Force\"";
                code = RunToLog("powershell.exe", ps,
                                Path.Combine(Path.GetTempPath(), "rdesk-server-unzip.log"),
                                out output);
            }
            if (code != 0)
            {
                window.Close();
                Fail("Не удалось распаковать файлы установки (код " + code + ").\n" + Last(output));
                return 1;
            }

            string python = Path.Combine(dest, Path.Combine("runtime", "python.exe"));
            string setup = Path.Combine(dest, Path.Combine("app", "setup.py"));
            if (!File.Exists(python) || !File.Exists(setup))
            {
                window.Close();
                Fail("В архиве установки нет нужных файлов.");
                return 1;
            }

            window.Step("Настройка ярлыков и автозапуска…");
            string setupLog = Path.Combine(Path.GetTempPath(), "rdesk-server-install.log");
            code = RunToLog(python, "\"" + setup + "\" install --role " + Role +
                                    " --dest \"" + dest + "\" --no-message", setupLog, out output);
            if (code != 0)
            {
                window.Close();
                Fail("Установка завершилась с ошибкой.\n" + Last(output));
                return 1;
            }

            window.Close();
            Done(dest);
            return 0;
        }
        catch (Exception error)
        {
            window.Close();
            Fail("Ошибка установки:\n" + error.Message);
            return 1;
        }
        finally
        {
            try { File.Delete(tempZip); } catch (Exception) { }
        }
    }

    private static void StopPrevious(string dest)
    {
        try
        {
            string python = Path.Combine(dest, Path.Combine("runtime", "python.exe"));
            string setup = Path.Combine(dest, Path.Combine("app", "setup.py"));
            if (!File.Exists(python) || !File.Exists(setup))
            {
                return;
            }
            int code;
            Run(python, "\"" + setup + "\" stop --role " + Role + " --dest \"" + dest + "\"",
                out code);
            Log("остановка предыдущей версии: " + code);
        }
        catch (Exception error)
        {
            Log("не удалось остановить предыдущую версию: " + error.Message);
        }
    }

    private static bool ExtractPayload(string tempZip)
    {
        try
        {
            using (Stream stream = Assembly.GetExecutingAssembly().GetManifestResourceStream("payload.zip"))
            {
                if (stream == null)
                {
                    return false;
                }
                using (FileStream file = File.Create(tempZip))
                {
                    stream.CopyTo(file);
                }
            }
            return true;
        }
        catch (Exception)
        {
            return false;
        }
    }

    private static string Run(string exe, string arguments, out int code)
    {
        ProcessStartInfo info = new ProcessStartInfo(exe, arguments);
        info.UseShellExecute = false;
        info.CreateNoWindow = true;
        info.RedirectStandardOutput = true;
        info.RedirectStandardError = true;
        using (Process process = Process.Start(info))
        {
            string output = process.StandardOutput.ReadToEnd() + process.StandardError.ReadToEnd();
            process.WaitForExit();
            code = process.ExitCode;
            return output;
        }
    }

    private static int RunToLog(string exe, string arguments, string logFile, out string output)
    {
        string command = "\"\"" + exe + "\" " + arguments + " > \"" + logFile + "\" 2>&1\"";
        ProcessStartInfo info = new ProcessStartInfo("cmd.exe", "/c " + command);
        info.UseShellExecute = false;
        info.CreateNoWindow = true;
        int code;
        using (Process process = Process.Start(info))
        {
            process.WaitForExit();
            code = process.ExitCode;
        }
        try
        {
            output = File.Exists(logFile) ? File.ReadAllText(logFile) : "";
        }
        catch (Exception)
        {
            output = "";
        }
        return code;
    }

    private static string Last(string text)
    {
        if (string.IsNullOrEmpty(text))
        {
            return "";
        }
        text = text.Trim();
        return text.Length > 1200 ? text.Substring(text.Length - 1200) : text;
    }

    private static void Fail(string message)
    {
        Log("ОШИБКА: " + message);
        if (!Quiet)
        {
            MessageBox.Show(message, Title, MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }

    private static void Done(string dest)
    {
        string text =
            "rdesk server установлен.\n\n" +
            "Открывается окно сервера. В нём показан пароль — скопируйте его и укажите " +
            "при установке rdesk host и при входе во вьювере.\n\n" +
            "Порт по умолчанию: 7443 (TCP, входящий только на этом сервере).\n" +
            "Для доступа из интернета откройте порт в брандмауэре кнопкой в окне сервера " +
            "и пробросьте его на роутере, если сервер за роутером.\n\n" +
            "Установлено в:\n" + dest;
        Log(text.Replace("\n", " | "));
        if (!Quiet)
        {
            MessageBox.Show(text, Title, MessageBoxButtons.OK, MessageBoxIcon.Information);
        }
    }
}

internal sealed class InstallWindow : Form
{
    private readonly Label label;

    public InstallWindow(string title)
    {
        Text = title;
        FormBorderStyle = FormBorderStyle.FixedDialog;
        StartPosition = FormStartPosition.CenterScreen;
        MaximizeBox = false;
        MinimizeBox = false;
        ClientSize = new Size(460, 118);
        BackColor = Color.White;

        label = new Label();
        label.AutoSize = false;
        label.SetBounds(18, 20, 424, 46);
        label.Font = new Font("Segoe UI", 9.5f);
        label.Text = "Подготовка…";
        Controls.Add(label);

        ProgressBar bar = new ProgressBar();
        bar.Style = ProgressBarStyle.Marquee;
        bar.MarqueeAnimationSpeed = 30;
        bar.SetBounds(18, 72, 424, 18);
        Controls.Add(bar);
    }

    public void Step(string text)
    {
        label.Text = text;
        Application.DoEvents();
    }
}
