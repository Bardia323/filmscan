// Tiny launcher: starts the film scanning app (server.py) with the Python in .venv.
using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

static class FilmScan
{
    [STAThread]
    static void Main()
    {
        string here = AppDomain.CurrentDomain.BaseDirectory;
        string app = File.Exists(Path.Combine(here, "server.py")) ? here : Path.Combine(here, "scanner-re");
        string py = Path.Combine(app, ".venv", "Scripts", "pythonw.exe");
        if (!File.Exists(py) || !File.Exists(Path.Combine(app, "server.py")))
        {
            MessageBox.Show("Film Scan could not find its files.\n\nExpected the folder:\n" + app +
                            "\n\nKeep FilmScan.exe in the app folder (next to server.py) and run setup.bat first.", "Film Scan",
                            MessageBoxButtons.OK, MessageBoxIcon.Error);
            return;
        }
        var psi = new ProcessStartInfo(py, "server.py") { WorkingDirectory = app, UseShellExecute = false };
        try { Process.Start(psi); }
        catch (Exception e) { MessageBox.Show("Could not start Film Scan:\n" + e.Message, "Film Scan", MessageBoxButtons.OK, MessageBoxIcon.Error); }
    }
}
